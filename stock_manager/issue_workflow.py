"""The site-request → issue-permit workflow.

The warehouse does not issue one item because one phone asked for it. It
collects the requests of a site, decides what will actually be released, writes
one numbered issue permit (إذن صرف), prints its three copies, and hands the
Oracle copy to the clerk who posts the deduction.

The database already had the primitives: ``TransferItem`` is a site's requested
line, ``StockDocument`` is the numbered issue permit, and
``StockDocumentLine`` is what the permit released. This module is the single
rule set that turns the first into the second, so the dashboard, the web page
and the API cannot each invent a different release path.
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from . import documents as stock_documents
from .audit import record
from .models import AuditEvent, StockDocument, TransferItem


class IssuePermitError(ValueError):
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
        raise IssuePermitError(
            _("Request %(code)s: the issued quantity must be a whole number.")
            % {"code": request.item.sku}
        )
    if quantity < 0:
        raise IssuePermitError(
            _("Request %(code)s: the issued quantity cannot be negative.")
            % {"code": request.item.sku}
        )
    if quantity > request.quantity:
        raise IssuePermitError(
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


def create_permit(
    *,
    site,
    request_ids,
    issued,
    permit_no,
    user,
    document_date=None,
    location="",
    engineer_name="",
    notes="",
):
    """Turn selected site requests into one numbered, approved issue permit.

    ``issued`` maps request ids to the quantity actually released. A line may
    be released in part; the remainder stays on the site's request queue for a
    later permit. A line released in full is removed from the queue. The stock
    movement and the queue update happen in one transaction, so a failed
    permit cannot consume a request without a document.
    """
    if site is None:
        raise IssuePermitError(_("Choose the site for this issue permit."))
    permit_no = (permit_no or "").strip()
    if not permit_no:
        raise IssuePermitError(_("Enter the issue permit number."))
    if len(permit_no) > 80:
        raise IssuePermitError(_("The issue permit number is too long."))

    ids = []
    for raw in request_ids or []:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            ids.append(int(text))
        except (TypeError, ValueError):
            raise IssuePermitError(_("The selected request is not valid."))
    ids = list(dict.fromkeys(ids))
    if not ids:
        raise IssuePermitError(_("Choose at least one site request."))

    with transaction.atomic():
        requests = list(
            TransferItem.objects.select_for_update()
            .select_related("item", "shop_user")
            .filter(pk__in=ids, ordered=True)
        )
        found = {request.pk for request in requests}
        if found != set(ids):
            raise IssuePermitError(
                _("One of the requests changed while you were reading it. Reload and try again.")
            )
        if any(request.shop_user_id != site.pk for request in requests):
            raise IssuePermitError(
                _("An issue permit belongs to one site; choose requests from one site only.")
            )

        released = []
        quantities = {}
        for request in requests:
            if request.item is None:
                raise IssuePermitError(
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
            raise IssuePermitError(_("The permit has no issued quantity."))

        document = stock_documents.create_document(
            doc_type=StockDocument.SITE_ISSUE,
            lines=released,
            site=site,
            external_ref=permit_no,
            notes=notes,
            created_by=user,
            source=StockDocument.WEB,
            pending=False,
            document_date=document_date or timezone.localdate(),
            location=(location or "").strip() or site.username,
            engineer_name=engineer_name,
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


def mark_oracle_posted(document, *, user, note=""):
    """Record the hand-off to the Oracle clerk without moving stock again."""
    with transaction.atomic():
        locked = StockDocument.objects.select_for_update().get(pk=document.pk)
        if locked.direction != StockDocument.OUT:
            raise IssuePermitError(_("Only an issue permit is posted to Oracle."))
        if locked.status != StockDocument.APPROVED:
            raise IssuePermitError(
                _("Only an approved issue permit can be posted to Oracle.")
            )
        if locked.oracle_posted_at is not None:
            raise IssuePermitError(
                _("This issue permit was already posted to Oracle.")
            )
        locked.oracle_posted_at = timezone.now()
        locked.oracle_posted_by = user
        locked.oracle_note = (note or "").strip()[:200]
        locked.save(
            update_fields=["oracle_posted_at", "oracle_posted_by", "oracle_note"]
        )
    record(
        user=user,
        action=AuditEvent.DOCUMENT_ORACLE_POSTED,
        entity="stock_document",
        entity_id=locked.pk,
        description=f"{locked.number} · {locked.external_ref or ''}".strip(" ·"),
    )
    return locked


def oracle_queue(limit=50):
    """Approved issues whose Oracle copy has not been posted yet."""
    return (
        StockDocument.objects.select_related("site", "created_by")
        .prefetch_related("lines__item__internal_group")
        .filter(
            direction=StockDocument.OUT,
            status=StockDocument.APPROVED,
            oracle_posted_at__isnull=True,
        )
        .order_by("-applied_at", "-id")[:limit]
    )
