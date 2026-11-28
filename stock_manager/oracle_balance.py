"""The daily Oracle sheet, and the warehouse balance it is matched against.

Oracle sends a stock export with one row per Oracle code. That code is the
``Item.sku`` the warehouse already uses, so the comparison is a join on the
code, never on a description or a name: the description changes, the code does
not.

Two rules make the page useful rather than merely correct:

* A code that exists on one side only is a real discrepancy, not a missing
  value. It stays on the page with the missing side shown as ``—``.
* The sheet can carry the same code under more than one sub-inventory. Those
  rows are one article, so their quantities are summed into one line; the
  snapshot records how many rows were merged.
"""

from __future__ import annotations

import csv
import io
import numbers
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path as FilePath

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _lazy
from openpyxl import load_workbook

from .models import Item, OracleBalanceLine, OracleSnapshot
from .permits import format_amount

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_ROWS = 50_000
MAX_CODE_LENGTH = 100

MATCHED = "matched"
DIFFERENT = "different"
MISSING_WAREHOUSE = "missing_warehouse"
MISSING_ORACLE = "missing_oracle"

STATUS_LABELS = {
    MATCHED: _lazy("Matched"),
    DIFFERENT: _lazy("Quantity differs"),
    MISSING_WAREHOUSE: _lazy("Not in the warehouse"),
    MISSING_ORACLE: _lazy("Not in the Oracle sheet"),
}

STATUS_ORDER = {
    DIFFERENT: 0,
    MISSING_WAREHOUSE: 1,
    MISSING_ORACLE: 2,
    MATCHED: 3,
}

STATUS_CLASSES = {
    MATCHED: "sap-pill--positive",
    DIFFERENT: "sap-pill--critical",
    MISSING_WAREHOUSE: "sap-pill--critical",
    MISSING_ORACLE: "sap-pill--info",
}

HEADER_ALIASES = {
    "oracle_code": {
        "CODE",
        "ORACLE_CODE",
        "ORACLE_ITEM_CODE",
        "ITEM_CODE",
        "ITEM",
        "SKU",
    },
    "description": {"DESCRIPTION", "ITEM_DESCRIPTION", "NAME"},
    "unit": {"UOM", "UNIT", "UNIT_OF_MEASURE"},
    "quantity": {"ON_HAND", "ONHAND", "QUANTITY", "QTY", "BALANCE"},
    "organization_name": {"ORGANIZATION_NAME", "ORGANIZATION", "ORG_NAME"},
    "sub_inventory": {"SUB_INV", "SUBINV", "SUB_INVENTORY"},
    "unit_cost": {"UNIT_COST", "UNITCOST", "COST"},
    "value": {"VALUE", "TOTAL_VALUE", "AMOUNT"},
}

THOUSANDS = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$")
DECIMAL_COMMA = re.compile(r"^-?\d+,\d{1,6}$")


class OracleFileError(Exception):
    """A file the operator can fix: wrong columns, a bad number, no rows."""


@dataclass
class ParsedLine:
    oracle_code: str
    organization_name: str
    description: str
    unit: str
    quantity: Decimal
    sub_inventory: str
    unit_cost: Decimal
    value: Decimal


@dataclass
class ParsedBalance:
    lines: list[ParsedLine]
    duplicate_codes: int = 0


@dataclass(frozen=True)
class BalanceRow:
    status: str
    oracle_code: str
    internal_code: str
    description: str
    unit: str
    warehouse_quantity: Decimal | None
    oracle_quantity: Decimal | None
    difference: Decimal
    organization_name: str = ""
    sub_inventory: str = ""
    unit_cost: Decimal | None = None
    value: Decimal | None = None

    @property
    def status_label(self):
        return STATUS_LABELS[self.status]

    @property
    def status_class(self):
        return STATUS_CLASSES[self.status]

    @property
    def warehouse_text(self):
        return format_quantity(self.warehouse_quantity)

    @property
    def oracle_text(self):
        return format_quantity(self.oracle_quantity)

    @property
    def difference_text(self):
        return format_quantity(self.difference, signed=True)

    @property
    def value_text(self):
        return format_amount(self.value) if self.value is not None else "—"


def format_quantity(value, *, signed=False):
    """A quantity without a localised decimal comma."""
    if value is None:
        return "—"
    number = Decimal(value)
    if number == number.to_integral():
        text = f"{int(number):,}"
    else:
        text = f"{number:,.6f}".rstrip("0").rstrip(".")
    if signed and number > 0:
        return f"+{text}"
    return text


def parse_filters(params) -> dict:
    status = (params.get("status") or "").strip()
    return {
        "query": (params.get("q") or "").strip(),
        "status": status if status in STATUS_LABELS else "",
    }


def parse_upload(uploaded_file) -> ParsedBalance:
    """Read the uploaded sheet and return its rows, with no database writes."""
    raw = uploaded_file.read()
    if len(raw) > MAX_FILE_BYTES:
        raise OracleFileError(_("The file is larger than 10 MB."))

    suffix = FilePath(getattr(uploaded_file, "name", "") or "").suffix.lower()
    if suffix == ".xlsx":
        rows = _xlsx_rows(raw)
    elif suffix == ".csv":
        rows = _csv_rows(raw)
    elif suffix == ".xls":
        raise OracleFileError(
            _("The old .xls format is not supported. Save the sheet as .xlsx or .csv.")
        )
    else:
        raise OracleFileError(_("Upload an .xlsx or .csv file."))

    if len(rows) > MAX_ROWS:
        raise OracleFileError(
            _("The sheet has too many rows. The limit is %(max)s.") % {"max": MAX_ROWS}
        )
    return _read_rows(rows)


def _xlsx_rows(raw):
    try:
        workbook = load_workbook(BytesIO(raw), read_only=True, data_only=True)
    except Exception:
        raise OracleFileError(_("The .xlsx file could not be read."))
    try:
        sheet = workbook.active
        return [tuple(row) for row in sheet.iter_rows(values_only=True)]
    finally:
        workbook.close()


def _csv_rows(raw):
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("cp1256")
        except UnicodeDecodeError:
            raise OracleFileError(_("The .csv file could not be read."))
    return list(csv.reader(io.StringIO(text)))


def _header_key(value) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(value or "").strip().upper()).strip("_")


def _find_header(rows):
    for index, row in enumerate(rows[:20]):
        keys = [_header_key(cell) for cell in row]
        mapping = {}
        for field, aliases in HEADER_ALIASES.items():
            for column, key in enumerate(keys):
                if key in aliases:
                    mapping[field] = column
                    break
        if "oracle_code" in mapping and "quantity" in mapping:
            return index, mapping
    raise OracleFileError(
        _("The sheet must contain a CODE column and an ON_HAND (or QUANTITY) column.")
    )


def _cell(row, mapping, field):
    column = mapping.get(field)
    if column is None or column >= len(row):
        return None
    return row[column]


def _text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _number(value, *, row_number, field, required=True) -> Decimal:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise OracleFileError(
                _("Row %(row)s: %(field)s is empty.")
                % {"row": row_number, "field": field}
            )
        return Decimal("0")

    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, numbers.Number) and not isinstance(value, bool):
        number = Decimal(str(value))
    else:
        text = str(value).strip().replace("\u00a0", "").replace(" ", "")
        text = (
            text.replace("\u066c", "")
            .replace("٬", "")
            .replace("\u066b", ".")
            .replace("٫", ".")
            .replace("£", "")
            .replace("$", "")
            .replace("€", "")
        )
        if "," in text:
            if THOUSANDS.match(text):
                text = text.replace(",", "")
            elif DECIMAL_COMMA.match(text):
                text = text.replace(",", ".")
            else:
                raise OracleFileError(
                    _("Row %(row)s: %(field)s is not a number: %(value)s.")
                    % {"row": row_number, "field": field, "value": value}
                )
        try:
            number = Decimal(text)
        except (InvalidOperation, ValueError):
            raise OracleFileError(
                _("Row %(row)s: %(field)s is not a number: %(value)s.")
                % {"row": row_number, "field": field, "value": value}
            )

    if not number.is_finite():
        raise OracleFileError(
            _("Row %(row)s: %(field)s is not a number: %(value)s.")
            % {"row": row_number, "field": field, "value": value}
        )
    return number


def _read_rows(rows) -> ParsedBalance:
    header_index, mapping = _find_header(rows)
    by_code: dict[str, ParsedLine] = {}
    duplicates = 0

    for offset, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        if not any(_text(cell) for cell in row):
            continue
        code = _text(_cell(row, mapping, "oracle_code"))
        if not code:
            continue
        if len(code) > MAX_CODE_LENGTH:
            raise OracleFileError(
                _("Row %(row)s: the Oracle code is longer than %(max)s characters.")
                % {"row": offset, "max": MAX_CODE_LENGTH}
            )

        quantity = _number(
            _cell(row, mapping, "quantity"),
            row_number=offset,
            field=_("quantity"),
        )
        unit_cost = _number(
            _cell(row, mapping, "unit_cost"),
            row_number=offset,
            field=_("unit cost"),
            required=False,
        )
        value = _number(
            _cell(row, mapping, "value"),
            row_number=offset,
            field=_("value"),
            required=False,
        ).quantize(Decimal("0.01"))
        line = ParsedLine(
            oracle_code=code,
            organization_name=_text(_cell(row, mapping, "organization_name"))[:200],
            description=_text(_cell(row, mapping, "description"))[:500],
            unit=_text(_cell(row, mapping, "unit"))[:20],
            quantity=quantity,
            sub_inventory=_text(_cell(row, mapping, "sub_inventory"))[:120],
            unit_cost=unit_cost,
            value=value,
        )

        existing = by_code.get(code)
        if existing is None:
            by_code[code] = line
            continue

        existing.quantity += quantity
        existing.value += value
        duplicates += 1
        existing.organization_name = _join_text(
            existing.organization_name, line.organization_name, 200
        )
        existing.sub_inventory = _join_text(
            existing.sub_inventory, line.sub_inventory, 120
        )

    if not by_code:
        raise OracleFileError(_("The sheet has no Oracle codes."))
    return ParsedBalance(lines=list(by_code.values()), duplicate_codes=duplicates)


def _join_text(left: str, right: str, limit: int) -> str:
    if not right or right == left:
        return left
    if not left:
        return right[:limit]
    return f"{left} · {right}"[:limit]


def save_snapshot(parsed: ParsedBalance, *, user, source_name="", balance_date=None):
    """Store one snapshot and its lines in one transaction."""
    snapshot_date = balance_date or timezone.localdate()
    with transaction.atomic():
        snapshot = OracleSnapshot.objects.create(
            balance_date=snapshot_date,
            uploaded_by=user if getattr(user, "is_authenticated", False) else None,
            source_name=(source_name or "")[:200],
            row_count=len(parsed.lines),
            duplicate_codes=parsed.duplicate_codes,
            total_quantity=sum((line.quantity for line in parsed.lines), Decimal("0")),
            total_value=sum((line.value for line in parsed.lines), Decimal("0.00")),
        )
        OracleBalanceLine.objects.bulk_create(
            [
                OracleBalanceLine(
                    snapshot=snapshot,
                    oracle_code=line.oracle_code,
                    organization_name=line.organization_name,
                    description=line.description,
                    unit=line.unit,
                    quantity=line.quantity,
                    sub_inventory=line.sub_inventory,
                    unit_cost=line.unit_cost,
                    value=line.value,
                )
                for line in parsed.lines
            ]
        )
    return snapshot


def build_rows(snapshot: OracleSnapshot | None) -> list[BalanceRow]:
    """Compare one snapshot with the current warehouse stock."""
    if snapshot is None:
        return []

    items = {item.sku: item for item in Item.objects.select_related("internal_group").all()}
    rows: list[BalanceRow] = []
    for line in snapshot.lines.all():
        item = items.pop(line.oracle_code, None)
        if item is None:
            status = MISSING_WAREHOUSE
            warehouse = None
            difference = -line.quantity
            internal_code = ""
        else:
            warehouse = Decimal(item.quantity)
            status = MATCHED if warehouse == line.quantity else DIFFERENT
            difference = warehouse - line.quantity
            internal_code = item.internal_code

        rows.append(
            BalanceRow(
                status=status,
                oracle_code=line.oracle_code,
                internal_code=internal_code,
                description=line.description or (item.description if item else ""),
                unit=(item.unit if item and item.unit else line.unit),
                warehouse_quantity=warehouse,
                oracle_quantity=line.quantity,
                difference=difference,
                organization_name=line.organization_name,
                sub_inventory=line.sub_inventory,
                unit_cost=line.unit_cost,
                value=line.value,
            )
        )

    for item in items.values():
        if item.quantity <= 0:
            continue
        rows.append(
            BalanceRow(
                status=MISSING_ORACLE,
                oracle_code=item.sku,
                internal_code=item.internal_code,
                description=item.description,
                unit=item.unit,
                warehouse_quantity=Decimal(item.quantity),
                oracle_quantity=None,
                difference=Decimal(item.quantity),
            )
        )

    rows.sort(
        key=lambda row: (
            STATUS_ORDER[row.status],
            -abs(row.difference),
            row.oracle_code,
        )
    )
    return rows


@dataclass(frozen=True)
class GroupRow:
    """The total of one internal code, over all its Oracle codes."""

    internal_code: str
    oracle_codes: tuple[str, ...]
    warehouse_quantity: Decimal
    oracle_quantity: Decimal
    difference: Decimal
    status: str

    @property
    def status_label(self):
        return STATUS_LABELS[self.status]

    @property
    def status_class(self):
        return STATUS_CLASSES[self.status]

    @property
    def warehouse_text(self):
        return format_quantity(self.warehouse_quantity)

    @property
    def oracle_text(self):
        return format_quantity(self.oracle_quantity)

    @property
    def difference_text(self):
        return format_quantity(self.difference, signed=True)


def grouped_rows(rows) -> list[GroupRow]:
    """Groups with more than one Oracle code, totalled under our own code.

    An internal code is the warehouse's answer to one article carried by more
    than one Oracle code. The per-code rows still say which code moved; this
    list answers the different question the manager asks: does the article as a
    whole match Oracle, even when the quantity is split between its codes?
    """
    grouped = {}
    for row in rows:
        if not row.internal_code:
            continue
        entry = grouped.setdefault(
            row.internal_code,
            {"codes": [], "oracle_codes": [], "warehouse": Decimal("0"), "oracle": Decimal("0")},
        )
        entry["codes"].append(row.oracle_code)
        entry["warehouse"] += row.warehouse_quantity or Decimal("0")
        if row.oracle_quantity is not None:
            entry["oracle_codes"].append(row.oracle_code)
            entry["oracle"] += row.oracle_quantity

    result = []
    for code, entry in grouped.items():
        if len(entry["codes"]) < 2:
            continue
        if not entry["oracle_codes"]:
            status = MISSING_ORACLE
        elif entry["warehouse"] == entry["oracle"]:
            status = MATCHED
        else:
            status = DIFFERENT
        result.append(
            GroupRow(
                internal_code=code,
                oracle_codes=tuple(sorted(entry["oracle_codes"])),
                warehouse_quantity=entry["warehouse"],
                oracle_quantity=entry["oracle"],
                difference=entry["warehouse"] - entry["oracle"],
                status=status,
            )
        )
    result.sort(
        key=lambda row: (
            STATUS_ORDER[row.status],
            -abs(row.difference),
            row.internal_code,
        )
    )
    return result


def filter_rows(rows, *, query="", status="") -> list[BalanceRow]:
    if status:
        rows = [row for row in rows if row.status == status]
    if query:
        needle = query.lower()
        rows = [
            row
            for row in rows
            if needle in row.oracle_code.lower()
            or needle in row.description.lower()
            or needle in row.internal_code.lower()
        ]
    return rows


def summarize(rows) -> dict:
    counts = {status: 0 for status in STATUS_LABELS}
    for row in rows:
        counts[row.status] += 1
    counts["total"] = len(rows)
    counts["oracle_codes"] = sum(
        1 for row in rows if row.oracle_quantity is not None
    )
    counts["warehouse_codes"] = sum(
        1 for row in rows if row.warehouse_quantity is not None
    )
    counts["with_difference"] = (
        counts[DIFFERENT] + counts[MISSING_WAREHOUSE] + counts[MISSING_ORACLE]
    )
    return counts
