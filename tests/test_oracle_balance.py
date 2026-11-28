"""رصيد الأوراكل: the uploaded sheet and its match against the warehouse."""

from decimal import Decimal
from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from openpyxl import Workbook

from stock_manager import oracle_balance
from stock_manager.models import AuditEvent, Item, ItemGroup, OracleSnapshot

pytestmark = pytest.mark.django_db

HEADER = "ORGANIZATION_NAME,CODE,DESCRIPTION,UOM,ON_HAND,SUB_INV,UNIT_COST,VALUE"


def csv_upload(text, name="oracle.csv"):
    return SimpleUploadedFile(name, text.encode("utf-8"), content_type="text/csv")


def xlsx_upload(rows, name="oracle.xlsx"):
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    output = BytesIO()
    workbook.save(output)
    return SimpleUploadedFile(
        name,
        output.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def sheet(*rows):
    return "\n".join((HEADER, *rows)) + "\n"


def upload(client, path="/oracle/", **extra):
    data = {"balance_date": "2026-11-24", **extra}
    return client.post(path, data)


def snapshot_with(*lines):
    parsed = oracle_balance.ParsedBalance(
        lines=[
            oracle_balance.ParsedLine(
                oracle_code=code,
                organization_name="Project",
                description=f"{code} description",
                unit="EA",
                quantity=quantity,
                sub_inventory="WY.B",
                unit_cost=Decimal("10"),
                value=Decimal("100"),
            )
            for code, quantity in lines
        ]
    )
    return oracle_balance.save_snapshot(parsed, user=None, source_name="oracle.csv")


class TestParsing:
    def test_a_csv_with_a_blank_leading_row_and_arabic_decimals(self):
        parsed = oracle_balance.parse_upload(
            csv_upload(
                sheet(
                    "",
                    "Project,03.21.21.25.51.00.00.000001,Steel,TON,961٫164,WY.B,34430٫91979,33093760٫6",
                )
            )
        )

        line = parsed.lines[0]
        assert line.oracle_code == "03.21.21.25.51.00.00.000001"
        assert line.quantity == Decimal("961.164")
        assert line.value == Decimal("33093760.60")
        assert line.unit == "TON"

    def test_an_xlsx_with_the_same_columns(self):
        parsed = oracle_balance.parse_upload(
            xlsx_upload(
                [
                    ["ORGANIZATION_NAME", "CODE", "DESCRIPTION", "UOM", "ON_HAND"],
                    ["Project", "SKU-1", "Widget", "EA", 7],
                ]
            )
        )

        assert parsed.lines[0].oracle_code == "SKU-1"
        assert parsed.lines[0].quantity == Decimal("7")
        assert parsed.lines[0].description == "Widget"

    def test_duplicate_codes_are_summed_not_dropped(self):
        parsed = oracle_balance.parse_upload(
            csv_upload(
                sheet(
                    "Project,SKU-1,Widget,EA,2,WY.A,10,20",
                    "Project,SKU-1,Widget,EA,3,WY.B,10,30",
                )
            )
        )

        assert len(parsed.lines) == 1
        assert parsed.lines[0].quantity == Decimal("5")
        assert parsed.duplicate_codes == 1

    def test_thousands_and_decimal_commas_are_read_as_numbers(self):
        parsed = oracle_balance.parse_upload(
            csv_upload(
                sheet(
                    "Project,SKU-1,Widget,EA,\"1,250.50\",WY.B,10,20",
                    "Project,SKU-2,Widget,EA,\"1250,50\",WY.B,10,20",
                )
            )
        )

        assert parsed.lines[0].quantity == Decimal("1250.50")
        assert parsed.lines[1].quantity == Decimal("1250.50")

    def test_a_sheet_without_the_required_columns_is_refused(self):
        with pytest.raises(oracle_balance.OracleFileError, match="CODE"):
            oracle_balance.parse_upload(
                csv_upload("NAME,QUANTITY\nWidget,3\n")
            )

    def test_a_non_numeric_quantity_names_the_row(self):
        with pytest.raises(oracle_balance.OracleFileError, match="Row 2"):
            oracle_balance.parse_upload(
                csv_upload(sheet("Project,SKU-1,Widget,EA,not-a-number,,"))
            )

    def test_the_old_xls_format_is_refused_with_an_explanation(self):
        with pytest.raises(oracle_balance.OracleFileError, match="xls"):
            oracle_balance.parse_upload(
                SimpleUploadedFile("oracle.xls", b"old", content_type="application/vnd.ms-excel")
            )


class TestComparison:
    def test_matched_different_and_missing_on_both_sides(
        self, make_item
    ):
        make_item(sku="SKU-1", quantity=10)
        make_item(sku="SKU-2", quantity=3)
        make_item(sku="SKU-4", quantity=2)
        make_item(sku="SKU-5", quantity=0)
        snapshot = snapshot_with(
            ("SKU-1", Decimal("10")),
            ("SKU-2", Decimal("4")),
            ("SKU-3", Decimal("7")),
        )

        rows = oracle_balance.build_rows(snapshot)
        by_code = {row.oracle_code: row for row in rows}

        assert by_code["SKU-1"].status == oracle_balance.MATCHED
        assert by_code["SKU-1"].difference == Decimal("0")
        assert by_code["SKU-2"].status == oracle_balance.DIFFERENT
        assert by_code["SKU-2"].difference == Decimal("-1")
        assert by_code["SKU-3"].status == oracle_balance.MISSING_WAREHOUSE
        assert by_code["SKU-3"].warehouse_text == "—"
        assert by_code["SKU-4"].status == oracle_balance.MISSING_ORACLE
        assert by_code["SKU-4"].oracle_text == "—"
        assert "SKU-5" not in by_code

    def test_an_internal_code_is_compared_by_the_total_of_its_codes(
        self, make_item
    ):
        group = ItemGroup.create_next()
        make_item(sku="SKU-A", quantity=0)
        make_item(sku="SKU-B", quantity=10)
        Item.objects.filter(sku__in=["SKU-A", "SKU-B"]).update(internal_group=group)
        snapshot = snapshot_with(
            ("SKU-A", Decimal("5")),
            ("SKU-B", Decimal("5")),
        )

        rows = oracle_balance.build_rows(snapshot)
        groups = oracle_balance.grouped_rows(rows)

        assert [row.status for row in rows] == [
            oracle_balance.DIFFERENT,
            oracle_balance.DIFFERENT,
        ]
        assert len(groups) == 1
        assert groups[0].internal_code == group.code
        assert groups[0].status == oracle_balance.MATCHED
        assert groups[0].warehouse_quantity == Decimal("10")
        assert groups[0].oracle_quantity == Decimal("10")

    def test_differences_are_listed_before_matches(self, make_item):
        make_item(sku="SKU-1", quantity=10)
        make_item(sku="SKU-2", quantity=3)
        snapshot = snapshot_with(
            ("SKU-1", Decimal("10")),
            ("SKU-2", Decimal("4")),
        )

        rows = oracle_balance.build_rows(snapshot)

        assert [row.oracle_code for row in rows] == ["SKU-2", "SKU-1"]

    def test_the_summary_counts_each_kind(self, make_item):
        make_item(sku="SKU-1", quantity=10)
        make_item(sku="SKU-2", quantity=3)
        make_item(sku="SKU-4", quantity=2)
        snapshot = snapshot_with(
            ("SKU-1", Decimal("10")),
            ("SKU-2", Decimal("4")),
            ("SKU-3", Decimal("7")),
        )

        summary = oracle_balance.summarize(oracle_balance.build_rows(snapshot))

        assert summary == {
            "matched": 1,
            "different": 1,
            "missing_warehouse": 1,
            "missing_oracle": 1,
            "total": 4,
            "oracle_codes": 3,
            "warehouse_codes": 3,
            "with_difference": 3,
        }


class TestUploadPage:
    def test_a_manager_uploads_a_sheet_and_sees_the_comparison(
        self, client, manager, make_item
    ):
        make_item(sku="SKU-1", quantity=10)
        client.force_login(manager)

        response = upload(
            client,
            file=csv_upload(sheet("Project,SKU-1,Widget,EA,7,WY.B,10,70")),
        )

        assert response.status_code == 302
        snapshot = OracleSnapshot.latest()
        assert snapshot.balance_date.isoformat() == "2026-11-24"
        assert snapshot.row_count == 1
        assert snapshot.uploaded_by == manager

        page = client.get("/oracle/")
        body = page.content.decode()
        assert "SKU-1" in body
        assert "Quantity differs" in body
        assert "Oracle balance" in body

    def test_the_page_shows_the_internal_code_total(self, client, manager, make_item):
        group = ItemGroup.create_next()
        make_item(sku="SKU-A", quantity=0)
        make_item(sku="SKU-B", quantity=10)
        Item.objects.filter(sku__in=["SKU-A", "SKU-B"]).update(internal_group=group)
        client.force_login(manager)
        upload(
            client,
            file=csv_upload(
                sheet(
                    "Project,SKU-A,Widget,EA,5,WY.B,10,50",
                    "Project,SKU-B,Widget,EA,5,WY.B,10,50",
                )
            ),
        )

        body = client.get("/oracle/").content.decode()

        assert 'id="oracleGroups"' in body
        assert group.code in body
        assert "Internal codes covering several Oracle codes" in body

    def test_the_page_is_translated_into_arabic(self, client, manager):
        client.force_login(manager)

        body = client.get(
            "/oracle/", HTTP_ACCEPT_LANGUAGE="ar"
        ).content.decode()

        assert "رصيد الأوراكل" in body
        assert "رفع شيت الأوراكل" in body

    def test_a_shop_user_may_read_but_not_upload(self, client, shop_user):
        client.force_login(shop_user)

        page = client.get("/oracle/")
        assert page.status_code == 200
        assert "Upload the Oracle sheet" not in page.content.decode()

        response = upload(
            client,
            file=csv_upload(sheet("Project,SKU-1,Widget,EA,7,WY.B,10,70")),
        )

        assert response.status_code == 302
        assert OracleSnapshot.objects.count() == 0

    def test_a_bad_sheet_is_refused_without_a_snapshot(self, client, manager):
        client.force_login(manager)

        response = upload(client, file=csv_upload("NAME,QUANTITY\nWidget,3\n"))

        assert response.status_code == 302
        assert OracleSnapshot.objects.count() == 0
        assert "CODE" in client.get("/oracle/").content.decode()

    def test_the_upload_is_recorded_in_the_audit_log(self, client, manager):
        client.force_login(manager)

        upload(client, file=csv_upload(sheet("Project,SKU-1,Widget,EA,7,WY.B,10,70")))

        event = AuditEvent.objects.get(action=AuditEvent.DATA_IMPORTED)
        assert event.entity == "oracle_balance"
        assert "1 Oracle codes" in event.description

    def test_the_latest_upload_wins_and_the_previous_one_is_kept(
        self, client, manager
    ):
        client.force_login(manager)
        upload(client, file=csv_upload(sheet("Project,OLD,Old,EA,1,,,")))
        upload(client, file=csv_upload(sheet("Project,NEW,New,EA,2,,,")))

        page = client.get("/oracle/").content.decode()

        assert "NEW" in page
        assert OracleSnapshot.objects.count() == 2

    def test_filters_narrow_the_table(self, client, manager, make_item):
        make_item(sku="SKU-1", quantity=10)
        make_item(sku="SKU-2", quantity=3)
        client.force_login(manager)
        upload(
            client,
            file=csv_upload(
                sheet(
                    "Project,SKU-1,Widget,EA,10,WY.B,10,100",
                    "Project,SKU-2,Other,EA,4,WY.B,10,40",
                )
            ),
        )

        body = client.get("/oracle/?status=different").content.decode()
        assert "SKU-2" in body and "SKU-1" not in body

        body = client.get("/oracle/?q=Other").content.decode()
        assert "SKU-2" in body and "SKU-1" not in body

    def test_an_empty_page_explains_what_to_do(self, client, manager):
        client.force_login(manager)

        body = client.get("/oracle/").content.decode()

        assert "No Oracle sheet has been uploaded yet." in body
        assert "Upload the daily export above" in body
