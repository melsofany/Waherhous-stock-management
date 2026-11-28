"""Releasing a site's app request against its paper, end to end.

A request arrives as a numbered, signed page of the request book. Approving it
deducts the warehouse at once; the vouchers written afterwards are covered by
``test_spending_sheets.py``. The stock arithmetic is checked on the actual Item
and ShopItem rows.
"""

from datetime import date

import pytest

from stock_manager import issue_workflow
from stock_manager.models import (
    AuditEvent,
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


def release(client, site, requests, *, request_no="REQ-1001", issued=None):
    data = {
        "site_id": str(site.pk),
        "request_no": request_no,
        "document_date": date.today().isoformat(),
        "location": "Site A",
        "engineer_name": "Eng. Hassan",
        "representative": "Mandoub Ali",
        "request_id": [str(request.pk) for request in requests],
    }
    for request in requests:
        if issued and request.pk in issued:
            data[f"issued_{request.pk}"] = str(issued[request.pk])
    return client.post("/issues/requests/", data)


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

    def test_approving_a_request_deducts_the_warehouse_at_once(
        self, client, manager, shop_user, make_item
    ):
        first = make_item(sku="SKU-1", quantity=20)
        second = make_item(sku="SKU-2", quantity=20)
        requests = [
            request_line(shop_user, first, 3),
            request_line(shop_user, second, 4),
        ]
        client.force_login(manager)

        response = release(client, shop_user, requests)

        assert response.status_code == 302
        assert response["Location"] == "/issues/"
        document = StockDocument.objects.get(external_ref="REQ-1001")
        assert document.doc_type == StockDocument.SITE_ISSUE
        assert document.status == StockDocument.APPROVED
        assert document.document_date == date.today()
        assert document.location == "Site A"
        assert document.engineer_name == "Eng. Hassan"
        assert document.representative == "Mandoub Ali"
        assert [(line.sku, line.quantity) for line in document.lines.all()] == [
            ("SKU-1", 3),
            ("SKU-2", 4),
        ]
        # No voucher yet: it is written in the book later.
        assert document.voucher_numbers == []
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

        release(client, shop_user, [request], issued={request.pk: 3})

        document = StockDocument.objects.get(external_ref="REQ-1001")
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

        release(client, shop_user, [request], issued={request.pk: 3})

        assert StockDocument.objects.count() == 0
        item.refresh_from_db()
        assert item.quantity == 20
        request.refresh_from_db()
        assert request.quantity == 2

    def test_one_paper_cannot_carry_two_sites(
        self, client, manager, shop_user, other_shop_user, make_item
    ):
        item = make_item(sku="SKU-1", quantity=20)
        first = request_line(shop_user, item, 2)
        second = request_line(other_shop_user, item, 3)
        client.force_login(manager)

        release(client, shop_user, [first, second])

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

        response = release(client, shop_user, [request])

        assert response.status_code == 302
        assert StockDocument.objects.count() == 0
        item.refresh_from_db()
        assert item.quantity == 1
        request.refresh_from_db()
        assert request.quantity == 5

    def test_the_request_number_is_required(
        self, client, manager, shop_user, make_item
    ):
        item = make_item(sku="SKU-1", quantity=20)
        request = request_line(shop_user, item, 2)
        client.force_login(manager)

        release(client, shop_user, [request], request_no="")

        assert StockDocument.objects.count() == 0
        item.refresh_from_db()
        assert item.quantity == 20

    def test_a_request_number_deducts_once(
        self, client, manager, shop_user, make_item
    ):
        item = make_item(sku="SKU-1", quantity=20)
        first = request_line(shop_user, item, 2)
        client.force_login(manager)
        release(client, shop_user, [first], request_no="REQ-7")

        second = request_line(shop_user, item, 3)
        release(client, shop_user, [second], request_no="REQ-7")

        assert StockDocument.objects.filter(external_ref="REQ-7").count() == 1
        item.refresh_from_db()
        assert item.quantity == 18
        second.refresh_from_db()
        assert second.quantity == 3

    def test_only_a_manager_can_approve(self, client, shop_user, make_item):
        item = make_item(sku="SKU-1", quantity=20)
        request = request_line(shop_user, item, 2)
        client.force_login(shop_user)

        release(client, shop_user, [request])

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
        queue = body.split('id="siteRequestGroups"', 1)[1].split("</section>", 1)[0]

        assert shop_user.username in queue
        assert other_shop_user.username not in queue
        assert 'name="request_no"' not in queue

    def test_the_queue_lives_on_the_issues_page_only(
        self, client, manager, shop_user, make_item
    ):
        item = make_item(sku="SKU-1", quantity=20)
        request_line(shop_user, item, 3)
        client.force_login(manager)

        issues = client.get("/issues/").content.decode()
        report = client.get("/issues/report/").content.decode()

        assert 'id="siteRequestGroups"' in issues
        assert 'id="siteRequestGroups"' not in report

    def test_the_queue_is_arabic(self, client, manager, shop_user, make_item):
        item = make_item(sku="SKU-1", quantity=20)
        request_line(shop_user, item, 3)
        client.force_login(manager)

        page = client.get("/issues/", HTTP_ACCEPT_LANGUAGE="ar").content.decode()

        for text in ("طلبات المواقع المنتظرة للاعتماد", "اعتماد وخصم من المخزن", "المندوب"):
            assert text in page


class TestManualPaper:
    def test_a_typed_paper_request_keeps_the_representative(
        self, client, manager, shop_user, make_item
    ):
        item = make_item(sku="SKU-1", quantity=20)
        client.force_login(manager)

        client.post(
            "/issues/",
            {
                "doc_type": StockDocument.SITE_ISSUE,
                "site": str(shop_user.pk),
                "external_ref": "REQ-2002",
                "engineer_name": "Eng. Hassan",
                "representative": "Mandoub Ali",
                "sku": ["SKU-1"],
                "quantity": ["4"],
            },
        )

        document = StockDocument.objects.get(external_ref="REQ-2002")
        assert document.representative == "Mandoub Ali"
        item.refresh_from_db()
        assert item.quantity == 16
