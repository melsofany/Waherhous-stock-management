"""The warehouse views, grouped by the job they serve.

Each submodule owns one part of the app; this package re-exports the names the
URL configuration and the other apps use, so ``from stock_manager.views import
x`` keeps working. Nothing here holds logic.

- ``items``          the stock table API (items, shop stock)
- ``transfers``      moving stock to the sites, and the site list
- ``receipts``       goods receipts against a supplier PO
- ``issues``         issuing stock to a site, the issues report, company permits
- ``document_pages`` the stock document pages and the helpers they share
- ``document_api``   the stock documents API
- ``reconcile``      missing paperwork: the reconciliation page and its export
- ``oracle``         the daily Oracle sheet and the balance comparison
- ``labels``         barcodes, the label picker and the item cards
- ``spreadsheet``    Excel export and import
- ``config``         app configuration and the edit lock
- ``session``        sign-in, language, the signed-in user and the overview
"""

from .config import app_config, get_edit_lock_status, set_edit_lock_status
from .document_api import decide_stock_document_api, stock_documents_api
from .document_pages import decide_document, document_detail, document_photo, stock_page
from .issues import (
    create_issue_permit,
    issue_permit_print,
    issue_permits_page,
    issue_to_site,
    issues_page,
    issues_report_page,
    mark_oracle_posted,
)
from .items import ItemViewSet, ShopItemViewSet
from .labels import item_barcode, print_item_cards, print_labels
from .receipts import list_receipts, receipts_page, receive_from_supplier
from .oracle import oracle_balance_page
from .reconcile import (
    reconciliation_export,
    reconciliation_page,
    reconciliation_set_permit,
    reconciliation_set_receipt_permit,
)
from .session import WarehouseLoginView, get_user, index, switch_language
from .spreadsheet import (
    export_balances_excel,
    export_data_excel,
    import_data_excel,
)
from .transfers import (
    TransferItemViewSet,
    complete_transfer,
    list_sites,
    submit_transfer_request,
    transfer_item,
)

__all__ = [
    "ItemViewSet",
    "ShopItemViewSet",
    "TransferItemViewSet",
    "WarehouseLoginView",
    "app_config",
    "complete_transfer",
    "decide_document",
    "decide_stock_document_api",
    "document_detail",
    "document_photo",
    "export_balances_excel",
    "export_data_excel",
    "get_edit_lock_status",
    "get_user",
    "import_data_excel",
    "index",
    "create_issue_permit",
    "issue_permit_print",
    "issue_permits_page",
    "issue_to_site",
    "issues_page",
    "issues_report_page",
    "item_barcode",
    "list_receipts",
    "list_sites",
    "mark_oracle_posted",
    "oracle_balance_page",
    "print_item_cards",
    "print_labels",
    "receipts_page",
    "receive_from_supplier",
    "reconciliation_export",
    "reconciliation_page",
    "reconciliation_set_permit",
    "reconciliation_set_receipt_permit",
    "set_edit_lock_status",
    "stock_documents_api",
    "stock_page",
    "submit_transfer_request",
    "switch_language",
    "transfer_item",
]
