"""The warehouse balance sheet: a printable Excel copy of the stock on hand.

One row per Oracle code. Several Oracle codes can share one internal code, so
the internal code repeats on each row instead of hiding the member codes behind
a concatenated cell -- the sheet must still be filterable by Oracle code.
"""

from io import BytesIO
from pathlib import Path

from openpyxl import Workbook
from openpyxl.drawing.image import Image as WorkbookImage
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.properties import PageSetupProperties

from .models import Admin, Item
from .utils import escape_formula

HEADERS = (
    "م",
    "الكود الداخلي",
    "كود أوراكل",
    "التوصيف",
    "وحدة القياس",
    "الكمية",
)

LOGO_PATH = Path(__file__).resolve().parent / "static" / "img" / "company-logo.png"

BRAND = "003D7D"
HEADER_FILL = PatternFill("solid", fgColor=BRAND)
STRIPE_FILL = PatternFill("solid", fgColor="F3F6F9")
BORDER = Border(
    left=Side(style="thin", color="D5DDE5"),
    right=Side(style="thin", color="D5DDE5"),
    top=Side(style="thin", color="D5DDE5"),
    bottom=Side(style="thin", color="D5DDE5"),
)
CODE_FONT = Font(name="Consolas", size=10, bold=True, color="1D2D3E")
TEXT_FONT = Font(name="Arial", size=10, color="1D2D3E")


def balance_rows():
    """Return the active stock, one row per Oracle code."""
    items = (
        Item.objects.filter(is_active=True)
        .select_related("internal_group")
        .order_by("internal_group__code", "sku")
    )
    return [
        {
            "internal_code": item.internal_code,
            "sku": item.sku,
            "description": item.description,
            "unit": item.unit,
            "quantity": item.quantity,
        }
        for item in items
    ]


def _style_cell(cell, *, font, fill=None, alignment=None):
    cell.font = font
    cell.border = BORDER
    if fill is not None:
        cell.fill = fill
    if alignment is not None:
        cell.alignment = alignment


def _add_logo(sheet):
    if not LOGO_PATH.exists():
        return
    logo = WorkbookImage(str(LOGO_PATH))
    width = 100
    height = int(width * logo.height / logo.width)
    logo.width = width
    logo.height = height
    sheet.add_image(logo, "A1")


def workbook_bytes():
    """Build the balances workbook in memory and return its bytes."""
    rows = balance_rows()
    project = Admin.get_project()

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "الأرصدة"
    sheet.sheet_view.rightToLeft = True
    sheet.sheet_view.showGridLines = False

    widths = (15.5, 16, 20, 50, 12, 10)
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width

    sheet.merge_cells("B1:F1")
    project_name = sheet["B1"]
    project_name.value = escape_formula(project["name"]) or "—"
    project_name.font = Font(name="Arial", size=16, bold=True, color=BRAND)
    project_name.alignment = Alignment(horizontal="right", vertical="center")

    sheet.merge_cells("B2:F2")
    title = sheet["B2"]
    title.value = "كشف أرصدة المخزن"
    title.font = Font(name="Arial", size=12, italic=True, color="5B6B7A")
    title.alignment = Alignment(horizontal="right", vertical="center")

    _add_logo(sheet)
    sheet.row_dimensions[1].height = 30
    sheet.row_dimensions[2].height = 22
    sheet.row_dimensions[3].height = 8

    header_row = 4
    for column, header in enumerate(HEADERS, start=1):
        cell = sheet.cell(row=header_row, column=column, value=header)
        _style_cell(
            cell,
            font=Font(name="Arial", size=11, bold=True, color="FFFFFF"),
            fill=HEADER_FILL,
            alignment=Alignment(horizontal="center", vertical="center"),
        )
    sheet.row_dimensions[header_row].height = 28

    centered = Alignment(horizontal="center", vertical="center")
    wrapped = Alignment(horizontal="right", vertical="center", wrap_text=True)

    for offset, row in enumerate(rows):
        excel_row = header_row + 1 + offset
        values = (
            offset + 1,
            escape_formula(row["internal_code"]),
            escape_formula(row["sku"]),
            escape_formula(row["description"]),
            escape_formula(row["unit"]),
            row["quantity"],
        )
        for column, value in enumerate(values, start=1):
            cell = sheet.cell(row=excel_row, column=column, value=value)
            is_text = column != 1 and column != 6
            _style_cell(
                cell,
                font=CODE_FONT if column in (2, 3) else TEXT_FONT,
                fill=STRIPE_FILL if offset % 2 else None,
                alignment=wrapped if column == 4 else centered,
            )
            if is_text:
                cell.number_format = "@"
            elif column == 6:
                cell.number_format = "0"
        sheet.row_dimensions[excel_row].height = 20

    last_row = header_row + len(rows)
    if rows:
        sheet.auto_filter.ref = f"A{header_row}:F{last_row}"
    sheet.freeze_panes = f"A{header_row + 1}"
    sheet.print_title_rows = f"{header_row}:{header_row}"
    sheet.print_area = f"A1:F{max(last_row, header_row)}"

    sheet.page_setup.orientation = "portrait"
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    sheet.page_margins.top = 0.5
    sheet.page_margins.bottom = 0.5
    sheet.page_margins.left = 0.4
    sheet.page_margins.right = 0.4
    sheet.oddFooter.center.text = "&C&P / &N"

    output = BytesIO()
    workbook.save(output)
    return output.getvalue()
