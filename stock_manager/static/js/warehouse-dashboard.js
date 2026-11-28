/*
 * The dashboard: the site-request queue's cancel action and its refresh
 * buttons. Issuing a request happens on the spending page, not here.
 */
(() => {
    'use strict';

    const W = window.Warehouse;

    // Submit transfer request.
    const submitTransferRequest = () => {
        $.ajax({
            url: '/api/submit-transfer-request/',
            type: 'POST',
            contentType: 'application/json',
            data: JSON.stringify({}),
            success: () => {
                W.fetchItems(3);
                W.notify(W.tr('Transfer request sent to the warehouse.'), 'success');
            },
            error: xhr => W.notify(W.errorDetail(xhr, W.tr('Could not send the transfer request.')), 'error')
        });
    };

    // Cancel a transfer request using AJAX POST. Issuing the request is the
    // spending page's job, so the dashboard never calls the dispatch path.
    const cancelTransfer = (sku, shop_user_id) => {
        $.ajax({
            url: '/api/complete-transfer/',
            type: "POST",
            contentType: 'application/json',
            data: JSON.stringify({ sku, shop_user_id, cancel: 'true' }),
            success: () => {
                [1, 3].forEach(tn => W.fetchItems(tn));
                W.notify(W.tr('Transfer cancelled.'), 'success');
            },
            error: xhr => W.notify(W.errorDetail(xhr, W.tr('Could not complete the transfer.')), 'error')
        });
        W.deleteXferVals(shop_user_id, sku);
    };

    window.cancelTransfer = cancelTransfer;

    // Transfer action buttons carry the SKU / shop user as data attributes
    // (read as exact strings) rather than interpolating them into inline
    // onclick handlers, so hostile values cannot inject JavaScript.
    $(document).on("click", ".cancel-btn", function () {
        cancelTransfer($(this).attr('data-sku'), $(this).attr('data-shop-user'));
    });
    $(document).ready(() => {
        $('#submitTransferRequest').click(submitTransferRequest);
        $('#refreshInventory').click(() => W.tablesOnPage().forEach((tn) => W.fetchItems(tn)));
        $('#refreshTransfersPending').click(() => W.tablesOnPage().forEach((tn) => W.fetchItems(tn)));
    });
})();
