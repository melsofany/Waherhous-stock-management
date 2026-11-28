"""Stock document pages and the helpers they share.

The stock page, the document detail, photo and decision views, and the form
helpers the adding pages share. A document is the audit record of a movement; the
movement itself is applied once, and only after approval.
"""


from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User  # For accessing the User model
from django.db.models import Sum
from django.db.models.functions import Lower
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.utils.translation import gettext as _
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from .. import documents as stock_documents
from ..models import (
    Item,
    ItemGroup,
    ShopItem,
    StockDocument,
)
from ..permissions import is_warehouse_manager
from ..ui_strings import js_messages

# Stock documents: separate pages for adding, issuing, the warehouse and the
# issues report. A document is the audit record of a movement; the movement
# itself is applied once, and only after approval.
# ---------------------------------------------------------------------------

RECENT_DOCUMENTS = 25


def _form_lines(request):
    """Read the repeated sku/quantity inputs of a document form."""
    skus = request.POST.getlist("sku")
    quantities = request.POST.getlist("quantity")
    descriptions = request.POST.getlist("description")
    units = request.POST.getlist("unit")
    return [
        {
            "sku": sku,
            "quantity": quantity,
            "description": descriptions[index] if index < len(descriptions) else "",
            "unit": units[index] if index < len(units) else "",
        }
        for index, (sku, quantity) in enumerate(zip(skus, quantities))
        if str(sku).strip()
    ]


def document_from_form(request, doc_type, *, pending=None):
    site_id = request.POST.get("site") or None
    site = (
        User.objects.filter(id=site_id, groups__name="shop_users").first()
        if site_id
        else None
    )
    return stock_documents.create_document(
        doc_type=doc_type,
        lines=_form_lines(request),
        site=site,
        counterparty=request.POST.get("counterparty", ""),
        reference=request.POST.get("reference", ""),
        external_ref=request.POST.get("external_ref", ""),
        notes=request.POST.get("notes", ""),
        created_by=request.user,
        source=StockDocument.WEB,
        # Only a warehouse manager may move stock without approval.
        pending=not is_warehouse_manager(request.user) if pending is None else pending,
        # The same paper the phone records: the date on it, where the goods go,
        # who signed it and who carried it. Typed on the web, these were
        # previously lost.
        document_date=form_date(request.POST.get("document_date")),
        location=request.POST.get("location", ""),
        engineer_name=request.POST.get("engineer_name", ""),
        representative=request.POST.get("representative", ""),
    )


def form_date(raw):
    """A date typed in a form, or None when it was left empty or unreadable."""
    from datetime import date

    text = (raw or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


@ensure_csrf_cookie
@login_required
def stock_page(request):
    """The warehouse and site stock, on their own page."""
    site_rows = (
        ShopItem.objects.select_related("item", "item__internal_group", "shop_user")
        .filter(quantity__gt=0)
        .order_by("shop_user__username", Lower("item__sku"))[:300]
    )
    return render(
        request,
        "pages/stock.html",
        {
            "js_messages": js_messages(),
            "site_rows": site_rows,
            "total_items": Item.objects.filter(is_active=True).count(),
            "internal_count": (
                ItemGroup.objects.filter(items__is_active=True).distinct().count()
                + Item.objects.filter(
                    is_active=True, internal_group__isnull=True
                ).count()
            ),
            "total_quantity": Item.objects.filter(is_active=True).aggregate(
                total=Sum("quantity")
            )["total"]
            or 0,
            "is_manager": is_warehouse_manager(request.user),
        },
    )

@require_POST
@login_required
def decide_document(request, pk, action):
    """Approve or reject a pending document. Managers only."""
    if not is_warehouse_manager(request.user):
        messages.error(request, _("Only a warehouse manager can approve documents."))
        return HttpResponseRedirect(request.POST.get("next") or "/issues/")
    document = StockDocument.objects.filter(pk=pk).first()
    if document is None:
        messages.error(request, _("Document not found."))
        return HttpResponseRedirect(request.POST.get("next") or "/issues/")
    note = request.POST.get("note", "")
    try:
        if action == "approve":
            stock_documents.approve_document(document, user=request.user, note=note)
            messages.success(
                request,
                _("Document %(number)s approved and the stock was moved.") % {
                    "number": document.number
                },
            )
        elif action == "reject":
            stock_documents.reject_document(document, user=request.user, note=note)
            messages.success(
                request,
                _("Document %(number)s rejected; nothing was moved.") % {
                    "number": document.number
                },
            )
        else:
            messages.error(request, _("Unknown action."))
    except stock_documents.DocumentError as exc:
        messages.error(request, str(exc))
    return HttpResponseRedirect(request.POST.get("next") or "/issues/")




@login_required
def document_detail(request, pk):
    """The paper behind a document: what the site wrote, and what it moved.

    A manager deciding on a document needs the voucher itself — the date on it,
    where the goods go, who signed it and the photograph — not just a list of
    quantities. Everything the phone sent is shown here, and approve/reject post
    to the same view the lists use, so there is one decision path.
    """
    document = (
        StockDocument.objects.select_related("site", "created_by", "decided_by")
        .prefetch_related("lines__voucher")
        .filter(pk=pk)
        .first()
    )
    if document is None:
        messages.error(request, _("Document not found."))
        return HttpResponseRedirect("/issues/")
    return render(
        request,
        "pages/document_detail.html",
        {
            "document": document,
            "lines": document.lines.select_related(
                "item__internal_group", "sheet", "voucher"
            ).all(),
            "has_photo": bool(document.photo),
            "is_manager": is_warehouse_manager(request.user),
            "back_url": request.GET.get("next")
            or ("/receipts/" if document.direction == StockDocument.IN else "/issues/"),
        },
    )


@login_required
def document_photo(request, pk):
    """The photographed voucher, straight from the database.

    Not a file under MEDIA_ROOT: Render wipes the disk on every deploy, so a
    stored path becomes a broken image within a week.
    """
    document = StockDocument.objects.filter(pk=pk).only("photo", "photo_type").first()
    if document is None or not document.photo:
        raise Http404("No photo")
    response = HttpResponse(bytes(document.photo), content_type=document.photo_type or "image/jpeg")
    response["Cache-Control"] = "private, max-age=3600"
    return response



def announce(request, document):
    """Tell the operator exactly what happened to their document."""
    if document.status == StockDocument.PENDING:
        messages.warning(
            request,
            _("Document %(number)s was saved and is waiting for a warehouse manager to approve it.")
            % {"number": document.number},
        )
    else:
        messages.success(
            request,
            _("Document %(number)s was saved and the stock was moved.") % {
                "number": document.number
            },
        )
