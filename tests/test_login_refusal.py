"""A sign-in refusal names its reason without naming the account.

``lifeendy`` could not sign in and the form said nothing useful: an app-only
account, a closed account and a wrong password all produced the same line. A
wrong password now says so and counts the attempts left. The app-only / closed
reasons are still shown only after the password checks out, so an outsider
learns nothing about which accounts exist or which door they use.
"""

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from stock_manager import auth, mobile
from stock_manager.models import AuditEvent, MobileProfile

pytestmark = pytest.mark.django_db

PASSWORD = "test-pass-123"
APP_ONLY = "This account signs in from the phone app only."
CLOSED = "This account is closed."
PASSWORD_WRONG = "Incorrect password."


def _app_only_user(username="lifeendy"):
    user = User.objects.create_user(username=username, password=PASSWORD)
    MobileProfile.objects.update_or_create(
        user=user,
        defaults={"phone": "201000000001", "mobile_only": True},
    )
    return user


def _sign_in(client, username, password=PASSWORD):
    return client.post(
        reverse("login"), {"username": username, "password": password}, follow=True
    )


def test_an_app_only_account_is_told_where_to_sign_in(client):
    _app_only_user()

    response = _sign_in(client, "lifeendy")

    body = response.content.decode()
    assert APP_ONLY in body
    assert not response.wsgi_request.user.is_authenticated


def test_the_same_refusal_does_not_leak_to_a_wrong_password(client, settings):
    settings.AXES_ENABLED = True
    _app_only_user()

    response = _sign_in(client, "lifeendy", "not-the-password")

    body = response.content.decode()
    assert APP_ONLY not in body
    assert PASSWORD_WRONG in body
    assert "Remaining attempts: 2." in body


def test_a_closed_account_is_named_as_closed(client):
    user = User.objects.create_user(username="closed", password=PASSWORD)
    user.is_active = False
    user.save(update_fields=["is_active"])

    response = _sign_in(client, "closed")

    assert CLOSED in response.content.decode()


def test_a_wrong_password_is_named_with_the_attempts_left(client, settings):
    settings.AXES_ENABLED = True
    User.objects.create_user(username="normal", password=PASSWORD)

    response = _sign_in(client, "normal", "not-the-password")

    body = response.content.decode()
    assert "Incorrect password. Remaining attempts: 2." in body
    assert APP_ONLY not in body and CLOSED not in body


def test_the_count_goes_down_with_each_wrong_password(client, settings):
    settings.AXES_ENABLED = True
    User.objects.create_user(username="normal", password=PASSWORD)

    for remaining in (2, 1):
        response = _sign_in(client, "normal", "not-the-password")
        assert f"Remaining attempts: {remaining}." in response.content.decode()


def test_the_lockout_page_says_no_attempts_remain(client, settings):
    settings.AXES_ENABLED = True
    User.objects.create_user(username="normal", password=PASSWORD)
    for _ in range(3):
        _sign_in(client, "normal", "not-the-password")

    response = client.post(
        reverse("login"), {"username": "normal", "password": PASSWORD}
    )

    assert response.status_code == 429
    assert "Remaining attempts: 0." in response.content.decode()


def test_a_wrong_password_is_explained_in_arabic(client, settings):
    settings.AXES_ENABLED = True
    User.objects.create_user(username="normal", password=PASSWORD)

    response = client.post(
        reverse("login"),
        {"username": "normal", "password": "not-the-password"},
        follow=True,
        HTTP_ACCEPT_LANGUAGE="ar",
    )

    body = response.content.decode()
    assert "كلمة المرور غير صحيحة" in body
    assert "المحاولات المتبقية: 2" in body


def test_the_app_door_still_admits_the_same_account():
    """One backend, two doors: the refusal is per door, not per account."""
    user = _app_only_user()

    admitted = auth.WarehouseBackend().authenticate(
        None, username="lifeendy", password=PASSWORD, app_sign_in=True
    )

    assert admitted == user


def test_the_refusal_is_recorded_so_support_can_answer_why(client):
    _app_only_user()

    _sign_in(client, "lifeendy")

    entries = AuditEvent.objects.filter(action=AuditEvent.LOGIN_FAILED)
    # ONE row per refusal, and it carries the reason.
    assert entries.count() == 1
    assert "lifeendy" in entries.first().description
    assert "app-only" in entries.first().description


def test_the_reason_is_translated_on_the_arabic_page(client):
    """A lazy string built at import time is frozen; the page must be Arabic.

    The first cut built the dict with the module's non-lazy ``gettext``, which
    evaluated it once under English — the Arabic page then showed the English
    sentence for every request. This asserts the rendered text, not the source.
    """
    _app_only_user()

    response = client.post(
        reverse("login"),
        {"username": "lifeendy", "password": PASSWORD},
        follow=True,
        HTTP_ACCEPT_LANGUAGE="ar",
    )

    body = response.content.decode()
    assert "الحساب ده بيدخل من تطبيق الموبايل بس" in body
    assert "This account signs in from the phone app only" not in body
