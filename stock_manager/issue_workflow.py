"""Releasing a site's request: the moment stock leaves the warehouse.

A request reaches the warehouse on paper — a numbered page of the company's
request book, signed by the engineer and carried by a representative. Approving
it deducts the warehouse immediately.

A request raised from the dashboard sits in ``TransferItem`` until the warehouse
releases it against that paper; this module turns those rows into one approved
site-issue :class:`~stock_manager.models.StockDocument` that carries the request
number. A request typed or photographed directly (the issues page, the Android
app) is a document already and needs nothing from here.

What happens *after* the release — the draft spending sheet, the handwritten
issue vouchers and the Oracle hand-off — lives in
:mod:`stock_manager.spending`.
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from . import documents as stock_documents
from .models import StockDocument, TransferItem

MAX_REQUEST_NUMBER_LENGTH = 80


class RequestError(ValueError):
    """A refusal the warehouse manager can act on."""


def pending_requests():
    """Submitted site requests, oldest site first, one row per requested item."""
    return (
        TransferItem.objects.select_related(
            "shop_user", "item", "item__internal_group"
        )
        .filter(ordered=True, item__isnull=False)
        .order_by("shop_user__username", "item__sku")
    )


def pending_groups():
    """The same requests grouped by site, with the per-site totals."""
    groups = {}
    for request in pending_requests():
        group = groups.setdefault(
            request.shop_user_id,
            {
                "site": request.shop_user,
                "requests": [],
                "total_quantity": 0,
                "item_count": 0,
            },
        )
        group["requests"].append(request)
        group["total_quantity"] += request.quantity
        group["item_count"] += 1
    return sorted(groups.values(), key=lambda group: group["site"].username)


def _issued_quantity(raw, request):
    if raw is None or str(raw).strip() == "":
        return request.quantity
    try:
        quantity = int(str(raw).strip())
    except (TypeError, ValueError):
        raise RequestError(
            _("Request %(code)s: the issued quantity must be a whole number.")
            % {"code": request.item.sku}
        )
    if quantity < 0:
        raise RequestError(
            _("Request %(code)s: the issued quantity cannot be negative.")
            % {"code": request.item.sku}
        )
    if quantity > request.quantity:
        raise RequestError(
            _(
                "Request %(code)s: you cannot issue %(issued)s when the site asked for %(requested)s."
            )
            % {
                "code": request.item.sku,
                "issued": quantity,
                "requested": request.quantity,
            }
        )
    return quantity


def release_requests(
    *,
    site,
    request_ids,
    issued,
    request_no,
    user,
    document_date=None,
    location="",
    engineer_name="",
    representative="",
    notes="",
):
    """Approve one paper request and deduct its quantities from the warehouse.

    ``issued`` maps request ids to the quantity actually released. A line may
    be released in part; the remainder stays on the site's queue for a later
    paper. A line released in full is removed from the queue. The stock
    movement and the queue update happen in one transaction, so a failed
    release cannot consume a request without a document.
    """
    if site is None:
        raise RequestError(_("Choose the site for this request."))
    request_no = (request_no or "").strip()
    if not request_no:
        raise RequestError(_("Enter the request number printed on the paper."))
    if len(request_no) > MAX_REQUEST_NUMBER_LENGTH:
        raise RequestError(_("The request number is too long."))

    ids = []
    for raw in request_ids or []:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            ids.append(int(text))
        except (TypeError, ValueError):
            raise RequestError(_("The selected request is not valid."))
    ids = list(dict.fromkeys(ids))
    if not ids:
        raise RequestError(_("Choose at least one site request."))

    booked = StockDocument.objects.filter(external_ref=request_no).first()
    if booked is not None:
        raise RequestError(
            _(
                "Number %(number)s is already booked as %(document)s; nothing was deducted twice."
            )
            % {"number": request_no, "document": booked.number}
        )

    with transaction.atomic():
        requests = list(
            TransferItem.objects.select_for_update()
            .select_related("item", "shop_user")
            .filter(pk__in=ids, ordered=True)
        )
        found = {request.pk for request in requests}
        if found != set(ids):
            raise RequestError(
                _("One of the requests changed while you were reading it. Reload and try again.")
            )
        if any(request.shop_user_id != site.pk for request in requests):
            raise RequestError(
                _("A paper request belongs to one site; choose lines from one site only.")
            )

        released = []
        quantities = {}
        for request in requests:
            if request.item is None:
                raise RequestError(
                    _("Request %(code)s has no item in the catalogue.")
                    % {"code": request.item_id or "—"}
                )
            quantity = _issued_quantity(issued.get(str(request.pk)), request)
            quantities[request.pk] = quantity
            if quantity:
                released.append(
                    {
                        "sku": request.item.sku,
                        "quantity": quantity,
                        "description": request.item.description,
                        "unit": request.item.unit,
                    }
                )
        if not released:
            raise RequestError(_("Nothing was entered as issued."))

        document = stock_documents.create_document(
            doc_type=StockDocument.SITE_ISSUE,
            lines=released,
            site=site,
            external_ref=request_no,
            notes=notes,
            created_by=user,
            source=StockDocument.WEB,
            pending=False,
            document_date=document_date or timezone.localdate(),
            location=(location or "").strip() or site.username,
            engineer_name=engineer_name,
            representative=representative,
        )

        for request in requests:
            quantity = quantities[request.pk]
            if quantity == 0:
                continue
            if quantity == request.quantity:
                request.delete()
            else:
                request.quantity -= quantity
                request.save(update_fields=["quantity", "last_updated"])

    return document
