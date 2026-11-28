"""Item labels: the barcode image, the label picker and the printable item cards."""

from collections import defaultdict

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.utils.translation import gettext as _
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import (
    Response,
)  # For returning HTTP responses in REST framework

from .. import audit
from ..barcodes import BarcodeError, barcode_value, render_barcode_svg
from ..models import (
    Admin,
    AuditEvent,
    Item,
)
from ..permissions import is_warehouse_manager


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def item_barcode(request, sku):
    """The Code 128 barcode for one item, as a standalone SVG document.

    A grouped item answers with its internal code, so the same barcode covers
    every Oracle code of the article. An unknown value is still rendered as
    given, which keeps the endpoint usable for a code that is not in the
    catalogue yet.
    """
    item = Item.objects.filter(sku=sku).select_related("internal_group").first()
    try:
        svg = render_barcode_svg(barcode_value(item) if item else sku)
    except BarcodeError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    response = HttpResponse(svg, content_type="image/svg+xml")
    response["Cache-Control"] = "private, max-age=86400"
    return response


@login_required
def print_labels(request):
    """Printable label sheet: one barcode per selected item, ``copies`` each.

    The barcode carries the internal code, so one label covers every Oracle code
    of the article; the Oracle codes themselves are printed under it.
    """
    raw = request.GET.get("skus") or request.GET.get("sku") or ""
    skus = [value.strip() for value in raw.split(",") if value.strip()]
    try:
        copies = min(max(int(request.GET.get("copies", 1)), 1), 60)
    except (TypeError, ValueError):
        copies = 1

    items, errors = [], []
    if skus:
        selected = list(
            Item.objects.filter(sku__in=skus).select_related("internal_group")
        )
        found = {item.sku: item for item in selected}
        group_ids = {
            item.internal_group_id for item in selected if item.internal_group_id
        }
        codes_by_group = defaultdict(list)
        for group_id, code in (
            Item.objects.filter(internal_group_id__in=group_ids, is_active=True)
            .order_by("sku")
            .values_list("internal_group_id", "sku")
        ):
            codes_by_group[group_id].append(code)

        for sku in skus:
            item = found.get(sku)
            if item is None:
                errors.append(_("Item %(sku)s was not found.") % {"sku": sku})
                continue
            value = barcode_value(item)
            try:
                svg = render_barcode_svg(value)
            except BarcodeError as exc:
                errors.append(f"{sku}: {exc}")
                continue
            items.append(
                {
                    "item": item,
                    "internal_code": item.internal_code,
                    "oracle_codes": codes_by_group.get(item.internal_group_id)
                    or [item.sku],
                    "svg": svg,
                }
            )

    return render(
        request,
        "labels.html",
        {"items": items, "copies": copies, "copies_range": range(copies), "errors": errors},
    )


@login_required
def print_item_cards(request):
    """Printable item cards, laid out like the stock card sheet.

    ``/labels/cards/?cards=SKU:COPIES,…`` renders each selected Oracle code as
    many cards as it has copies; ``?skus=…`` is one copy each. The card shows the
    project from the App Configuration page, the internal code with its own
    barcode, every Oracle code that belongs to that internal code, the unit, the
    description and the Arabic name. The browser's print dialog prints them or
    saves them as a PDF.
    """
    requested = []  # [(sku, copies)] in the order the operator picked them
    spec = (request.GET.get("cards") or "").strip()
    if spec:
        for part in spec.split(","):
            sku, _sep, count = part.strip().rpartition(":")
            if not sku:
                continue
            try:
                copies = min(max(int(count), 0), 60)
            except ValueError:
                copies = 1
            requested.append((sku.strip(), copies))
    else:
        raw = request.GET.get("skus") or request.GET.get("sku") or ""
        requested = [(v.strip(), 1) for v in raw.split(",") if v.strip()]

    skus = [sku for sku, _copies in requested]
    cards, errors = [], []
    if skus:
        selected = list(
            Item.objects.filter(sku__in=skus).select_related("internal_group")
        )
        found = {item.sku: item for item in selected}
        group_ids = {
            item.internal_group_id for item in selected if item.internal_group_id
        }
        codes_by_group = defaultdict(list)
        for group_id, code in (
            Item.objects.filter(internal_group_id__in=group_ids, is_active=True)
            .order_by("sku")
            .values_list("internal_group_id", "sku")
        ):
            codes_by_group[group_id].append(code)

        for sku, copies in requested:
            item = found.get(sku)
            if item is None:
                errors.append(_("Item %(sku)s was not found.") % {"sku": sku})
                continue
            if copies == 0:
                continue
            try:
                svg = render_barcode_svg(barcode_value(item))
            except BarcodeError as exc:
                errors.append(f"{sku}: {exc}")
                continue
            card = {
                "item": item,
                "internal_code": item.internal_code,
                "oracle_codes": codes_by_group.get(item.internal_group_id)
                or [item.sku],
                "barcode_svg": svg,
            }
            cards.extend([card] * copies)

    settings = Admin.get_card_settings()
    return render(
        request,
        "item_cards.html",
        {
            "cards": cards,
            "errors": errors,
            "project": settings,
            "card_direction": settings["direction"],
            "card_orientation": settings["orientation"],
        },
    )


CARD_SETTINGS_URL = "/labels/card-settings/"


@login_required
def item_card_settings(request):
    """The project header, card direction and the per-item SUB/Arabic name."""
    if not is_warehouse_manager(request.user):
        messages.error(request, _("Only a warehouse manager can change the item card."))
        return HttpResponseRedirect("/labels/")

    config = Admin.objects.first()
    if config is None:
        config = Admin.objects.create(id=1)

    if request.method == "POST":
        config.project_name = (request.POST.get("project_name") or "").strip()[:120]
        config.project_code = (request.POST.get("project_code") or "").strip()[:20]
        direction = request.POST.get("card_direction") or "rtl"
        orientation = request.POST.get("card_orientation") or "portrait"
        if direction not in {"rtl", "ltr"}:
            direction = "rtl"
        if orientation not in {"portrait", "landscape"}:
            orientation = "portrait"
        config.card_direction = direction
        config.card_orientation = orientation
        config.save(
            update_fields=[
                "project_name",
                "project_code",
                "card_direction",
                "card_orientation",
            ]
        )
        audit.record(
            user=request.user,
            action=AuditEvent.SETTINGS_CHANGED,
            entity="settings",
            entity_id="item_card",
            description=f"{config.project_code or '—'} · {direction} · {orientation}",
        )

        changed = 0
        for key, item_id in request.POST.items():
            if not key.startswith("item_"):
                continue
            index = key.removeprefix("item_")
            item = Item.objects.filter(pk=item_id).first()
            if item is None:
                continue
            sub_inventory = (request.POST.get(f"sub_{index}") or "").strip()[:120]
            arabic_name = (request.POST.get(f"arabic_{index}") or "").strip()[:250]
            if (item.sub_inventory, item.arabic_name) == (sub_inventory, arabic_name):
                continue
            item.sub_inventory = sub_inventory
            item.arabic_name = arabic_name
            item.save(update_fields=["sub_inventory", "arabic_name"])
            changed += 1
            audit.record(
                user=request.user,
                action=AuditEvent.ITEM_UPDATED,
                entity="item",
                entity_id=item.pk,
                description=_("Item card fields: SUB and Arabic name"),
            )
        messages.success(
            request,
            _("The item card settings were saved. %(count)s items were updated.")
            % {"count": changed},
        )
        return HttpResponseRedirect(CARD_SETTINGS_URL)

    query = (request.GET.get("q") or "").strip()
    items = Item.objects.filter(is_active=True)
    if query:
        items = items.filter(
            Q(sku__icontains=query)
            | Q(description__icontains=query)
            | Q(sub_inventory__icontains=query)
            | Q(arabic_name__icontains=query)
        )
    return render(
        request,
        "pages/item_card_settings.html",
        {
            "config": config,
            "items": items.order_by("sku")[:300],
            "query": query,
            "directions": [
                (value, _(label))
                for value, label in Admin._meta.get_field("card_direction").choices
            ],
            "orientations": [
                (value, _(label))
                for value, label in Admin._meta.get_field("card_orientation").choices
            ],
        },
    )
