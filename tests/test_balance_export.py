"""The printable balances sheet: logo, project header, one row per Oracle code."""

from io import BytesIO
from zipfile import ZipFile

import pytest
from openpyxl import load_workbook

from stock_manager import item_codes

pytestmark = pytest.mark.django_db


def _download(client, user, app_config, project_name="Ras El Hekma"):
    app_config.project_name = project_name
    app_config.save()
    client.force_login(user)
    response = client.get("/api/export_balances/")
    payload = b"".join(response.streaming_content)
    return response, load_workbook(BytesIO(payload)), payload


class TestBalanceSheet:
    def test_has_the_company_header_and_the_expected_columns(
        self, client, manager, app_config, make_item
    ):
        make_item(sku="ORA-1", description="Cable", unit="MTR", quantity=7)

        response, workbook, _ = _download(client, manager, app_config)
        sheet = workbook.active

        assert response.status_code == 200
        assert response["Content-Type"].startswith(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        assert sheet["B1"].value == "Ras El Hekma"
        assert sheet["B2"].value == "كشف أرصدة المخزن"
        assert [sheet.cell(row=4, column=i).value for i in range(1, 7)] == [
            "م",
            "الكود الداخلي",
            "كود أوراكل",
            "التوصيف",
            "وحدة القياس",
            "الكمية",
        ]
        assert sheet.sheet_view.rightToLeft is True

    def test_each_oracle_code_is_a_separate_row_under_the_internal_code(
        self, client, manager, app_config, make_item
    ):
        first = make_item(sku="ORA-A", description="Cable A", unit="MTR", quantity=5)
        second = make_item(sku="ORA-B", description="Cable B", unit="MTR", quantity=8)
        group = item_codes.merge_codes([first.sku, second.sku])

        _, workbook, _ = _download(client, manager, app_config)
        sheet = workbook.active
        rows = list(sheet.iter_rows(min_row=5, values_only=True))

        assert len(rows) == 2
        assert [row[1] for row in rows] == [group.code, group.code]
        assert [row[2] for row in rows] == ["ORA-A", "ORA-B"]
        assert [row[3] for row in rows] == ["Cable A", "Cable B"]
        assert [row[4] for row in rows] == ["MTR", "MTR"]
        assert [row[5] for row in rows] == [5, 8]
        assert [row[0] for row in rows] == [1, 2]
        assert "ORA-A\nORA-B" not in [row[2] for row in rows]

    def test_the_logo_is_embedded_in_the_workbook(
        self, client, manager, app_config, make_item
    ):
        make_item(sku="ORA-1")

        response, _, payload = _download(client, manager, app_config)

        with ZipFile(BytesIO(payload)) as archive:
            names = archive.namelist()
        assert any(name.startswith("xl/media/") for name in names)

    def test_an_inactive_item_is_not_on_the_balance_sheet(
        self, client, manager, app_config, make_item
    ):
        make_item(sku="ACTIVE-1", quantity=1)
        make_item(sku="OLD-1", quantity=9, is_active=False)

        _, workbook, _ = _download(client, manager, app_config)
        codes = [
            row[2] for row in workbook.active.iter_rows(min_row=5, values_only=True)
        ]

        assert codes == ["ACTIVE-1"]

    def test_a_formula_in_a_description_is_exported_as_text(
        self, client, manager, app_config, make_item
    ):
        make_item(sku="ORA-1", description="=1+1")

        _, workbook, _ = _download(client, manager, app_config)
        rows = list(workbook.active.iter_rows(min_row=5, values_only=True))

        assert rows[0][3] == "'=1+1"

    def test_the_items_page_offers_the_balances_download(
        self, client, manager, app_config
    ):
        client.force_login(manager)

        html = client.get("/stock/").content.decode()

        assert 'id="downloadBalances"' in html
        assert "Download balances sheet" in html

    def test_the_balances_button_is_translated_into_arabic(
        self, client, manager, app_config, settings
    ):
        client.force_login(manager)
        client.cookies[settings.LANGUAGE_COOKIE_NAME] = "ar"

        html = client.get("/stock/").content.decode()

        assert "تحميل كشف الأرصدة" in html

    def test_a_shop_user_may_download_the_shared_balance_sheet(
        self, client, shop_user, app_config, make_item
    ):
        make_item(sku="ORA-1")

        response, _, _ = _download(client, shop_user, app_config)

        assert response.status_code == 200

    def test_an_account_in_no_group_is_refused(
        self, client, plain_user, app_config, make_item
    ):
        make_item(sku="ORA-1")
        client.force_login(plain_user)

        response = client.get("/api/export_balances/")

        assert response.status_code == 403
