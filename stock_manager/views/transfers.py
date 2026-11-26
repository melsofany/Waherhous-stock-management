"""Moving stock to the sites: the transfer API, the request and dispatch
endpoints, and the site list.
"""

import logging

from django.contrib.auth.models import User  # For accessing the User model
from django.db import transaction
from django.db.models import F, Q
from rest_framework import status, viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import (
    Response,
)  # For returning HTTP responses in REST framework

from email_service.email import SendEmail

from .. import audit
from ..models import (
    Admin,
    AuditEvent,
    Item,
    ShopItem,
    TransferItem,
)
from ..ordering import apply_ordering
from ..pagination import CustomPagination
from ..permissions import is_warehouse_manager
from ..serializers import TransferItemSerializer

logger = logging.getLogger(__name__)


class TransferItemViewSet(viewsets.ReadOnlyModelViewSet):
    # Read-only: transfers are created via /api/transfer/ and dispatched or
    # cancelled via /api/complete-transfer/, so this collection exposes no
    # write verbs.
    queryset = TransferItem.objects.all()
    serializer_class = TransferItemSerializer
    lookup_field = "item__sku"
    permission_classes = [IsAuthenticated]
    pagination_class = CustomPagination

    def get_queryset(self):
        user = self.request.user
        if is_warehouse_manager(user):
            queryset = TransferItem.objects.filter(ordered=True)
        else:
            queryset = TransferItem.objects.filter(shop_user=user)
        search_query = self.request.query_params.get("search", None)
        if search_query:
            queryset = queryset.filter(
                Q(item__description__icontains=search_query)
                | Q(item__sku__icontains=search_query)
            )  # 🔍 Search filter
        return apply_ordering(
            queryset,
            self.request.query_params.get("ordering"),
            natural_key=lambda row: row.item.sku,
        )

def site_accounts():
    """The construction sites stock can be issued to (they are user accounts)."""
    return list(
        User.objects.filter(groups__name="shop_users", is_active=True)
        .order_by("username")
        .values("id", "username")
    )

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def transfer_item(request):
    if Admin.is_edit_locked():
        logger.debug("Transfer attempt while update mode is enabled.")
        return Response(
            {
                "detail": "Transfers are disabled as the warehouse is being maintained. Please try again later."
            },
            status=status.HTTP_403_FORBIDDEN,
        )
    if not request.user.groups.filter(name="shop_users").exists():
        logger.debug("Permission denied: user is not in shop_users group.")
        return Response(
            {"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN
        )
    sku = request.data.get("sku")
    # Accept the quantity as either a string or a JSON number.
    try:
        transfer_quantity = int(str(request.data.get("transfer_quantity")))
    except (TypeError, ValueError):
        logger.debug("Invalid transfer quantity: not an integer.")
        return Response(
            {"detail": "Transfer quantity must be an integer."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if transfer_quantity <= 0:
        logger.debug("Invalid transfer quantity: less than or equal to zero.")
        return Response(
            {"detail": "Transfer quantity must be greater than zero."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        item = Item.objects.get(sku=sku)
        transfer_to_shop(item, request.user, transfer_quantity)
    except Item.DoesNotExist:
        logger.debug("Item not found: sku=%s", sku)
        return Response({"detail": "Item not found."}, status=status.HTTP_404_NOT_FOUND)
    except (ValueError, LookupError) as e:
        # Intentional, user-facing business-rule messages.
        logger.debug("Transfer rejected: %s", str(e))
        return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception:
        # Do not leak unexpected internal error detail to the client.
        logger.error("Unexpected error during transfer.", exc_info=True)
        return Response(
            {"detail": "An unexpected error occurred while processing the transfer."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return Response({"detail": "Transfer successful."}, status=status.HTTP_200_OK)


def transfer_to_shop(
    item, shop_user, transfer_quantity, complete=False, cancel=False, manager=False
):
    if Admin.is_edit_locked() and not manager:
        raise ValueError(
            "Transfers are disabled as the warehouse is being maintained. Please try again later."
        )
    if cancel:
        transfer_item = TransferItem.objects.get(
            item=item, shop_user=shop_user
        ).delete()
    else:
        transfer_quantity = int(transfer_quantity)
        if transfer_quantity <= 0:
            raise ValueError("Transfer quantity must be greater than zero.")
        if item.quantity < transfer_quantity:
            raise ValueError("Not enough stock to transfer")
        if not complete:
            xfer_item, created = TransferItem.objects.get_or_create(
                shop_user=shop_user, item=item
            )
            if xfer_item.ordered:
                raise LookupError(
                    "This item has already been ordered and is awaiting dispatch. Please contact the warehouse manager if you wish to amend your order."
                )
            xfer_item.quantity = transfer_quantity
            xfer_item.save()
        else:
            # Dispatch: move stock from the warehouse to the shop. The
            # warehouse decrement is a single conditional UPDATE, so two
            # concurrent dispatches cannot oversell (guards against a
            # check-then-update race).
            shop_user = User.objects.get(id=shop_user)
            with transaction.atomic():
                rows = Item.objects.filter(
                    pk=item.pk, quantity__gte=transfer_quantity
                ).update(quantity=F("quantity") - transfer_quantity)
                if not rows:
                    raise ValueError("Not enough stock to transfer")
                shop_item, _ = ShopItem.objects.get_or_create(
                    item=item, shop_user=shop_user
                )
                ShopItem.objects.filter(pk=shop_item.pk).update(
                    quantity=F("quantity") + transfer_quantity
                )
                TransferItem.objects.filter(item=item, shop_user=shop_user).delete()
            item.refresh_from_db()


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def submit_transfer_request(request):
    try:
        if not Admin.is_edit_locked():
            queryset = TransferItem.objects.filter(
                shop_user=request.user.id, ordered=False
            )
            if queryset.exists():
                # send notification email
                SendEmail().compose(
                    records=list(
                        queryset.values(
                            "id",
                            "item__sku",
                            "item__description",
                            "item__retail_price",
                            "quantity",
                        )
                    ),
                    user=request.user,
                    notification_type=SendEmail.EmailType.STOCK_TRANSFER,
                )
                # update records ordered status to True
                queryset.update(ordered=True)
            else:
                return Response(
                    {"detail": "There were no outstanding items to request!"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            return Response(
                {
                    "detail": "Transfers cannot proceed while the warehouse is under maintenance. Please try again later."
                },
                status=status.HTTP_403_FORBIDDEN,
            )
    except Exception:
        # Do not leak unexpected internal error detail to the client.
        logger.error("Unexpected error while submitting transfer.", exc_info=True)
        return Response(
            {"detail": "An unexpected error occurred while submitting the transfer request."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    audit.record(
        user=request.user,
        action=AuditEvent.TRANSFER_SENT,
        entity="transfer",
        description=request.user.get_username(),
    )
    return Response(
        {"detail": "Transfer successfully submitted."}, status=status.HTTP_200_OK
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def complete_transfer(request):
    sku = request.data.get("sku")
    quantity = request.data.get("quantity")
    shop_user_id = request.data.get("shop_user_id")
    cancel = True if request.data.get("cancel") == "true" else False
    try:
        shop_user = User.objects.get(username=shop_user_id)
        shop_user_id = shop_user.id
    except User.DoesNotExist:
        logger.debug("User does not exist!")
        return Response(
            {"detail": "Shop user not found."}, status=status.HTTP_400_BAD_REQUEST
        )
    is_manager = is_warehouse_manager(request.user)
    if not is_manager:
        # Non-managers may only cancel, and only their own transfers. The
        # shop_user_id in the request body must not let one shop user act on
        # another's pending transfers.
        if not cancel:
            return Response(
                {"detail": "Permission denied. User is not in managers group."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if shop_user != request.user:
            return Response(
                {"detail": "Permission denied. You may only cancel your own transfers."},
                status=status.HTTP_403_FORBIDDEN,
            )
    if not cancel:
        # Dispatch requires a valid positive quantity; a negative value would
        # otherwise inflate warehouse stock.
        try:
            quantity = int(str(quantity))
        except (TypeError, ValueError):
            return Response(
                {"detail": "Quantity must be a positive integer."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if quantity <= 0:
            return Response(
                {"detail": "Quantity must be a positive integer."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    try:
        item = Item.objects.get(sku=sku)
        transfer_to_shop(
            manager=is_manager,
            item=item,
            shop_user=shop_user_id,
            transfer_quantity=quantity,
            complete=True,
            cancel=cancel,
        )
    except Item.DoesNotExist:
        logger.debug("Item not found: sku=%s", sku)
        return Response({"detail": "Item not found."}, status=status.HTTP_404_NOT_FOUND)
    except (ValueError, LookupError) as e:
        # Intentional, user-facing business-rule messages.
        logger.debug("Transfer action rejected: %s", str(e))
        return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception:
        # Do not leak unexpected internal error detail to the client.
        logger.error("Unexpected error during transfer action.", exc_info=True)
        return Response(
            {"detail": "An unexpected error occurred while processing the transfer action."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    audit.record(
        user=request.user,
        action=AuditEvent.TRANSFER_COMPLETED,
        entity="transfer",
        description=request.user.get_username(),
    )
    return Response(
        {"detail": "Transfer action successful."}, status=status.HTTP_200_OK
    )

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_sites(request):
    """The construction sites stock can be issued to (they are user accounts)."""
    return Response({"results": site_accounts()})
