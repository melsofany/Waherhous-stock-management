"""Sign-in, the language switch, the signed-in user and the overview page.

WarehouseLoginView owns the refusal messages shown on the sign-in page.
"""


from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.exceptions import NON_FIELD_ERRORS
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _lazy
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST
from rest_framework.decorators import api_view
from rest_framework.response import (
    Response,
)  # For returning HTTP responses in REST framework

from .. import lockout
from ..auth import REFUSAL_APP_ONLY, REFUSAL_INACTIVE
from ..models import (
    Admin,
    Item,
    ShopItem,
    TransferItem,
)
from ..permissions import is_warehouse_manager
from ..ui_strings import js_messages


@require_POST
def switch_language(request):
    """Store the chosen interface language and return to the same page.

    Django's own ``set_language`` view re-resolves ``next`` through the URL
    patterns, which sends the dashboard to the API root here, so the redirect
    target is used verbatim (after the usual host check).
    """
    language = request.POST.get("language", "")
    next_url = request.POST.get("next") or "/"
    if not url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        next_url = "/"
    response = HttpResponseRedirect(next_url)
    if language in dict(settings.LANGUAGES):
        response.set_cookie(
            settings.LANGUAGE_COOKIE_NAME,
            language,
            max_age=settings.LANGUAGE_COOKIE_AGE,
            samesite="Lax",
        )
    return response


# Main Page View
@ensure_csrf_cookie
@login_required
def index(request):
    """The overview: the key figures, the site-request queue, and where to go.

    The tables that do the work live on their own pages; this page states the
    numbers so a reader sees the state of the warehouse without opening them.
    """
    user = request.user
    site_lines = ShopItem.objects.filter(quantity__gt=0)
    transfers = TransferItem.objects.filter(ordered=True)
    if not is_warehouse_manager(user):
        site_lines = site_lines.filter(shop_user=user)
        transfers = transfers.filter(shop_user=user)
    return render(
        request,
        "index.html",
        {
            "js_messages": js_messages(),
            "total_items": Item.objects.filter(is_active=True).count(),
            "site_lines": site_lines.count(),
            "pending_transfers": transfers.count(),
            "edit_locked": Admin.is_edit_locked(),
        },
    )


@api_view(["GET"])
def get_user(request):
    # Ensure the user is authenticated
    if not request.user.is_authenticated:
        return Response(
            {"detail": "Authentication credentials were not provided."}, status=401
        )

    user = request.user
    return Response(
        {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "groups": list(user.groups.values_list("name", flat=True)),
        }
    )

REFUSAL_MESSAGES = {
    REFUSAL_APP_ONLY: _lazy("This account signs in from the phone app only. Open the app, or ask a warehouse manager to allow web sign-in for it."),
    REFUSAL_INACTIVE: _lazy("This account is closed. Ask a warehouse manager to reopen it."),
}


class WarehouseLoginView(LoginView):
    """The sign-in form, with a refusal that says what to do next."""

    def form_invalid(self, form):
        refusal = REFUSAL_MESSAGES.get(getattr(self.request, "login_refusal", None))
        if refusal:
            # The credentials were CORRECT, so "enter a correct username and
            # password" is simply untrue — it is replaced, not added to.
            form.errors.pop(NON_FIELD_ERRORS, None)
            form.add_error(None, refusal)
            return super().form_invalid(form)

        # Missing username/password is a field error, not a rejected password.
        if form.errors.get("username") or form.errors.get("password"):
            return super().form_invalid(form)

        remaining = lockout.remaining_attempts(self.request.POST.get("username", ""))
        if remaining is None:
            message = _("Incorrect password.")
        else:
            message = _(
                "Incorrect password. Remaining attempts: %(remaining)s."
            ) % {"remaining": remaining}
        form.errors.pop(NON_FIELD_ERRORS, None)
        form.add_error(None, message)
        return super().form_invalid(form)
