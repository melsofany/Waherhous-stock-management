"""Goods receipts: booking a supplier delivery against a PO, listing receipts,
and the receipts page.
"""

import logging
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import F
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.utils.translation import gettext as _
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import (
    Response,
)  # For returning HTTP responses in REST framework

from .. import documents as stock_documents
from ..models import (
    Admin,
    GoodsReceipt,
    GoodsReceiptLine,
    Item,
    StockDocument,
)
from ..permissions import is_warehouse_manager
from .document_pages import RECENT_DOCUMENTS, announce, document_from_form
from .transfers import site_accounts

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Goods receipts (stock in, against a supplier purchase order) and item labels
# ---------------------------------------------------------------------------


def _parse_receipt_lines(raw_lines):
    """Normalise the submitted receipt lines into ``[(sku, quantity, description)]``.

    A repeated SKU in one delivery is summed, so the same item cannot be booked
    twice from a double click.
    """
    merged = {}
    for raw in raw_lines:
        if not isinstance(raw, dict):
            raise ValueError(_("Each receipt line must be an item and a quantity."))
        sku = str(raw.get("sku") or "").strip()
        if not sku:
            continue
        if sku.isdigit() and not Item.objects.filter(sku=sku).exists():
            member = stock_documents.resolve_internal_code(sku)
            if member is not None:
                sku = member.sku
        try:
            quantity = int(str(raw.get("quantity")))
        except (TypeError, ValueError):
            raise ValueError(_("The received quantity must be a whole number."))
        if quantity <= 0:
            raise ValueError(_("The received quantity must be greater than zero."))
        description = str(raw.get("description") or "").strip()
        if sku in merged:
            merged[sku] = (merged[sku][0] + quantity, merged[sku][1] or description)
        else:
            merged[sku] = (quantity, description)
    if not merged:
        raise ValueError(_("Add at least one item to receive."))
    return [(sku, qty, desc) for sku, (qty, desc) in merged.items()]


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def receive_from_supplier(request):
    """Book a supplier delivery: stock in against a PO number."""
    if Admin.is_edit_locked():
        return Response(
            {"detail": _("Receiving is disabled while the warehouse is being maintained.")},
            status=status.HTTP_403_FORBIDDEN,
        )

    po_number = str(request.data.get("po_number") or "").strip()
    if not po_number:
        return Response(
            {"detail": _("Enter the purchase order number.")},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        lines = _parse_receipt_lines(request.data.get("lines") or [])
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    supplier = str(request.data.get("supplier") or "").strip()
    notes = str(request.data.get("notes") or "").strip()

    try:
        with transaction.atomic():
            receipt = GoodsReceipt.objects.create(
                po_number=po_number,
                supplier=supplier,
                notes=notes,
                received_by=request.user if request.user.is_authenticated else None,
            )
            booked = []
            for sku, quantity, description in lines:
                item, created = Item.objects.get_or_create(
                    sku=sku,
                    defaults={
                        "description": description or sku,
                        "retail_price": Decimal("0.00"),
                        "quantity": 0,
                    },
                )
                if created and not description:
                    item.description = sku
                Item.objects.filter(pk=item.pk).update(
                    quantity=F("quantity") + quantity
                )
                item.refresh_from_db()
                GoodsReceiptLine.objects.create(
                    receipt=receipt,
                    item=item,
                    sku=sku,
                    description=description or item.description,
                    quantity=quantity,
                    unit=item.unit,
                )
                booked.append(
                    {
                        "sku": sku,
                        "description": description or item.description,
                        "quantity": quantity,
                        "warehouse_quantity": item.quantity,
                        "created_item": created,
                    }
                )
    except Exception:
        logger.error("Unexpected error while receiving a delivery.", exc_info=True)
        return Response(
            {"detail": _("An unexpected error occurred while receiving the delivery.")},
            status=status.HTTP_400_BAD_REQUEST,
        )

    return Response(
        {
            "id": receipt.id,
            "po_number": receipt.po_number,
            "supplier": receipt.supplier,
            "received_at": receipt.received_at,
            "received_by": request.user.username,
            "lines": booked,
            "total_quantity": sum(line["quantity"] for line in booked),
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_receipts(request):
    """Recent goods receipts, newest first, with their lines."""
    try:
        limit = min(max(int(request.query_params.get("limit", 25)), 1), 200)
    except (TypeError, ValueError):
        limit = 25

    queryset = GoodsReceipt.objects.prefetch_related("lines").all()
    po_number = (request.query_params.get("po") or "").strip()
    if po_number:
        queryset = queryset.filter(po_number__icontains=po_number)

    rows = []
    for receipt in queryset[:limit]:
        rows.append(
            {
                "id": receipt.id,
                "po_number": receipt.po_number,
                "supplier": receipt.supplier,
                "received_at": receipt.received_at,
                "received_by": receipt.received_by.username
                if receipt.received_by
                else "",
                "notes": receipt.notes,
                "total_quantity": receipt.total_quantity,
                "lines": [
                    {
                        "sku": line.sku,
                        "description": line.description,
                        "quantity": line.quantity,
                    }
                    for line in receipt.lines.all()
                ],
            }
        )
    return Response({"results": rows, "count": len(rows)})

@ensure_csrf_cookie
@login_required
def receipts_page(request):
    """Additions: PO receipt, transfer in from another project, site return."""
    if request.method == "POST":
        doc_type = request.POST.get("doc_type", "")
        if doc_type not in StockDocument.IN_TYPES:
            messages.error(request, _("Choose the type of addition."))
            return HttpResponseRedirect("/receipts/")
        try:
            document = document_from_form(request, doc_type)
        except stock_documents.DocumentError as exc:
            messages.error(request, str(exc))
            return HttpResponseRedirect("/receipts/")
        announce(request, document)
        return HttpResponseRedirect("/receipts/")

    return render(
        request,
        "pages/receipts.html",
        {
            "sites": site_accounts(),
            "unit_options": stock_documents.COMMON_UNITS,
            "types": [
                (value, _(label))
                for value, label in StockDocument.TYPE_CHOICES
                if value in StockDocument.IN_TYPES
            ],
            "recent": StockDocument.objects.select_related("site", "created_by")
            .prefetch_related("lines")
            .filter(direction=StockDocument.IN)[:RECENT_DOCUMENTS],
            "pending": StockDocument.objects.select_related("site", "created_by")
            .prefetch_related("lines")
            .filter(status=StockDocument.PENDING, direction=StockDocument.IN),
            "pending_title": _("Additions waiting for approval"),
            "back_url": "/receipts/",
            "is_manager": is_warehouse_manager(request.user),
            "form_title": _("New addition"),
            "form_hint": _("Stock goes IN"),
            "submit_label": _("Save the addition"),
            "site_types": StockDocument.SITE_RETURN,
            "action_url": "/receipts/",
        },
    )
