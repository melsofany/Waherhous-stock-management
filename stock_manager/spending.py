"""Spending sheets and issue vouchers: the paperwork after the stock has left.

The warehouse deducts stock the moment a request is approved. Afterwards:

1. the issued lines are collected on a **draft spending sheet** (الصرفية) that
   shows quantities, Oracle codes and where each was issued;
2. the sheet is printed and copied by hand into the company's three-copy
   voucher book (the app never replaces that book);
3. the **voucher number** is written back beside the lines it covers — one sheet
   spans several vouchers, and one voucher may cover several work sites;
4. the Oracle clerk deducts each voucher from the company system, and that is
   marked here.

Nothing in this module changes a quantity. Every function only records which
paper an already-issued line is on.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.db.models import Count
from django.utils import timezone
from django.utils.translation import gettext as _

from .audit import record
from .models import (
    AuditEvent,
    DocumentPermit,
    IssueVoucher,
    SpendingSheet,
    StockDocument,
    StockDocumentLine,
)

MAX_VOUCHER_LENGTH = 80
MAX_NUMBER_ATTEMPTS = 5

_LINE_RELATED = ("document", "document__site", "item", "item__internal_group", "voucher")


class SpendingError(ValueError):
    """A refusal the warehouse manager can act on."""


# --- reading -----------------------------------------------------------------


def open_lines(*, date_from=None, date_to=None):
    """Issued lines that are on no sheet and no voucher yet.

    Only approved site issues qualify: a pending request has deducted nothing.
    Lines already documented another way — a company permit recorded on the
    document, or a document posted to Oracle before vouchers existed — are left
    out so the Oracle balance cannot be deducted twice.
    """
    lines = (
        StockDocumentLine.objects.select_related(*_LINE_RELATED)
        .filter(
            document__direction=StockDocument.OUT,
            document__doc_type=StockDocument.SITE_ISSUE,
            document__status=StockDocument.APPROVED,
            document__oracle_posted_at__isnull=True,
            sheet__isnull=True,
            voucher__isnull=True,
            permit__isnull=True,
        )
        .order_by("document__applied_at", "document_id", "id")
    )
    if date_from:
        lines = lines.filter(document__applied_at__date__gte=date_from)
    if date_to:
        lines = lines.filter(document__applied_at__date__lte=date_to)
    return lines


def place_of(document) -> tuple[str, str]:
    """(site name, written location) — where the goods of a request went."""
    site = document.site.get_username() if document.site_id else document.counterparty
    location = (document.location or "").strip()
    if location == site:
        location = ""
    if not site:
        site, location = location, ""
    return site or "", location


def request_number(document) -> str:
    """The number on the paper request, or the app's own number without one."""
    return document.external_ref or document.number


def group_lines(lines) -> list[dict]:
    """The rows of a sheet: one per place, item, unit and voucher.

    The book is written per item, so the same item issued to the same place on
    several requests is one row with the total, and the request numbers behind
    it stay visible.
    """
    rows: dict[tuple, dict] = {}
    for line in lines:
        site, location = place_of(line.document)
        unit = line.unit or (line.item.unit if line.item else "")
        key = (site, location, line.sku, unit, line.voucher_id)
        row = rows.get(key)
        if row is None:
            row = rows[key] = {
                "site": site,
                "location": location,
                "place": " — ".join(part for part in (site, location) if part),
                "sku": line.sku,
                "internal_code": line.item.internal_code if line.item else "",
                "description": line.description,
                "unit": unit,
                "quantity": 0,
                "line_ids": [],
                "requests": [],
                "voucher": line.voucher,
            }
        row["quantity"] += line.quantity
        row["line_ids"].append(line.pk)
        number = request_number(line.document)
        if number not in row["requests"]:
            row["requests"].append(number)

    result = sorted(
        rows.values(),
        key=lambda row: (row["site"], row["location"], row["sku"], row["unit"]),
    )
    for row in result:
        row["value"] = ",".join(str(pk) for pk in row["line_ids"])
    return result


def sheet_lines(sheet):
    return sheet.lines.select_related(*_LINE_RELATED).order_by("document_id", "id")


def sheet_rows(sheet) -> list[dict]:
    return group_lines(sheet_lines(sheet))


def sheets(limit=50):
    """The latest sheets with how many of their lines already have a number."""
    return (
        SpendingSheet.objects.select_related("created_by")
        .annotate(
            line_count=Count("lines"),
            numbered_count=Count("lines__voucher"),
        )
        .order_by("-created_at", "-id")[:limit]
    )


def voucher_summary(vouchers) -> list[dict]:
    """Each voucher with what the Oracle clerk has to deduct for it."""
    result = []
    for voucher in vouchers:
        lines = list(voucher.lines.all())
        rows = group_lines(lines)
        places = []
        for row in rows:
            if row["place"] and row["place"] not in places:
                places.append(row["place"])
        result.append({"voucher": voucher, "rows": rows, "places": places})
    return result


def _vouchers():
    return IssueVoucher.objects.select_related("oracle_posted_by").prefetch_related(
        "lines__document__site", "lines__item__internal_group", "lines__voucher"
    )


def pending_vouchers():
    """Vouchers the Oracle clerk still has to deduct, oldest first."""
    return list(
        _vouchers().filter(oracle_posted_at__isnull=True).order_by("created_at", "id")
    )


def posted_vouchers(limit=20):
    return list(
        _vouchers()
        .filter(oracle_posted_at__isnull=False)
        .order_by("-oracle_posted_at", "-id")[:limit]
    )


# --- sheets ------------------------------------------------------------------


def parse_line_ids(values) -> list[int]:
    """Line ids from the form; one row's checkbox carries several, comma-joined."""
    ids: list[int] = []
    for value in values or []:
        for part in str(value or "").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                ids.append(int(part))
            except ValueError:
                raise SpendingError(_("The selected line is not valid."))
    ids = list(dict.fromkeys(ids))
    if not ids:
        raise SpendingError(_("Choose at least one line."))
    return ids


def _changed():
    return SpendingError(
        _("The lines changed while you were reading them. Reload and try again.")
    )


def _next_sheet_number() -> str:
    stem = f"SH-{timezone.localdate().year}-"
    last = (
        SpendingSheet.objects.filter(number__startswith=stem)
        .order_by("-number")
        .values_list("number", flat=True)
        .first()
    )
    sequence = int(last.rsplit("-", 1)[1]) + 1 if last else 1
    return f"{stem}{sequence:04d}"


def _insert_sheet(user) -> SpendingSheet:
    for attempt in range(MAX_NUMBER_ATTEMPTS):
        try:
            with transaction.atomic():
                return SpendingSheet.objects.create(
                    number=_next_sheet_number(), created_by=user
                )
        except IntegrityError:
            if attempt == MAX_NUMBER_ATTEMPTS - 1:
                raise
    raise SpendingError(_("Could not number the spending sheet. Try again."))


def create_sheet(*, line_ids, user) -> SpendingSheet:
    """Collect open issued lines on a new draft sheet."""
    ids = parse_line_ids(line_ids)
    with transaction.atomic():
        found = list(
            open_lines()
            .select_related(None)
            .select_for_update()
            .filter(pk__in=ids)
            .values_list("pk", flat=True)
        )
        if len(found) != len(ids):
            raise _changed()
        sheet = _insert_sheet(user)
        StockDocumentLine.objects.filter(pk__in=ids).update(sheet=sheet)
    _log(user, AuditEvent.SHEET_CREATED, "spending_sheet", sheet.pk,
         f"{sheet.number} · {len(ids)} lines")
    return sheet


def _locked_sheet_lines(sheet, ids):
    lines = list(
        StockDocumentLine.objects.select_for_update().filter(pk__in=ids, sheet=sheet)
    )
    if len(lines) != len(ids):
        raise _changed()
    return lines


def remove_lines(sheet, *, line_ids, user) -> bool:
    """Send unnumbered lines back to the open list.

    Returns True when that left the sheet empty and it was deleted.
    """
    ids = parse_line_ids(line_ids)
    number, pk = sheet.number, sheet.pk
    with transaction.atomic():
        lines = _locked_sheet_lines(sheet, ids)
        if any(line.voucher_id for line in lines):
            raise SpendingError(
                _("Clear the voucher number before removing a line from the sheet.")
            )
        StockDocumentLine.objects.filter(pk__in=ids).update(sheet=None)
        emptied = not StockDocumentLine.objects.filter(sheet=sheet).exists()
        if emptied:
            sheet.delete()
    _log(user, AuditEvent.SHEET_CHANGED, "spending_sheet", pk,
         f"{number} · -{len(ids)} lines")
    return emptied


def delete_sheet(sheet, *, user) -> None:
    """Throw a draft away; its lines return to the open list."""
    number, pk = sheet.number, sheet.pk
    with transaction.atomic():
        lines = StockDocumentLine.objects.select_for_update().filter(sheet=sheet)
        if any(line.voucher_id for line in lines):
            raise SpendingError(
                _("This sheet already carries voucher numbers; clear them first.")
            )
        StockDocumentLine.objects.filter(sheet=sheet).update(sheet=None)
        sheet.delete()
    _log(user, AuditEvent.SHEET_CHANGED, "spending_sheet", pk, f"{number} · deleted")


# --- vouchers ----------------------------------------------------------------


def record_voucher(
    sheet, *, line_ids, number, user, voucher_date=None, add_to_existing=False
) -> IssueVoucher:
    """Write a handwritten voucher's number beside the chosen sheet lines."""
    number = (number or "").strip()
    if not number:
        raise SpendingError(_("Enter the issue voucher number from the book."))
    if len(number) > MAX_VOUCHER_LENGTH:
        raise SpendingError(_("The issue voucher number is too long."))
    ids = parse_line_ids(line_ids)

    permit = DocumentPermit.objects.filter(permit_no=number).select_related("document").first()
    if permit is not None:
        raise SpendingError(
            _("Number %(number)s is already recorded as a company permit on %(document)s.")
            % {"number": number, "document": permit.document.number}
        )

    try:
        with transaction.atomic():
            lines = _locked_sheet_lines(sheet, ids)
            if any(line.voucher_id for line in lines):
                raise SpendingError(
                    _("A chosen line already has a voucher number; clear it first.")
                )
            if any(line.permit_id for line in lines):
                raise SpendingError(
                    _("A chosen line is already covered by a company permit.")
                )
            voucher = (
                IssueVoucher.objects.select_for_update().filter(number=number).first()
            )
            if voucher is None:
                voucher = IssueVoucher.objects.create(
                    number=number, voucher_date=voucher_date, created_by=user
                )
            elif voucher.oracle_posted_at is not None:
                raise SpendingError(
                    _("Voucher %(number)s was already posted to Oracle; no line can be added to it.")
                    % {"number": number}
                )
            elif not add_to_existing:
                # A repeated number is usually a typing mistake, so adding to a
                # voucher that exists has to be said out loud.
                raise SpendingError(
                    _("Voucher %(number)s is already recorded. If these lines are on the same paper, tick “Add to the existing voucher”.")
                    % {"number": number}
                )
            elif voucher_date and voucher.voucher_date is None:
                voucher.voucher_date = voucher_date
                voucher.save(update_fields=["voucher_date"])
            StockDocumentLine.objects.filter(pk__in=ids).update(voucher=voucher)
    except IntegrityError:
        raise SpendingError(_("That voucher number was just recorded elsewhere."))

    _log(user, AuditEvent.VOUCHER_RECORDED, "issue_voucher", voucher.pk,
         f"{voucher.number} · {sheet.number} · {len(ids)} lines")
    return voucher


def clear_voucher(sheet, *, line_ids, user) -> int:
    """Take a wrongly typed number off sheet lines, until Oracle has it."""
    ids = parse_line_ids(line_ids)
    with transaction.atomic():
        lines = _locked_sheet_lines(sheet, ids)
        numbered = [line for line in lines if line.voucher_id]
        if not numbered:
            raise SpendingError(_("The chosen lines have no voucher number."))
        vouchers = list(
            IssueVoucher.objects.select_for_update().filter(
                pk__in={line.voucher_id for line in numbered}
            )
        )
        posted = [v.number for v in vouchers if v.oracle_posted_at is not None]
        if posted:
            raise SpendingError(
                _("Voucher %(number)s was already posted to Oracle and cannot be changed.")
                % {"number": ", ".join(posted)}
            )
        StockDocumentLine.objects.filter(
            pk__in=[line.pk for line in numbered]
        ).update(voucher=None)
        numbers = ", ".join(sorted(v.number for v in vouchers))
        for voucher in vouchers:
            # An empty voucher is deleted so its number can be typed again.
            if not voucher.lines.exists():
                voucher.delete()
    _log(user, AuditEvent.VOUCHER_CLEARED, "spending_sheet", sheet.pk,
         f"{numbers} · {sheet.number} · {len(numbered)} lines")
    return len(numbered)


def post_voucher(voucher, *, user, note="") -> IssueVoucher:
    """Record that the Oracle clerk deducted this voucher. Moves no stock."""
    with transaction.atomic():
        locked = IssueVoucher.objects.select_for_update().get(pk=voucher.pk)
        if locked.oracle_posted_at is not None:
            raise SpendingError(
                _("Voucher %(number)s was already posted to Oracle.")
                % {"number": locked.number}
            )
        locked.oracle_posted_at = timezone.now()
        locked.oracle_posted_by = user
        locked.oracle_note = (note or "").strip()[:200]
        locked.save(
            update_fields=["oracle_posted_at", "oracle_posted_by", "oracle_note"]
        )
    _log(user, AuditEvent.VOUCHER_ORACLE_POSTED, "issue_voucher", locked.pk, locked.number)
    return locked


def _log(user, action, entity, entity_id, description):
    record(
        user=user,
        action=action,
        entity=entity,
        entity_id=entity_id,
        description=description,
    )
