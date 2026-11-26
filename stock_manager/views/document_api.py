"""The stock documents API used by the web and the phone app."""


from django.contrib.auth.models import User  # For accessing the User model
from django.utils.translation import gettext as _
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import (
    Response,
)  # For returning HTTP responses in REST framework

from .. import documents as stock_documents
from ..models import (
    StockDocument,
)
from ..permissions import is_warehouse_manager


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def stock_documents_api(request):
    """The Android app's entry point: a submission that moves nothing yet."""
    if request.method == "POST":
        try:
            document = stock_documents.create_document(
                doc_type=str(request.data.get("doc_type") or ""),
                lines=request.data.get("lines") or [],
                site=User.objects.filter(
                    username=request.data.get("site") or "", groups__name="shop_users"
                ).first(),
                counterparty=request.data.get("counterparty", ""),
                reference=request.data.get("reference", ""),
                external_ref=request.data.get("external_ref", ""),
                notes=request.data.get("notes", ""),
                created_by=request.user,
                source=str(request.data.get("source") or StockDocument.MOBILE),
                pending=not is_warehouse_manager(request.user),
            )
        except stock_documents.DocumentError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(_serialize_document(document), status=status.HTTP_201_CREATED)

    queryset = StockDocument.objects.prefetch_related("lines")
    status_filter = request.query_params.get("status")
    if status_filter in dict(StockDocument.STATUS_CHOICES):
        queryset = queryset.filter(status=status_filter)
    return Response(
        {
            "results": [_serialize_document(document) for document in queryset[:100]],
            "count": queryset.count(),
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def decide_stock_document_api(request, pk):
    if not is_warehouse_manager(request.user):
        return Response(
            {"detail": _("Only a warehouse manager can approve documents.")},
            status=status.HTTP_403_FORBIDDEN,
        )
    document = StockDocument.objects.filter(pk=pk).first()
    if document is None:
        return Response({"detail": _("Document not found.")}, status=status.HTTP_404_NOT_FOUND)
    action = request.data.get("action")
    try:
        if action == "approve":
            document = stock_documents.approve_document(
                document, user=request.user, note=request.data.get("note", "")
            )
        elif action == "reject":
            document = stock_documents.reject_document(
                document, user=request.user, note=request.data.get("note", "")
            )
        else:
            return Response(
                {"detail": _("Unknown action.")}, status=status.HTTP_400_BAD_REQUEST
            )
    except stock_documents.DocumentError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(_serialize_document(document))


def _serialize_document(document):
    return {
        "id": document.id,
        "number": document.number,
        "direction": document.direction,
        "doc_type": document.doc_type,
        "status": document.status,
        "site": document.site.username if document.site_id else "",
        "counterparty": document.counterparty,
        "reference": document.reference,
        "external_ref": document.external_ref,
        "notes": document.notes,
        "created_by": document.created_by.username if document.created_by else "",
        "created_at": document.created_at,
        "applied_at": document.applied_at,
        "total_quantity": document.total_quantity,
        "lines": [
            {
                "sku": line.sku,
                "description": line.description,
                "quantity": line.quantity,
            }
            for line in document.lines.all()
        ],
    }


