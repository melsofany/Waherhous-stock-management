"""Excel export and import of the stock list."""

import logging
from datetime import datetime
from io import BytesIO

from django.http import FileResponse
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import (
    Response,
)  # For returning HTTP responses in REST framework

from .. import audit
from ..balances import workbook_bytes as balances_workbook_bytes
from ..models import (
    Admin,
    AuditEvent,
)
from ..permissions import is_warehouse_manager
from ..utils import SpreadsheetTools

logger = logging.getLogger(__name__)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def export_data_excel(request):
    """
    Export data as Excel for download.
    """
    if not (
        is_warehouse_manager(request.user)
        or request.user.groups.filter(name="shop_users").exists()
    ):
        logger.debug("Permission denied: user is not in shop_users or managers group.")
        return Response(
            {"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN
        )
    audit.record(
        user=request.user,
        action=AuditEvent.DATA_EXPORTED,
        entity="spreadsheet",
        description=request.user.get_username(),
    )
    return SpreadsheetTools(request).generate_excel_response()


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def export_balances_excel(request):
    """Download the warehouse balances as a formatted, printable sheet."""
    if not (
        is_warehouse_manager(request.user)
        or request.user.groups.filter(name="shop_users").exists()
    ):
        logger.debug("Permission denied: user is not in shop_users or managers group.")
        return Response(
            {"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN
        )
    audit.record(
        user=request.user,
        action=AuditEvent.DATA_EXPORTED,
        entity="balances",
        description=request.user.get_username(),
    )
    filename = datetime.now().strftime("SSM_BALANCES_%d%b%Y_%H%M%S.xlsx")
    return FileResponse(
        BytesIO(balances_workbook_bytes()),
        as_attachment=True,
        filename=filename,
        content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def import_data_excel(request):
    """
    Import data from an uploaded Excel file.
    """
    # Only allow managers to perform the upload.
    if not is_warehouse_manager(request.user):
        logger.debug("Permission denied: user is not in managers group.")
        return Response(
            {"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN
        )
    if not Admin.is_allow_updoads():
        logger.debug("Attempted upload when disabled.")
        return Response(
            {"detail": "Uploads are disabled in the app configuration."}, status=400
        )
    response = SpreadsheetTools(request).handle_excel_upload()
    if getattr(response, "status_code", 500) < 400:
        audit.record(
            user=request.user,
            action=AuditEvent.DATA_IMPORTED,
            entity="spreadsheet",
            description=request.user.get_username(),
        )
    return response
