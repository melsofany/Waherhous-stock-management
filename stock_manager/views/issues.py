"""Issuing stock to a site: the issue API, the issues page, the issues report,
and the company permits (إذن صرف) page.
"""

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User  # For accessing the User model
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import (
    Response,
)  # For returning HTTP responses in REST framework

from .. import documents as stock_documents
from .. import issue_report
from .. import permits as company_permits
from ..models import (
    Admin,
    Item,
    StockDocument,
)
from ..permissions import is_warehouse_manager
from .document_pages import RECENT_DOCUMENTS, announce, document_from_form
from .transfers import site_accounts, transfer_to_shop

logger = logging.getLogger(__name__)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def issue_to_site(request):
    """Move stock from the warehouse to a construction site in one step."""
    if Admin.is_edit_locked():
        return Response(
            {"detail": _("Issuing stock is disabled while the warehouse is being maintained.")},
            status=status.HTTP_403_FORBIDDEN,
        )
    if not is_warehouse_manager(request.user):
        return Response(
            {"detail": _("Only a warehouse manager can issue stock to a site.")},
            status=status.HTTP_403_FORBIDDEN,
        )

    sku = str(request.data.get("sku") or "").strip()
    site_id = request.data.get("site_id")
    try:
        quantity = int(str(request.data.get("quantity")))
    except (TypeError, ValueError):
        return Response(
            {"detail": _("The issued quantity must be a whole number.")},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if quantity <= 0:
        return Response(
            {"detail": _("The issued quantity must be greater than zero.")},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        item = Item.objects.get(sku=sku)
    except Item.DoesNotExist:
        return Response({"detail": _("Item not found.")}, status=status.HTTP_404_NOT_FOUND)

    site = User.objects.filter(id=site_id, groups__name="shop_users").first()
    if site is None:
        return Response(
            {"detail": _("Site not found.")}, status=status.HTTP_400_BAD_REQUEST
        )

    try:
        transfer_to_shop(
            item=item,
            shop_user=site.id,
            transfer_quantity=quantity,
            complete=True,
            manager=True,
        )
    except (ValueError, LookupError) as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception:
        logger.error("Unexpected error while issuing to a site.", exc_info=True)
        return Response(
            {"detail": _("An unexpected error occurred while issuing the stock.")},
            status=status.HTTP_400_BAD_REQUEST,
        )

    item.refresh_from_db()
    return Response(
        {
            "detail": _("Stock issued to the site."),
            "sku": item.sku,
            "site": site.username,
            "warehouse_quantity": item.quantity,
        }
    )


# ---------------------------------------------------------------------------

@ensure_csrf_cookie
@login_required
def issues_page(request):
    """Issues: to a site, to another project, or back to the supplier."""
    if request.method == "POST":
        doc_type = request.POST.get("doc_type", "")
        if doc_type not in StockDocument.OUT_TYPES:
            messages.error(request, _("Choose the type of issue."))
            return HttpResponseRedirect("/issues/")
        try:
            document = document_from_form(request, doc_type)
        except stock_documents.DocumentError as exc:
            messages.error(request, str(exc))
            return HttpResponseRedirect("/issues/")
        announce(request, document)
        return HttpResponseRedirect("/issues/")

    return render(
        request,
        "pages/issues.html",
        {
            "sites": site_accounts(),
            "unit_options": stock_documents.COMMON_UNITS,
            "types": [
                (value, _(label))
                for value, label in StockDocument.TYPE_CHOICES
                if value in StockDocument.OUT_TYPES
            ],
            "pending": StockDocument.objects.select_related("site", "created_by")
            .prefetch_related("lines")
            .filter(status=StockDocument.PENDING, direction=StockDocument.OUT),
            "pending_title": _("Waiting for approval"),
            "back_url": "/issues/",
            "recent": StockDocument.objects.select_related(
                "site", "created_by", "decided_by"
            )
            .prefetch_related("lines")
            .filter(direction=StockDocument.OUT)[:RECENT_DOCUMENTS],
            "is_manager": is_warehouse_manager(request.user),
            "form_title": _("New issue"),
            "form_hint": _("Stock goes OUT"),
            "submit_label": _("Save the issue"),
            "site_types": "site_issue,site_return",
            "action_url": "/issues/",
        },
    )

@ensure_csrf_cookie
@login_required
def issues_report_page(request):
    """Everything that was issued in a date range, with an Excel export."""
    date_from, date_to = issue_report.parse_range(
        request.GET.get("from"), request.GET.get("to")
    )
    report = issue_report.build_report(date_from, date_to)

    if request.GET.get("export") == "xlsx":
        response = HttpResponse(
            issue_report.workbook_bytes(report),
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )
        response["Content-Disposition"] = (
            f'attachment; filename="issues-{date_from}-to-{date_to}.xlsx"'
        )
        return response

    return render(
        request,
        "pages/issue_report.html",
        {
            "report": report,
            "date_from": date_from,
            "date_to": date_to,
            # The permits page returns here, so the operator keeps the window
            # they were reading instead of landing on a default one.
            "report_back": (
                f"/issues/report/?from={date_from:%Y-%m-%d}&to={date_to:%Y-%m-%d}"
            ),
        },
    )


PERMIT_BLANK_ROWS = 2


def _permits_context(document, *, is_manager, rows=None, assignments=None):
    """Everything the permits page shows, from the database or a failed save."""
    lines = list(document.lines.select_related("item").all())

    if rows is None:
        rows = [
            {
                "permit_no": permit.permit_no,
                "amount": permit.amount,
                "amount_display": company_permits.format_amount(permit.amount),
                "note": permit.note,
            }
            for permit in document.permits.all()
        ]
        assignments = {
            str(line.pk): line.permit.permit_no
            for line in lines
            if line.permit_id is not None
        }

    # Two blank rows are always offered so a second permit can be added without
    # a script; saving them offers the next pair.
    rows = list(rows) + [
        {"permit_no": "", "amount": "", "amount_display": "", "note": ""}
    ] * PERMIT_BLANK_ROWS

    return {
        "document": document,
        "rows": rows,
        "line_rows": [
            {
                "line": line,
                "value": company_permits.format_amount(
                    company_permits.line_value(line)
                ),
                "permit_no": assignments.get(str(line.pk), ""),
            }
            for line in lines
        ],
        "coverage": company_permits.coverage(document),
        "is_manager": is_manager,
    }


@login_required
def issue_permits_page(request, pk):
    """The company permits of one issue, and the items each one covers."""
    document = get_object_or_404(StockDocument, pk=pk)
    manager = is_warehouse_manager(request.user)
    self_url = f"/issues/{document.pk}/permits/"

    back = request.POST.get("next") or request.GET.get("next") or ""
    if not url_has_allowed_host_and_scheme(
        back, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        back = ""

    if request.method != "POST":
        return render(
            request,
            "pages/issue_permits.html",
            {
                **_permits_context(document, is_manager=manager),
                "back": back or "/issues/report/",
            },
        )

    if not manager:
        messages.error(
            request, _("Only a warehouse manager can record company permits.")
        )
        return HttpResponseRedirect(back or self_url)

    line_ids = {str(line.pk) for line in document.lines.all()}
    try:
        rows, assignments = company_permits.parse_form(request.POST, line_ids)
        company_permits.save_permits(document, rows, assignments, user=request.user)
    except company_permits.PermitError as exc:
        # Re-render what was typed: losing three permit numbers to a validation
        # error is how an operator stops using the page.
        context = _permits_context(
            document,
            is_manager=manager,
            rows=company_permits.submitted_rows(request.POST),
            assignments=company_permits.submitted_items(request.POST, line_ids),
        )
        context["back"] = back or "/issues/report/"
        messages.error(request, str(exc))
        return render(request, "pages/issue_permits.html", context)

    messages.success(
        request,
        _("Company permits recorded on %(number)s.") % {"number": document.number},
    )
    return HttpResponseRedirect(back or self_url)
