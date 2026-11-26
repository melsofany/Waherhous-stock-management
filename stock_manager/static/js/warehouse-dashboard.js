/*
 * The dashboard: the site-request queue's dispatch/cancel actions and its
 * refresh buttons. Loaded only by /.
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

    // Complete or cancel a transfer using AJAX POST.
    const completeTransfer = (sku, shop_user_id, cancel) => {
        const quantity = W.getXferVals(shop_user_id, sku);
        $.ajax({
            url: '/api/complete-transfer/',
            type: "POST",
            contentType: 'application/json',
            data: JSON.stringify({ sku, shop_user_id, quantity, cancel }),
            success: () => {
                [1, 3].forEach(tn => W.fetchItems(tn));
                W.notify(cancel ? W.tr('Transfer cancelled.') : W.tr('Transfer dispatched.'), 'success');
            },
            error: xhr => W.notify(W.errorDetail(xhr, W.tr('Could not complete the transfer.')), 'error')
        });
        W.deleteXferVals(shop_user_id, sku);
    };

    window.completeTransfer = completeTransfer;

    // Transfer action buttons carry the SKU / shop user as data attributes
    // (read as exact strings) rather than interpolating them into inline
    // onclick handlers, so hostile values cannot inject JavaScript.
    $(document).on("click", ".dispatch-btn", function () {
        completeTransfer($(this).attr('data-sku'), $(this).attr('data-shop-user'), 'false');
    });
    $(document).on("click", ".cancel-btn", function () {
        completeTransfer($(this).attr('data-sku'), $(this).attr('data-shop-user'), 'true');
    });
    $(document).on("blur", `input[id^="quantity3-"]`, function () {
        const id = $(this).attr('id');
        const sku = id.replace('quantity3-', '');
        const shopUserID = $(this).data('shop_user_id');
        W.setXferVals(shopUserID, sku, $(this).val());
    });

    $(document).ready(() => {
        $('#submitTransferRequest').click(submitTransferRequest);
        $('#refreshInventory').click(() => W.tablesOnPage().forEach((tn) => W.fetchItems(tn)));
        $('#refreshTransfersPending').click(() => W.tablesOnPage().forEach((tn) => W.fetchItems(tn)));
    });
})();
