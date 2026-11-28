"""The Oracle-balance page: upload the daily sheet and read the comparison."""

from datetime import date
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.csrf import ensure_csrf_cookie

from .. import audit, oracle_balance
from ..permits import format_amount
from ..models import AuditEvent, OracleSnapshot
from ..permissions import is_warehouse_manager

ORACLE_PAGE_SIZE = 100


def _redirect_oracle():
    return HttpResponseRedirect("/oracle/")


def _balance_date(raw):
    text = (raw or "").strip()
    if not text:
        return timezone.localdate()
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise oracle_balance.OracleFileError(
            _("Enter the balance date as YYYY-MM-DD.")
        )


def _upload(request):
    if not is_warehouse_manager(request.user):
        messages.error(
            request, _("Only a warehouse manager can upload the Oracle sheet.")
        )
        return _redirect_oracle()

    uploaded = request.FILES.get("file")
    if uploaded is None:
        messages.error(request, _("Choose the Oracle sheet first."))
        return _redirect_oracle()

    try:
        balance_date = _balance_date(request.POST.get("balance_date"))
        parsed = oracle_balance.parse_upload(uploaded)
    except oracle_balance.OracleFileError as exc:
        messages.error(request, str(exc))
        return _redirect_oracle()

    source_name = Path(getattr(uploaded, "name", "") or "").name
    snapshot = oracle_balance.save_snapshot(
        parsed,
        user=request.user,
        source_name=source_name,
        balance_date=balance_date,
    )
    audit.record(
        user=request.user,
        action=AuditEvent.DATA_IMPORTED,
        entity="oracle_balance",
        entity_id=snapshot.pk,
        description=(
            f"{snapshot.balance_date} · {snapshot.source_name} · "
            f"{snapshot.row_count} Oracle codes"
        ),
    )
    messages.success(
        request,
        _("The Oracle sheet was uploaded: %(rows)s codes for %(date)s.")
        % {"rows": snapshot.row_count, "date": snapshot.balance_date},
    )
    return _redirect_oracle()


@ensure_csrf_cookie
@login_required
def oracle_balance_page(request):
    """The latest Oracle sheet beside the current warehouse balance."""
    if request.method == "POST":
        return _upload(request)

    snapshot = OracleSnapshot.latest()
    all_rows = oracle_balance.build_rows(snapshot)
    filters = oracle_balance.parse_filters(request.GET)
    rows = oracle_balance.filter_rows(all_rows, **filters)
    page = Paginator(rows, ORACLE_PAGE_SIZE).get_page(request.GET.get("page"))
    grouped = oracle_balance.grouped_rows(all_rows)

    query = request.GET.copy()
    query.pop("page", None)
    querystring = query.urlencode()
    page_prefix = f"/oracle/?{querystring}&" if querystring else "/oracle/?"

    return render(
        request,
        "pages/oracle_balance.html",
        {
            "snapshot": snapshot,
            "rows": page,
            "summary": oracle_balance.summarize(all_rows),
            "grouped": grouped,
            "statuses": oracle_balance.STATUS_LABELS,
            "filters": filters,
            "today": timezone.localdate(),
            "recent": OracleSnapshot.objects.select_related("uploaded_by")[:6],
            "total_value_text": format_amount(snapshot.total_value)
            if snapshot
            else "—",
            "page_url_prefix": page_prefix,
            "is_manager": is_warehouse_manager(request.user),
        },
    )
