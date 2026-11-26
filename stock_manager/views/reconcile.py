"""Missing paperwork: the reconciliation page, its permit forms and export."""


from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from .. import documents as stock_documents
from .. import reconciliation
from ..models import (
    GoodsReceipt,
    StockDocument,
)
from ..permissions import is_warehouse_manager
from .transfers import site_accounts

RECONCILIATION_PAGE_SIZE = 100


def _reconciliation_redirect(request):
    """Back to the page with the filters the operator was reading."""
    target = request.POST.get("next") or "/reconciliation/"
    if not url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        target = "/reconciliation/"
    return HttpResponseRedirect(target)


@login_required
def reconciliation_page(request):
    """Everything that moved (or should have) without complete paperwork."""
    filters = reconciliation.parse_filters(request.GET)
    rows = reconciliation.build_rows(**filters)
    page = Paginator(rows, RECONCILIATION_PAGE_SIZE).get_page(request.GET.get("page"))

    query = request.GET.copy()
    query.pop("page", None)
    querystring = query.urlencode()
    # One place builds the three URLs, so none of them can grow a stray "?" or
    # lose the filters the operator is reading.
    suffix = f"?{querystring}" if querystring else ""
    page_prefix = f"/reconciliation/?{querystring}&" if querystring else "/reconciliation/?"

    return render(
        request,
        "pages/reconciliation.html",
        {
            "rows": page,
            "summary": reconciliation.summarize(rows),
            "reasons": reconciliation.REASON_LABELS,
            "short_reasons": reconciliation.SHORT_REASON_LABELS,
            "filters": filters,
            "sites": site_accounts(),
            "next_url": f"/reconciliation/{suffix}",
            "export_url": f"/reconciliation/export/{suffix}",
            "page_url_prefix": page_prefix,
            "is_manager": is_warehouse_manager(request.user),
        },
    )


@login_required
@require_POST
def reconciliation_set_permit(request, pk):
    """Record the company voucher on a document, approving it when asked."""
    if not is_warehouse_manager(request.user):
        messages.error(
            request, _("Only a warehouse manager can complete the paperwork.")
        )
        return _reconciliation_redirect(request)

    document = get_object_or_404(StockDocument, pk=pk)
    try:
        reconciliation.record_permit(
            document,
            request.POST.get("external_ref"),
            approve=request.POST.get("approve") == "1",
            user=request.user,
        )
    except stock_documents.DocumentError as exc:
        messages.error(request, str(exc))
        return _reconciliation_redirect(request)

    messages.success(
        request,
        _("Voucher recorded on %(number)s.") % {"number": document.number},
    )
    return _reconciliation_redirect(request)


@login_required
@require_POST
def reconciliation_set_receipt_permit(request, pk):
    """Record the company voucher of a purchase-order delivery."""
    if not is_warehouse_manager(request.user):
        messages.error(
            request, _("Only a warehouse manager can complete the paperwork.")
        )
        return _reconciliation_redirect(request)

    receipt = get_object_or_404(GoodsReceipt, pk=pk)
    try:
        reconciliation.record_receipt_permit(
            receipt, request.POST.get("external_ref"), user=request.user
        )
    except stock_documents.DocumentError as exc:
        messages.error(request, str(exc))
        return _reconciliation_redirect(request)

    messages.success(
        request,
        _("Voucher recorded on the delivery of %(po)s.") % {"po": receipt.po_number},
    )
    return _reconciliation_redirect(request)


@login_required
def reconciliation_export(request):
    """The same list, as a spreadsheet."""
    rows = reconciliation.build_rows(**reconciliation.parse_filters(request.GET))
    response = HttpResponse(
        reconciliation.workbook_bytes(rows),
        content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    )
    response["Content-Disposition"] = 'attachment; filename="missing-paperwork.xlsx"'
    return response
