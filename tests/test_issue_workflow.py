"""The site-request → issue-permit workflow, end to end.

The operator's loop is: collect a site's requests, decide what is released,
write one numbered permit, print three copies, and hand the Oracle copy to the
clerk. These tests pin the release, the paper and the hand-off; the stock
arithmetic is checked on the actual Item and ShopItem rows.
"""

from datetime import date

import pytest
from django.urls import reverse

from stock_manager import issue_workflow
from stock_manager.models import (
    AuditEvent,
    Item,
    ShopItem,
    StockDocument,
    TransferItem,
)

pytestmark = pytest.mark.django_db


def request_line(site, item, quantity, *, ordered=True):
    return TransferItem.objects.create(
        shop_user=site,
        item=item,
        quantity=quantity,
        ordered=ordered,
    )


def create_permit(client, site, requests, *, permit_no="ISS-PAPER-1", issued=None):
    data = {
        "site_id": str(site.pk),
        "permit_no": permit_no,
        "document_date": date.today().isoformat(),
        "location": "Site A",
        "engineer_name": "Eng. Hassan",
        "request_id": [str(request.pk) for request in requests],
    }
    for request in requests:
        if issued and request.pk in issued:
            data[f"issued_{request.pk}"] = str(issued[request.pk])
    return client.post("/issues/permits/", data)


class TestCollectedRequests:
    def test_the_queue_is_grouped_by_site_with_its_total(
        self, manager, shop_user, other_shop_user, make_item
    ):
        first = make_item(sku="SKU-1", quantity=20)
        second = make_item(sku="SKU-2", quantity=20)
        request_line(shop_user, first, 3)
        request_line(shop_user, second, 4)
        request_line(other_shop_user, first, 9)
        request_line(shop_user, first, 99, ordered=False)

        groups = issue_workflow.pending_groups()

        assert [group["site"].username for group in groups] == [
            shop_user.username,
            other_shop_user.username,
        ]
        own = next(group for group in groups if group["site"] == shop_user)
        assert own["item_count"] == 2
        assert own["total_quantity"] == 7

    def test_a_site_queue_becomes_one_numbered_permit(
        self, client, manager, shop_user, make_item
    ):
        first = make_item(sku="SKU-1", quantity=20)
        second = make_item(sku="SKU-2", quantity=20)
        requests = [
            request_line(shop_user, first, 3),
            request_line(shop_user, second, 4),
        ]
        client.force_login(manager)

        response = create_permit(client, shop_user, requests)

        assert response.status_code == 302
        document = StockDocument.objects.get(external_ref="ISS-PAPER-1")
        assert response["Location"] == f"/issues/{document.pk}/print/"
        assert document.doc_type == StockDocument.SITE_ISSUE
        assert document.status == StockDocument.APPROVED
        assert document.document_date == date.today()
        assert document.location == "Site A"
        assert document.engineer_name == "Eng. Hassan"
        assert [(line.sku, line.quantity) for line in document.lines.all()] == [
            ("SKU-1", 3),
            ("SKU-2", 4),
        ]
        first.refresh_from_db()
        second.refresh_from_db()
        assert first.quantity == 17
        assert second.quantity == 16
        assert ShopItem.objects.get(shop_user=shop_user, item=first).quantity == 3
        assert ShopItem.objects.get(shop_user=shop_user, item=second).quantity == 4
        assert not TransferItem.objects.filter(pk__in=[r.pk for r in requests]).exists()
        assert AuditEvent.objects.filter(
            action=AuditEvent.DOCUMENT_CREATED, entity_id=str(document.pk)
        ).exists()

    def test_a_partial_release_leaves_the_remainder_on_the_queue(
        self, client, manager, shop_user, make_item
    ):
        item = make_item(sku="SKU-1", quantity=20)
        request = request_line(shop_user, item, 5)
        client.force_login(manager)

        create_permit(client, shop_user, [request], issued={request.pk: 3})

        document = StockDocument.objects.get(external_ref="ISS-PAPER-1")
        assert document.lines.get().quantity == 3
        item.refresh_from_db()
        assert item.quantity == 17
        request.refresh_from_db()
        assert request.quantity == 2
        assert request.ordered is True

    def test_a_request_cannot_be_released_beyond_what_the_site_asked(
        self, client, manager, shop_user, make_item
    ):
        item = make_item(sku="SKU-1", quantity=20)
        request = request_line(shop_user, item, 2)
        client.force_login(manager)

        create_permit(client, shop_user, [request], issued={request.pk: 3})

        assert StockDocument.objects.count() == 0
        item.refresh_from_db()
        assert item.quantity == 20
        request.refresh_from_db()
        assert request.quantity == 2

    def test_requests_from_two_sites_cannot_share_a_permit(
        self, client, manager, shop_user, other_shop_user, make_item
    ):
        item = make_item(sku="SKU-1", quantity=20)
        first = request_line(shop_user, item, 2)
        second = request_line(other_shop_user, item, 3)
        client.force_login(manager)

        create_permit(client, shop_user, [first, second])

        assert StockDocument.objects.count() == 0
        assert TransferItem.objects.filter(pk__in=[first.pk, second.pk]).count() == 2
        item.refresh_from_db()
        assert item.quantity == 20

    def test_insufficient_stock_refuses_without_consuming_the_request(
        self, client, manager, shop_user, make_item
    ):
        item = make_item(sku="SKU-1", quantity=1)
        request = request_line(shop_user, item, 5)
        client.force_login(manager)

        response = create_permit(client, shop_user, [request])

        assert response.status_code == 302
        assert StockDocument.objects.count() == 0
        item.refresh_from_db()
        assert item.quantity == 1
        request.refresh_from_db()
        assert request.quantity == 5

    def test_a_permit_number_is_required(
        self, client, manager, shop_user, make_item
    ):
        item = make_item(sku="SKU-1", quantity=20)
        request = request_line(shop_user, item, 2)
        client.force_login(manager)

        create_permit(client, shop_user, [request], permit_no="")

        assert StockDocument.objects.count() == 0
        item.refresh_from_db()
        assert item.quantity == 20

    def test_a_permit_number_is_booked_once(
        self, client, manager, shop_user, make_item
    ):
        item = make_item(sku="SKU-1", quantity=20)
        first = request_line(shop_user, item, 2)
        client.force_login(manager)
        create_permit(client, shop_user, [first], permit_no="PAPER-7")

        second = request_line(shop_user, item, 3)
        create_permit(client, shop_user, [second], permit_no="PAPER-7")

        assert StockDocument.objects.filter(external_ref="PAPER-7").count() == 1
        item.refresh_from_db()
        assert item.quantity == 18
        assert TransferItem.objects.filter(pk=second.pk).exists()
        assert TransferItem.objects.filter(pk=second.pk).get().quantity == 3

    def test_only_a_manager_can_create_a_permit(
        self, client, shop_user, make_item
    ):
        item = make_item(sku="SKU-1", quantity=20)
        request = request_line(shop_user, item, 2)
        client.force_login(shop_user)

        create_permit(client, shop_user, [request])

        assert StockDocument.objects.count() == 0
        assert TransferItem.objects.filter(pk=request.pk).exists()

    def test_a_site_user_sees_only_their_own_queue(
        self, client, shop_user, other_shop_user, make_item
    ):
        item = make_item(sku="SKU-1", quantity=20)
        request_line(shop_user, item, 2)
        request_line(other_shop_user, item, 3)
        client.force_login(shop_user)

        body = client.get("/issues/").content.decode()
        queue = body.split('id="siteRequestGroups"', 1)[1].split(
            'id="oracleQueue"', 1
        )[0]

        assert shop_user.username in queue
        assert other_shop_user.username not in queue


class TestThePaper:
    def test_the_print_page_has_three_named_copies(
        self, client, manager, shop_user, make_item
    ):
        item = make_item(sku="SKU-1", quantity=20, unit="قطعة")
        request = request_line(shop_user, item, 3)
        client.force_login(manager)
        create_permit(client, shop_user, [request])
        document = StockDocument.objects.get(external_ref="ISS-PAPER-1")

        page = client.get(f"/issues/{document.pk}/print/")
        body = page.content.decode()

        assert page.status_code == 200
        assert body.count('class="permit-copy"') == 3
        assert body.count('data-copy="warehouse"') == 1
        assert body.count('data-copy="site"') == 1
        assert body.count('data-copy="oracle"') == 1
        assert "ISS-PAPER-1" in body
        assert "SKU-1" in body
        assert "قطعة" in body

    def test_the_operator_flow_is_arabic_end_to_end(
        self, client, manager, shop_user, make_item
    ):
        item = make_item(sku="SKU-1", quantity=20, unit="قطعة")
        request = request_line(shop_user, item, 3)
        client.force_login(manager)

        queue = client.get(
            "/issues/", HTTP_ACCEPT_LANGUAGE="ar"
        ).content.decode()
        assert "طلبات المواقع المنتظرة لإذن صرف" in queue
        assert "إنشاء إذن الصرف والطباعة" in queue

        create_permit(client, shop_user, [request])
        document = StockDocument.objects.get(external_ref="ISS-PAPER-1")
        paper = client.get(
            f"/issues/{document.pk}/print/", HTTP_ACCEPT_LANGUAGE="ar"
        ).content.decode()
        for text in ("نسخة المخزن", "نسخة الموقع", "نسخة الأوراكل", "أمين المخزن"):
            assert text in paper

    def test_another_site_cannot_read_the_permit(
        self, client, manager, shop_user, other_shop_user, make_item
    ):
        item = make_item(sku="SKU-1", quantity=20)
        request = request_line(shop_user, item, 3)
        client.force_login(manager)
        create_permit(client, shop_user, [request])
        document = StockDocument.objects.get(external_ref="ISS-PAPER-1")
        client.force_login(other_shop_user)

        response = client.get(f"/issues/{document.pk}/print/")

        assert response.status_code == 302
        assert response["Location"] == "/issues/"


class TestOraclePosting:
    def test_posting_records_the_handoff_without_moving_stock(
        self, client, manager, shop_user, make_item
    ):
        item = make_item(sku="SKU-1", quantity=20)
        request = request_line(shop_user, item, 3)
        client.force_login(manager)
        create_permit(client, shop_user, [request])
        document = StockDocument.objects.get(external_ref="ISS-PAPER-1")
        item.refresh_from_db()
        quantity_after_release = item.quantity

        response = client.post(
            f"/issues/{document.pk}/oracle-posted/",
            {"note": "دفتر أوراكل 12", "next": "/issues/"},
        )

        document.refresh_from_db()
        assert response.status_code == 302
        assert document.oracle_posted_at is not None
        assert document.oracle_posted_by == manager
        assert document.oracle_note == "دفتر أوراكل 12"
        item.refresh_from_db()
        assert item.quantity == quantity_after_release == 17
        assert AuditEvent.objects.filter(
            action=AuditEvent.DOCUMENT_ORACLE_POSTED, entity_id=str(document.pk)
        ).exists()

    def test_a_pending_permit_cannot_be_posted(
        self, client, manager, shop_user, make_item
    ):
        item = make_item(sku="SKU-1", quantity=20)
        document = StockDocument.objects.create(
            number="ISS-PENDING-1",
            direction=StockDocument.OUT,
            doc_type=StockDocument.SITE_ISSUE,
            status=StockDocument.PENDING,
            site=shop_user,
            created_by=shop_user,
        )
        client.force_login(manager)

        client.post(f"/issues/{document.pk}/oracle-posted/", {"next": "/issues/"})

        document.refresh_from_db()
        assert document.oracle_posted_at is None

    def test_only_a_manager_can_post_to_oracle(
        self, client, manager, shop_user, make_item
    ):
        item = make_item(sku="SKU-1", quantity=20)
        request = request_line(shop_user, item, 3)
        client.force_login(manager)
        create_permit(client, shop_user, [request])
        document = StockDocument.objects.get(external_ref="ISS-PAPER-1")
        client.force_login(shop_user)

        client.post(f"/issues/{document.pk}/oracle-posted/", {"next": "/issues/"})

        document.refresh_from_db()
        assert document.oracle_posted_at is None

    def test_the_report_counts_permits_waiting_for_oracle(
        self, client, manager, shop_user, make_item
    ):
        item = make_item(sku="SKU-1", quantity=20)
        request = request_line(shop_user, item, 3)
        client.force_login(manager)
        create_permit(client, shop_user, [request])
        document = StockDocument.objects.get(external_ref="ISS-PAPER-1")

        pending = client.get("/issues/report/").content.decode()
        client.post(f"/issues/{document.pk}/oracle-posted/", {"next": "/issues/"})
        posted = client.get("/issues/report/").content.decode()

        assert "kpiOraclePending" in pending
        assert "kpiOraclePending" in posted
        assert "Not posted" in pending
        assert "Posted" in posted
