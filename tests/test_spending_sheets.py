"""Draft spending sheets, handwritten voucher numbers and the Oracle hand-off.

The warehouse is deducted when a request is approved. Everything here is paper:
collect the issued lines on a draft, print it, write each voucher number back,
and mark the voucher once Oracle has it — without ever moving stock again.
"""

import pytest

from stock_manager import documents as service
from stock_manager import issue_report, permits, reconciliation, spending
from stock_manager.models import (
    AuditEvent,
    IssueVoucher,
    SpendingSheet,
    StockDocument,
    StockDocumentLine,
)

pytestmark = pytest.mark.django_db


def issue(manager, site, lines, **extra):
    return service.create_document(
        doc_type=extra.pop("doc_type", StockDocument.SITE_ISSUE),
        lines=[{"sku": sku, "quantity": quantity} for sku, quantity in lines],
        site=site,
        created_by=manager,
        **extra,
    )


def all_ids():
    return [str(pk) for pk in spending.open_lines().values_list("pk", flat=True)]


@pytest.fixture()
def two_sites(manager, shop_user, other_shop_user, make_item):
    """Two requests for one site and one for another, sharing an item."""
    make_item(sku="SKU-1", quantity=100, unit="قطعة")
    make_item(sku="SKU-2", quantity=100, unit="متر")
    issue(manager, shop_user, [("SKU-1", 3)], external_ref="REQ-1")
    issue(manager, shop_user, [("SKU-1", 4), ("SKU-2", 5)], external_ref="REQ-2")
    issue(manager, other_shop_user, [("SKU-1", 6)], external_ref="REQ-3")
    return shop_user, other_shop_user


def row_for(sheet, site, sku):
    return next(
        row
        for row in spending.sheet_rows(sheet)
        if row["site"] == site.username and row["sku"] == sku
    )


class TestOpenLines:
    def test_only_approved_site_issues_wait_for_a_sheet(
        self, manager, shop_user, make_item
    ):
        make_item(sku="SKU-1", quantity=100)
        approved = issue(manager, shop_user, [("SKU-1", 1)])
        issue(manager, shop_user, [("SKU-1", 2)], pending=True)
        issue(
            manager,
            None,
            [("SKU-1", 3)],
            doc_type=StockDocument.PROJECT_TRANSFER_OUT,
            counterparty="Other project",
        )

        assert [line.document_id for line in spending.open_lines()] == [approved.pk]

    def test_lines_documented_another_way_are_not_offered_again(
        self, manager, shop_user, make_item
    ):
        make_item(sku="SKU-1", quantity=100)
        with_permit = issue(manager, shop_user, [("SKU-1", 1)])
        line = with_permit.lines.get()
        permits.save_permits(
            with_permit,
            [{"permit_no": "P-1", "amount": "1", "note": ""}],
            {str(line.pk): "P-1"},
        )
        posted_before = issue(manager, shop_user, [("SKU-1", 2)])
        StockDocument.objects.filter(pk=posted_before.pk).update(
            oracle_posted_at=posted_before.applied_at
        )

        assert not spending.open_lines().exists()


class TestSheets:
    def test_a_sheet_totals_each_item_per_place(self, manager, two_sites):
        first, second = two_sites

        sheet = spending.create_sheet(line_ids=all_ids(), user=manager)

        assert sheet.number.startswith("SH-")
        rows = spending.sheet_rows(sheet)
        assert [(row["site"], row["sku"], row["quantity"]) for row in rows] == [
            (first.username, "SKU-1", 7),
            (first.username, "SKU-2", 5),
            (second.username, "SKU-1", 6),
        ]
        assert rows[0]["requests"] == ["REQ-1", "REQ-2"]
        assert not spending.open_lines().exists()

    def test_a_later_issue_does_not_change_a_printed_sheet(
        self, manager, two_sites
    ):
        first, _second = two_sites
        sheet = spending.create_sheet(line_ids=all_ids(), user=manager)

        issue(manager, first, [("SKU-1", 50)], external_ref="REQ-9")

        assert row_for(sheet, first, "SKU-1")["quantity"] == 7
        assert spending.open_lines().count() == 1

    def test_a_line_goes_on_one_sheet_only(self, manager, two_sites):
        ids = all_ids()
        spending.create_sheet(line_ids=ids, user=manager)

        with pytest.raises(spending.SpendingError):
            spending.create_sheet(line_ids=ids, user=manager)

        assert SpendingSheet.objects.count() == 1

    def test_deleting_a_draft_reopens_its_lines(self, manager, two_sites):
        sheet = spending.create_sheet(line_ids=all_ids(), user=manager)

        spending.delete_sheet(sheet, user=manager)

        assert not SpendingSheet.objects.exists()
        assert spending.open_lines().count() == 4

    def test_taking_every_line_off_deletes_the_sheet(self, manager, two_sites):
        sheet = spending.create_sheet(line_ids=all_ids(), user=manager)
        values = [row["value"] for row in spending.sheet_rows(sheet)]

        assert spending.remove_lines(sheet, line_ids=values, user=manager) is True
        assert not SpendingSheet.objects.exists()


class TestVouchers:
    def test_one_voucher_covers_two_sites_and_one_sheet_takes_two(
        self, manager, two_sites
    ):
        first, second = two_sites
        sheet = spending.create_sheet(line_ids=all_ids(), user=manager)

        spending.record_voucher(
            sheet,
            line_ids=[
                row_for(sheet, first, "SKU-1")["value"],
                row_for(sheet, second, "SKU-1")["value"],
            ],
            number="000501",
            user=manager,
        )
        spending.record_voucher(
            sheet,
            line_ids=[row_for(sheet, first, "SKU-2")["value"]],
            number="000502",
            user=manager,
        )

        one = IssueVoucher.objects.get(number="000501")
        assert sorted(line.quantity for line in one.lines.all()) == [3, 4, 6]
        assert len({line.document.site_id for line in one.lines.all()}) == 2
        assert IssueVoucher.objects.get(number="000502").lines.get().quantity == 5
        assert not StockDocumentLine.objects.filter(voucher__isnull=True).exists()

    def test_a_repeated_number_must_be_confirmed(self, manager, two_sites):
        first, second = two_sites
        sheet = spending.create_sheet(line_ids=all_ids(), user=manager)
        spending.record_voucher(
            sheet,
            line_ids=[row_for(sheet, first, "SKU-1")["value"]],
            number="000501",
            user=manager,
        )
        other = row_for(sheet, second, "SKU-1")["value"]

        with pytest.raises(spending.SpendingError):
            spending.record_voucher(
                sheet, line_ids=[other], number="000501", user=manager
            )
        spending.record_voucher(
            sheet,
            line_ids=[other],
            number="000501",
            add_to_existing=True,
            user=manager,
        )

        assert IssueVoucher.objects.get().lines.count() == 3

    def test_a_company_permit_number_cannot_become_a_voucher(
        self, manager, two_sites, shop_user
    ):
        sheet = spending.create_sheet(line_ids=all_ids(), user=manager)
        other = issue(manager, shop_user, [("SKU-1", 1)])
        permits.save_permits(
            other, [{"permit_no": "P-1", "amount": "1", "note": ""}], {}
        )

        with pytest.raises(spending.SpendingError):
            spending.record_voucher(
                sheet,
                line_ids=[spending.sheet_rows(sheet)[0]["value"]],
                number="P-1",
                user=manager,
            )

    def test_a_line_on_a_sheet_cannot_take_a_company_permit(
        self, manager, two_sites
    ):
        spending.create_sheet(line_ids=all_ids(), user=manager)
        document = StockDocument.objects.get(external_ref="REQ-1")
        line = document.lines.get()

        with pytest.raises(permits.PermitError):
            permits.save_permits(
                document,
                [{"permit_no": "P-1", "amount": "1", "note": ""}],
                {str(line.pk): "P-1"},
            )

    def test_a_wrong_number_can_be_cleared_and_typed_again(
        self, manager, two_sites
    ):
        sheet = spending.create_sheet(line_ids=all_ids(), user=manager)
        value = spending.sheet_rows(sheet)[0]["value"]
        spending.record_voucher(sheet, line_ids=[value], number="BAD", user=manager)

        with pytest.raises(spending.SpendingError):
            spending.delete_sheet(sheet, user=manager)
        spending.clear_voucher(sheet, line_ids=[value], user=manager)

        assert not IssueVoucher.objects.exists()


class TestOracle:
    def test_posting_moves_no_stock_and_locks_the_voucher(self, manager, two_sites):
        first, _second = two_sites
        sheet = spending.create_sheet(line_ids=all_ids(), user=manager)
        value = row_for(sheet, first, "SKU-1")["value"]
        voucher = spending.record_voucher(
            sheet, line_ids=[value], number="000501", user=manager
        )
        before = list(
            StockDocumentLine.objects.values_list("item__quantity", flat=True)
        )

        spending.post_voucher(voucher, user=manager, note="دفتر أوراكل 12")

        voucher.refresh_from_db()
        assert voucher.oracle_posted_by == manager
        assert voucher.oracle_note == "دفتر أوراكل 12"
        assert list(
            StockDocumentLine.objects.values_list("item__quantity", flat=True)
        ) == before
        assert AuditEvent.objects.filter(
            action=AuditEvent.VOUCHER_ORACLE_POSTED
        ).exists()
        with pytest.raises(spending.SpendingError):
            spending.post_voucher(voucher, user=manager)
        with pytest.raises(spending.SpendingError):
            spending.clear_voucher(sheet, line_ids=[value], user=manager)
        with pytest.raises(spending.SpendingError):
            spending.record_voucher(
                sheet,
                line_ids=[row_for(sheet, first, "SKU-2")["value"]],
                number="000501",
                add_to_existing=True,
                user=manager,
            )


class TestPaperworkElsewhere:
    def test_the_report_and_missing_paperwork_follow_the_voucher(
        self, manager, shop_user, make_item
    ):
        make_item(sku="SKU-1", quantity=100)
        document = issue(manager, shop_user, [("SKU-1", 2)])
        assert [row.number for row in reconciliation.build_rows()] == [
            document.number
        ]
        sheet = spending.create_sheet(line_ids=all_ids(), user=manager)
        spending.record_voucher(
            sheet,
            line_ids=[spending.sheet_rows(sheet)[0]["value"]],
            number="000501",
            user=manager,
        )

        day = document.applied_at.date()
        report = issue_report.build_report(day, day)

        assert reconciliation.build_rows() == []
        assert report["rows"][0]["voucher_no"] == "000501"
        assert report["rows"][0]["oracle"] == issue_report.ORACLE_WAITING
        assert report["totals"]["uncovered_lines"] == 0
        assert report["totals"]["vouchers_pending"] == 1
        assert report["needs_permits"] == []


class TestPages:
    def test_the_whole_loop_through_the_pages(self, client, manager, two_sites):
        client.force_login(manager)
        assert "REQ-3" in client.get("/spending/").content.decode()

        created = client.post("/spending/", {"line": all_ids()})
        sheet = SpendingSheet.objects.get()
        assert created["Location"] == f"/spending/{sheet.pk}/"
        values = [row["value"] for row in spending.sheet_rows(sheet)]

        draft = client.get(f"/spending/{sheet.pk}/print/").content.decode()
        assert "DRAFT" in draft
        assert "SKU-2" in draft

        client.post(
            f"/spending/{sheet.pk}/",
            {"action": "voucher", "row": values, "voucher_no": "000501"},
        )
        voucher = IssueVoucher.objects.get(number="000501")
        assert voucher.lines.count() == 4
        assert "000501" in client.get(f"/spending/{sheet.pk}/").content.decode()

        client.post(f"/spending/vouchers/{voucher.pk}/posted/")
        voucher.refresh_from_db()
        assert voucher.oracle_posted_at is not None
        assert "000501" in client.get("/spending/").content.decode()

    def test_a_refused_number_is_kept_in_the_form(self, client, manager, two_sites):
        client.force_login(manager)
        sheet = spending.create_sheet(line_ids=all_ids(), user=manager)

        response = client.post(
            f"/spending/{sheet.pk}/", {"action": "voucher", "voucher_no": "000777"}
        )

        assert response.status_code == 200
        assert 'value="000777"' in response.content.decode()
        assert not IssueVoucher.objects.exists()

    @pytest.mark.parametrize("path", ["/spending/", "sheet", "print"])
    def test_site_users_cannot_open_the_paperwork(
        self, client, manager, two_sites, path
    ):
        first, _second = two_sites
        sheet = spending.create_sheet(line_ids=all_ids(), user=manager)
        url = {
            "sheet": f"/spending/{sheet.pk}/",
            "print": f"/spending/{sheet.pk}/print/",
        }.get(path, path)
        client.force_login(first)

        response = client.get(url)

        assert response.status_code == 302
        assert response["Location"] == "/"

    def test_the_pages_are_arabic(self, client, manager, two_sites):
        client.force_login(manager)
        sheet = spending.create_sheet(line_ids=all_ids(), user=manager)

        page = client.get("/spending/", HTTP_ACCEPT_LANGUAGE="ar").content.decode()
        draft = client.get(
            f"/spending/{sheet.pk}/print/", HTTP_ACCEPT_LANGUAGE="ar"
        ).content.decode()

        assert "أذون منتظرة للأوراكل" in page
        assert "مسودة — ليست إذن صرف" in draft
