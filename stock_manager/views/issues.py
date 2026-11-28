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
from django.views.decorators.http import require_POST
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import (
    Response,
)  # For returning HTTP responses in REST framework

from .. import documents as stock_documents
from .. import issue_report
from .. import issue_workflow
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

def _request_groups(user):
    """The site queues this reader may see, with totals."""
    groups = issue_workflow.pending_groups()
    if not is_warehouse_manager(user):
        groups = [group for group in groups if group["site"].id == user.id]
    return groups


def _form_date(raw):
    from datetime import date

    text = (raw or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


@require_POST
@login_required
def create_issue_permit(request):
    """Close a site's collected requests into one numbered issue permit."""
    if not is_warehouse_manager(request.user):
        messages.error(request, _("Only a warehouse manager can create an issue permit."))
        return HttpResponseRedirect("/issues/")

    try:
        site_id = int(request.POST.get("site_id") or "")
    except (TypeError, ValueError):
        messages.error(request, _("Choose the site for this issue permit."))
        return HttpResponseRedirect("/issues/")
    site = User.objects.filter(
        id=site_id, groups__name="shop_users", is_active=True
    ).first()
    if site is None:
        messages.error(request, _("Choose the site for this issue permit."))
        return HttpResponseRedirect("/issues/")

    request_ids = request.POST.getlist("request_id")
    issued = {
        str(request_id): request.POST.get(f"issued_{request_id}")
        for request_id in request_ids
    }
    try:
        document = issue_workflow.create_permit(
            site=site,
            request_ids=request_ids,
            issued=issued,
            permit_no=request.POST.get("permit_no", ""),
            user=request.user,
            document_date=_form_date(request.POST.get("document_date")),
            location=request.POST.get("location", ""),
            engineer_name=request.POST.get("engineer_name", ""),
            notes=request.POST.get("notes", ""),
        )
    except (issue_workflow.IssuePermitError, stock_documents.DocumentError) as exc:
        messages.error(request, str(exc))
        return HttpResponseRedirect("/issues/")

    messages.success(
        request,
        _("Issue permit %(permit)s was created; its three copies are ready to print.")
        % {"permit": document.external_ref},
    )
    return HttpResponseRedirect(f"/issues/{document.pk}/print/")


@login_required
def issue_permit_print(request, pk):
    """The three carbon copies of one issue permit."""
    document = get_object_or_404(
        StockDocument.objects.select_related("site", "created_by").prefetch_related(
            "lines__item__internal_group"
        ),
        pk=pk,
    )
    if document.direction != StockDocument.OUT or document.doc_type != StockDocument.SITE_ISSUE:
        messages.error(request, _("Only a site issue permit has three copies."))
        return HttpResponseRedirect("/issues/")
    if not is_warehouse_manager(request.user) and document.site_id != request.user.id:
        messages.error(request, _("This issue permit belongs to another site."))
        return HttpResponseRedirect("/issues/")

    lines = list(document.lines.all())
    copies = [
        {
            "key": "warehouse",
            "label": _("Warehouse copy"),
            "hint": _("Keep this copy in the warehouse file."),
        },
        {
            "key": "site",
            "label": _("Site copy"),
            "hint": _("Give this copy to the site receiving the goods."),
        },
        {
            "key": "oracle",
            "label": _("Oracle copy"),
            "hint": _("Give this copy to the Oracle clerk to post the deduction."),
        },
    ]
    return render(
        request,
        "pages/issue_permit_print.html",
        {
            "document": document,
            "lines": lines,
            "copies": copies,
            "permit_no": document.external_ref or document.number,
            "document_date": document.document_date
            or (document.applied_at.date() if document.applied_at else None),
            "total_quantity": sum(line.quantity for line in lines),
        },
    )


@require_POST
@login_required
def mark_oracle_posted(request, pk):
    """Record that the Oracle copy was handed over and the balance posted."""
    if not is_warehouse_manager(request.user):
        messages.error(request, _("Only a warehouse manager can post to Oracle."))
        return HttpResponseRedirect("/issues/")
    document = get_object_or_404(StockDocument, pk=pk)
    try:
        issue_workflow.mark_oracle_posted(
            document, user=request.user, note=request.POST.get("note", "")
        )
    except issue_workflow.IssuePermitError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            _("Issue permit %(permit)s is posted to Oracle.") % {
                "permit": document.external_ref or document.number
            },
        )
    next_url = request.POST.get("next") or f"/documents/{document.pk}/"
    if not url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        next_url = f"/documents/{document.pk}/"
    return HttpResponseRedirect(next_url)


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
            "request_groups": _request_groups(request.user),
            "oracle_queue": issue_workflow.oracle_queue(),
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
            "request_groups": _request_groups(request.user),
            "oracle_queue": issue_workflow.oracle_queue(),
            "is_manager": is_warehouse_manager(request.user),
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
