"""App configuration and the warehouse edit lock."""


from django.utils.translation import gettext as _
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import (
    Response,
)  # For returning HTTP responses in REST framework

from .. import audit
from ..models import (
    Admin,
    AppRelease,
    AuditEvent,
)
from ..permissions import is_warehouse_manager


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def set_edit_lock_status(request):
    if not is_warehouse_manager(request.user):
        return Response(
            {"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN
        )

    edit_lock_status = request.data.get("edit_lock_status", False)
    admin, created = Admin.objects.get_or_create(id=1)
    admin.edit_lock = edit_lock_status
    admin.save()
    audit.record(
        user=request.user,
        action=AuditEvent.SETTINGS_CHANGED,
        entity="settings",
        entity_id="edit_lock",
        # Read by people, so say what changed in words rather than a field dump.
        description=_("Edit lock: %(state)s")
        % {"state": _("on") if admin.edit_lock else _("off")},
    )
    return Response(
        {"edit_lock": admin.edit_lock},
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_edit_lock_status(request):
    return Response({"edit_lock": Admin.is_edit_locked()})

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def app_config(request):
    config = Admin.objects.first()
    release = AppRelease.latest()
    return Response({
        "records_per_page": config.records_per_page if config else 25,
        "allow_upload_deletions": config.allow_upload_deletions if config else False,
        # The newest published build, so the app can offer its own update.
        "latest_app_version": release.version if release else None,
        "app_download_url": release.download_url if release else None,
    })
