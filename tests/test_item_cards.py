"""Printable item cards: the stock card layout, its project header and its codes."""

import pytest
from django.conf import settings
from django.contrib import admin as django_admin

from stock_manager import item_codes
from stock_manager.barcodes import render_barcode_svg
from stock_manager.models import Admin, Item
from stock_manager.admin import ItemAdmin

pytestmark = pytest.mark.django_db


@pytest.fixture
def project(app_config):
    """The project entered once on the App Configuration page."""
    app_config.project_name = "Ras El Hekma Wadi Yam Basement"
    app_config.project_code = "578"
    app_config.save()
    return app_config


@pytest.fixture
def card_item(make_item):
    item = make_item(
        sku="26.05.21.01.03.24.10.000001",
        description="ELECTRICAL COMMON WORK LOW VOLTAGE CU/XLPE/PVC 3C 10MM2",
        unit="LM",
    )
    item.sub_inventory = "كهرباء"
    item.arabic_name = "كابل نحاس 3*10مم"
    item.save()
    return item


class TestProjectSettings:
    def test_project_is_read_from_the_configuration(self, project):
        assert Admin.get_project() == {
            "name": "Ras El Hekma Wadi Yam Basement",
            "code": "578",
        }

    def test_project_is_empty_before_a_configuration_exists(self, db):
        assert Admin.get_project() == {"name": "", "code": ""}

    def test_the_card_uses_the_saved_project_without_retyping_it(
        self, client, manager, project, card_item
    ):
        client.force_login(manager)

        html = client.get(f"/labels/cards/?skus={card_item.sku}").content.decode()

        assert "Ras El Hekma Wadi Yam Basement" in html
        assert "Code : 578" in html

    def test_changing_the_project_once_changes_every_later_card(
        self, client, manager, project, card_item
    ):
        client.force_login(manager)
        project.project_name = "New Site"
        project.project_code = "900"
        project.save()

        html = client.get(f"/labels/cards/?skus={card_item.sku}").content.decode()

        assert "New Site" in html
        assert "Code : 900" in html
        assert "Ras El Hekma" not in html


class TestItemCardContent:
    def test_card_shows_the_stock_card_rows(self, client, manager, project, card_item):
        client.force_login(manager)

        html = client.get(f"/labels/cards/?skus={card_item.sku}").content.decode()

        assert html.count('class="card"') == 1
        for label in (
            "Sub Inventory",
            "Internal Code",
            "Item Code",
            "Unit",
            "Item Description",
            "Arabic Item",
        ):
            assert label in html
        assert "كهرباء" in html
        assert "كابل نحاس 3*10مم" in html
        assert "LM" in html
        assert card_item.description in html

    def test_the_card_stays_english_and_reads_right_to_left(
        self, client, manager, project, card_item
    ):
        client.force_login(manager)
        client.cookies[settings.LANGUAGE_COOKIE_NAME] = "ar"

        html = client.get(f"/labels/cards/?skus={card_item.sku}").content.decode()

        assert 'lang="en"' in html
        assert 'dir="rtl"' in html
        assert "Sub Inventory" in html
        assert "المخزن الفرعي" not in html

    def test_card_prints_the_internal_code_with_its_barcode(
        self, client, manager, project, card_item
    ):
        client.force_login(manager)

        html = client.get(f"/labels/cards/?skus={card_item.sku}").content.decode()

        internal = card_item.internal_code
        assert internal
        assert internal in html
        assert render_barcode_svg(internal) in html
        assert render_barcode_svg(card_item.sku) not in html

    def test_item_code_lists_every_oracle_code_of_the_internal_code(
        self, client, manager, project, make_item
    ):
        first = make_item(sku="ORACLE-A", description="Cable 3C 10MM2", unit="LM")
        second = make_item(sku="ORACLE-B", description="Cable 3C 10MM2", unit="LM")
        item_codes.merge_codes([first.sku, second.sku])
        client.force_login(manager)

        html = client.get(f"/labels/cards/?skus={first.sku}").content.decode()

        assert html.count('class="card"') == 1
        assert "ORACLE-A" in html
        assert "ORACLE-B" in html

    def test_a_card_for_each_selected_item(self, client, manager, project, make_item):
        first = make_item(sku="CARD-1", description="One")
        second = make_item(sku="CARD-2", description="Two")
        client.force_login(manager)

        html = client.get("/labels/cards/?skus=CARD-1,CARD-2").content.decode()

        assert html.count('class="card"') == 2

    def test_an_unknown_code_is_reported_not_silently_skipped(
        self, client, manager, project, card_item
    ):
        client.force_login(manager)

        html = client.get(
            f"/labels/cards/?skus={card_item.sku},MISSING-1"
        ).content.decode()

        assert html.count('class="card"') == 1
        assert "MISSING-1" in html

    def test_each_item_prints_its_own_number_of_copies(
        self, client, manager, project, make_item
    ):
        make_item(sku="COPY-1", description="One")
        make_item(sku="COPY-2", description="Two")
        client.force_login(manager)

        html = client.get("/labels/cards/?cards=COPY-1:3,COPY-2:1").content.decode()

        assert html.count('class="card"') == 4
        assert html.count(">COPY-1<") == 3
        assert html.count(">COPY-2<") == 1

    def test_zero_copies_leaves_the_item_out(self, client, manager, project, make_item):
        make_item(sku="ZERO-1", description="One")
        make_item(sku="ZERO-2", description="Two")
        client.force_login(manager)

        html = client.get("/labels/cards/?cards=ZERO-1:0,ZERO-2:2").content.decode()

        assert html.count('class="card"') == 2
        assert ">ZERO-1<" not in html

    def test_no_selection_says_so(self, client, manager, project):
        client.force_login(manager)

        html = client.get("/labels/cards/").content.decode()

        assert "Select at least one item" in html

    def test_requires_a_login(self, client, card_item):
        response = client.get(f"/labels/cards/?skus={card_item.sku}")

        assert response.status_code == 302


class TestPicker:
    def test_the_picker_offers_the_item_cards_next_to_the_labels(
        self, client, manager, card_item
    ):
        client.force_login(manager)

        html = client.get("/labels/").content.decode()

        assert 'formaction="/labels/cards/"' in html
        assert 'id="printCardsBtn"' in html
        assert 'class="sap-input sap-input--narrow cardCopies"' in html
        assert 'name="cards" id="cardSpec"' in html


class TestCardSettingsPage:
    def test_the_manager_sets_the_project_direction_and_item_fields(
        self, client, manager, app_config, card_item
    ):
        client.force_login(manager)

        response = client.post(
            "/labels/card-settings/",
            {
                "project_name": "New Project",
                "project_code": "900",
                "card_direction": "ltr",
                "card_orientation": "landscape",
                "item_0": card_item.pk,
                "sub_0": "كهرباء",
                "arabic_0": "كابل نحاس",
            },
        )

        assert response.status_code == 302
        app_config.refresh_from_db()
        card_item.refresh_from_db()
        assert app_config.project_name == "New Project"
        assert app_config.project_code == "900"
        assert app_config.card_direction == "ltr"
        assert app_config.card_orientation == "landscape"
        assert card_item.sub_inventory == "كهرباء"
        assert card_item.arabic_name == "كابل نحاس"

    def test_the_saved_direction_and_orientation_reach_the_card(
        self, client, manager, app_config, card_item
    ):
        app_config.project_name = "New Project"
        app_config.project_code = "900"
        app_config.card_direction = "ltr"
        app_config.card_orientation = "landscape"
        app_config.save()
        client.force_login(manager)

        html = client.get(f"/labels/cards/?skus={card_item.sku}").content.decode()

        assert 'dir="ltr"' in html
        assert "size: A4 landscape;" in html
        assert 'class="cards-page cards-page--ltr cards-page--landscape"' in html

    def test_the_page_offers_the_project_and_item_fields(
        self, client, manager, card_item
    ):
        client.force_login(manager)

        html = client.get("/labels/card-settings/").content.decode()

        assert 'name="project_name"' in html
        assert 'name="project_code"' in html
        assert 'name="card_direction"' in html
        assert 'name="card_orientation"' in html
        assert f'name="item_0" value="{card_item.pk}"' in html
        assert 'name="sub_0"' in html
        assert 'name="arabic_0"' in html

    def test_the_page_is_arabic_on_the_arabic_interface(
        self, client, manager, card_item
    ):
        client.force_login(manager)
        client.cookies[settings.LANGUAGE_COOKIE_NAME] = "ar"

        html = client.get("/labels/card-settings/").content.decode()

        assert "بيانات كارت الصنف" in html
        assert "اسم المشروع" in html
        assert "من اليمين لليسار" in html

    def test_a_site_user_cannot_change_the_card(self, client, shop_user, card_item):
        client.force_login(shop_user)

        page = client.get("/labels/card-settings/")
        post = client.post("/labels/card-settings/", {"project_name": "Hacked"})

        assert page.status_code == 302
        assert post.status_code == 302
        assert not Admin.objects.filter(project_name="Hacked").exists()

    def test_the_picker_links_to_the_settings(self, client, manager, card_item):
        client.force_login(manager)

        html = client.get("/labels/").content.decode()

        assert 'href="/labels/card-settings/"' in html
        assert "Item card settings" in html


class TestItemAdminCardFields:
    def test_only_the_card_fields_are_editable_in_the_item_admin(self):
        assert isinstance(django_admin.site._registry[Item], ItemAdmin)
        model_admin = django_admin.site._registry[Item]

        assert set(model_admin.list_editable) == {"arabic_name", "sub_inventory", "unit"}
        for field in ("retail_price", "quantity", "description"):
            assert field in model_admin.readonly_fields

    def test_the_item_admin_cannot_add_items(self, manager):
        from django.test import RequestFactory

        model_admin = django_admin.site._registry[Item]
        request = RequestFactory().get("/admin/")
        request.user = manager

        assert model_admin.has_add_permission(request) is False
