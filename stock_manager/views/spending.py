"""Spending sheets: collect issued lines, print the draft, record the vouchers.

The pages are the warehouse manager's paperwork desk. The rules live in
:mod:`stock_manager.spending`; nothing here moves stock.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.utils.translation import gettext as _
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from .. import spending
from ..models import IssueVoucher, SpendingSheet
from ..permissions import is_warehouse_manager
from .document_pages import form_date


def _refused(request):
    """True (with a message) when this reader may not do the paperwork."""
    if is_warehouse_manager(request.user):
        return False
    messages.error(request, _("Only a warehouse manager can open this page."))
    return True


@ensure_csrf_cookie
@login_required
def spending_page(request):
    """Open issued lines, the sheets made from them and the Oracle hand-off."""
    if _refused(request):
        return HttpResponseRedirect("/")

    if request.method == "POST":
        try:
            sheet = spending.create_sheet(
                line_ids=request.POST.getlist("line"), user=request.user
            )
        except spending.SpendingError as exc:
            messages.error(request, str(exc))
            return HttpResponseRedirect("/spending/")
        messages.success(
            request,
            _("Draft spending sheet %(number)s is ready to print.")
            % {"number": sheet.number},
        )
        return HttpResponseRedirect(f"/spending/{sheet.pk}/")

    date_from = form_date(request.GET.get("from"))
    date_to = form_date(request.GET.get("to"))
    open_rows = [
        {
            "line": line,
            "place": " — ".join(part for part in spending.place_of(line.document) if part),
            "request_number": spending.request_number(line.document),
        }
        for line in spending.open_lines(date_from=date_from, date_to=date_to)
    ]
    return render(
        request,
        "pages/spending.html",
        {
            "open_rows": open_rows,
            "date_from": date_from,
            "date_to": date_to,
            "sheets": spending.sheets(),
            "pending_vouchers": spending.voucher_summary(spending.pending_vouchers()),
            "posted_vouchers": spending.voucher_summary(spending.posted_vouchers()),
        },
    )


@ensure_csrf_cookie
@login_required
def spending_sheet_page(request, pk):
    """One draft sheet: its rows, and the voucher numbers written beside them."""
    if _refused(request):
        return HttpResponseRedirect("/")
    sheet = get_object_or_404(SpendingSheet.objects.select_related("created_by"), pk=pk)
    self_url = f"/spending/{sheet.pk}/"

    # What was typed survives a refusal: the ticks and the number come back.
    typed = {"voucher_no": "", "voucher_date": "", "rows": set()}

    if request.method == "POST":
        action = request.POST.get("action", "")
        selected = request.POST.getlist("row")
        number = sheet.number
        try:
            if action == "voucher":
                voucher = spending.record_voucher(
                    sheet,
                    line_ids=selected,
                    number=request.POST.get("voucher_no", ""),
                    voucher_date=form_date(request.POST.get("voucher_date")),
                    add_to_existing=bool(request.POST.get("add_to_existing")),
                    user=request.user,
                )
                messages.success(
                    request,
                    _("Voucher %(number)s is recorded on the chosen lines.")
                    % {"number": voucher.number},
                )
            elif action == "clear":
                spending.clear_voucher(sheet, line_ids=selected, user=request.user)
                messages.success(request, _("The voucher number was removed."))
            elif action == "remove":
                if spending.remove_lines(sheet, line_ids=selected, user=request.user):
                    messages.success(
                        request,
                        _("Sheet %(number)s had no lines left and was deleted.")
                        % {"number": number},
                    )
                    return HttpResponseRedirect("/spending/")
                messages.success(request, _("The lines are back on the open list."))
            elif action == "delete":
                spending.delete_sheet(sheet, user=request.user)
                messages.success(
                    request,
                    _("Sheet %(number)s was deleted; its lines are open again.")
                    % {"number": number},
                )
                return HttpResponseRedirect("/spending/")
            else:
                messages.error(request, _("Unknown action."))
        except spending.SpendingError as exc:
            messages.error(request, str(exc))
            typed = {
                "voucher_no": request.POST.get("voucher_no", ""),
                "voucher_date": request.POST.get("voucher_date", ""),
                "rows": set(selected),
            }
        else:
            return HttpResponseRedirect(self_url)

    rows = spending.sheet_rows(sheet)
    for row in rows:
        row["checked"] = row["value"] in typed["rows"]
    vouchers = IssueVoucher.objects.filter(lines__sheet=sheet).distinct()
    return render(
        request,
        "pages/spending_sheet.html",
        {
            "sheet": sheet,
            "rows": rows,
            "typed": typed,
            "unnumbered": sum(1 for row in rows if row["voucher"] is None),
            "has_vouchers": vouchers.exists(),
            "vouchers": spending.voucher_summary(
                IssueVoucher.objects.filter(pk__in=vouchers.values("pk"))
                .select_related("oracle_posted_by")
                .prefetch_related(
                    "lines__document__site",
                    "lines__item__internal_group",
                    "lines__voucher",
                )
                .order_by("created_at", "id")
            ),
        },
    )


@login_required
def spending_sheet_print(request, pk):
    """The printable DRAFT that is copied by hand into the voucher book."""
    if _refused(request):
        return HttpResponseRedirect("/")
    sheet = get_object_or_404(SpendingSheet.objects.select_related("created_by"), pk=pk)
    rows = spending.sheet_rows(sheet)
    return render(
        request,
        "pages/spending_print.html",
        {"sheet": sheet, "rows": rows},
    )


@require_POST
@login_required
def post_voucher(request, pk):
    """Record that the Oracle clerk deducted one voucher."""
    if _refused(request):
        return HttpResponseRedirect("/")
    voucher = get_object_or_404(IssueVoucher, pk=pk)
    try:
        spending.post_voucher(
            voucher, user=request.user, note=request.POST.get("note", "")
        )
    except spending.SpendingError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            _("Voucher %(number)s is marked as deducted in Oracle.")
            % {"number": voucher.number},
        )
    return HttpResponseRedirect("/spending/")
