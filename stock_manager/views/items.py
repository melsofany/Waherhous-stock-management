"""ItemViewSet and ShopItemViewSet: the stock table API.

List, search, grouping, create, update and soft delete of items.
"""


from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from rest_framework import status, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import (
    Response,
)  # For returning HTTP responses in REST framework

from .. import audit
from ..models import (
    AuditEvent,
    GoodsReceiptLine,
    Item,
    ShopItem,
    StockDocumentLine,
    TransferItem,
)
from ..ordering import apply_ordering
from ..pagination import CustomPagination
from ..permissions import is_warehouse_manager
from ..serializers import ItemSerializer, ShopItemSerializer


def _rename_oracle_code(item, new_sku):
    """Move every reference before removing the old primary key.

    The document and receipt lines keep their ``sku`` snapshot: a past line
    still says which Oracle code was written on its paper. Only the catalogue
    identity and its live references move.
    """
    old_sku = item.sku
    replacement = Item.objects.create(
        sku=new_sku,
        description=item.description,
        retail_price=item.retail_price,
        quantity=item.quantity,
        unit=item.unit,
        sub_inventory=item.sub_inventory,
        arabic_name=item.arabic_name,
        internal_group_id=item.internal_group_id,
        is_active=item.is_active,
    )
    StockDocumentLine.objects.filter(item_id=old_sku).update(item_id=new_sku)
    GoodsReceiptLine.objects.filter(item_id=old_sku).update(item_id=new_sku)
    ShopItem.objects.filter(item_id=old_sku).update(item_id=new_sku)
    TransferItem.objects.filter(item_id=old_sku).update(item_id=new_sku)
    item.delete()
    return replacement


# API View
class ItemViewSet(viewsets.ModelViewSet):
    queryset = Item.objects.filter(is_active=True)
    serializer_class = ItemSerializer
    lookup_field = "sku"
    permission_classes = [IsAuthenticated]
    pagination_class = CustomPagination

    @staticmethod
    def with_group_totals(queryset):
        """Attach the summed quantity of the internal code to every row.

        The group total is read from the member rows, never stored, so a
        quantity cannot be counted twice.
        """
        totals = (
            Item.objects.filter(
                internal_group=OuterRef("internal_group"), is_active=True
            )
            .values("internal_group")
            .annotate(total=Sum("quantity"))
            .values("total")
        )
        sizes = (
            Item.objects.filter(
                internal_group=OuterRef("internal_group"), is_active=True
            )
            .values("internal_group")
            .annotate(total=Count("pk"))
            .values("total")
        )
        return queryset.select_related("internal_group").annotate(
            group_total=Coalesce(Subquery(totals), 0),
            group_size=Coalesce(Subquery(sizes), 0),
        )

    def get_queryset(self):
        queryset = self.with_group_totals(Item.objects.filter(is_active=True))
        search_query = self.request.query_params.get("search", None)
        if search_query:
            queryset = queryset.filter(
                Q(description__icontains=search_query)
                | Q(sku__icontains=search_query)
                | Q(internal_group__code__icontains=search_query)
            )  # 🔍 Search filter
        return apply_ordering(
            queryset,
            self.request.query_params.get("ordering"),
            natural_key=lambda row: row.sku,
        )

    def list(self, request, *args, **kwargs):
        """The table can ask for pages of *internal codes*, not of rows.

        A group split across a page boundary would print its code and total
        twice, so the stock table paginates by group when it asks to. Other
        callers (the phone, a script) keep the flat page they always had.
        """
        if request.query_params.get("grouped") != "1":
            return super().list(request, *args, **kwargs)

        items = list(self.get_queryset().order_by("internal_group__code", "sku"))
        groups, order = {}, []
        for item in items:
            key = item.internal_group_id or f"sku:{item.sku}"
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(item)

        paginator = self.pagination_class()
        page_size = paginator.get_page_size(request)
        pager = Paginator(order, page_size)
        page = pager.get_page(request.query_params.get("page") or 1)
        selected = [item for key in page.object_list for item in groups[key]]

        before = sum(len(groups[key]) for key in order[: page.start_index() - 1])
        return Response(
            {
                "results": self.get_serializer(selected, many=True).data,
                "count": len(items),
                "current_page": page.number,
                "total_pages": pager.num_pages,
                "first_row": before + 1 if selected else 0,
                "last_row": before + len(selected),
                "previous": None,
                "next": None,
                "previous_page_number": page.previous_page_number()
                if page.has_previous()
                else None,
                "next_page_number": page.next_page_number()
                if page.has_next()
                else None,
            }
        )

    def create(self, request, *args, **kwargs):
        if not is_warehouse_manager(request.user):
            return Response(
                {"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN
            )
        sku = request.data.get("sku")
        if sku:
            try:
                item = Item.objects.get(sku=sku)
                if not item.is_active:
                    # Reactivate and update fields
                    serializer = ItemSerializer(item, data=request.data, partial=True)
                    if serializer.is_valid():
                        serializer.save(is_active=True)
                        audit.record(
                            user=request.user,
                            action=AuditEvent.ITEM_CREATED,
                            entity="item",
                            entity_id=sku,
                            description=f"{sku} · {serializer.instance.description}",
                        )
                        return Response(serializer.data, status=status.HTTP_200_OK)
                    else:
                        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
                else:
                    return Response({"detail": "Item with this SKU already exists."}, status=status.HTTP_400_BAD_REQUEST)
            except Item.DoesNotExist:
                pass
        response = super().create(request, *args, **kwargs)
        if response.status_code in (200, 201):
            item = response.data
            audit.record(
                user=request.user,
                action=AuditEvent.ITEM_CREATED,
                entity="item",
                entity_id=item.get("sku"),
                description=f"{item.get('sku')} · {item.get('description')}",
            )
        return response

    def update(self, request, *args, **kwargs):
        if not is_warehouse_manager(request.user):
            return Response(
                {"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN
            )
        old_sku = kwargs.get("sku")
        try:
            item = Item.objects.get(sku=old_sku)
        except Item.DoesNotExist:
            return Response(
                {"error": "Item not found."}, status=status.HTTP_404_NOT_FOUND
            )

        serializer = ItemSerializer(item, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        changed = sorted(serializer.validated_data.keys())
        new_sku = serializer.validated_data.get("sku", item.sku)
        try:
            with transaction.atomic():
                if new_sku != item.sku:
                    item = _rename_oracle_code(item, new_sku)
                for field, value in serializer.validated_data.items():
                    if field != "sku":
                        setattr(item, field, value)
                if not item.is_active:
                    item.is_active = True
                    if "is_active" not in changed:
                        changed.append("is_active")
                item.save()
        except IntegrityError:
            return Response(
                {"sku": ["An item with this Oracle code already exists."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        audit.record(
            user=request.user,
            action=AuditEvent.ITEM_UPDATED,
            entity="item",
            entity_id=item.sku,
            description=f"{old_sku} → {item.sku} · " + ", ".join(sorted(changed)),
        )
        return Response(ItemSerializer(item).data, status=status.HTTP_200_OK)

    def destroy(self, request, *args, **kwargs):
        if not is_warehouse_manager(request.user):
            return Response(
                {"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN
            )
        sku = kwargs.get("sku")
        try:
            item = Item.objects.get(sku=sku)
            item.is_active = False
            item.save()
            # A soft delete: the row stays for the documents that reference it.
            audit.record(
                user=request.user,
                action=AuditEvent.ITEM_DELETED,
                entity="item",
                entity_id=sku,
                description=f"{sku} · {item.description}",
            )
            return Response(status=status.HTTP_204_NO_CONTENT)
        except Item.DoesNotExist:
            return Response({"error": "Item not found."}, status=status.HTTP_404_NOT_FOUND)


class ShopItemViewSet(viewsets.ReadOnlyModelViewSet):
    # Read-only: shop stock is only ever moved via the transfer/complete
    # endpoints, so this collection exposes no write verbs.
    queryset = ShopItem.objects.all()
    serializer_class = ShopItemSerializer
    lookup_field = "item__sku"
    permission_classes = [IsAuthenticated]
    pagination_class = CustomPagination

    def get_queryset(self):
        queryset = ShopItem.objects.filter(shop_user=self.request.user).exclude(item=None)
        search_query = self.request.query_params.get("search", None)
        if search_query:
            queryset = queryset.filter(
                Q(item__description__icontains=search_query)
                | Q(item__sku__icontains=search_query)
            )  # 🔍 Search filter
        return apply_ordering(
            queryset,
            self.request.query_params.get("ordering"),
            natural_key=lambda row: row.item.sku if row.item else "",
        )
