/*
 * The items page: add/update/delete/transfer, the Excel exchange and the
 * maintenance-mode switch. Loaded only by /stock/.
 */
(() => {
    'use strict';

    const W = window.Warehouse;

    // Add a new item using AJAX POST.
    const addItem = () => {
        const retailPrice = $('#retail_price').val();
        if (!/^\d+(\.\d{1,2})?$/.test(retailPrice)) {
            W.notify(W.tr('Retail price must be a valid number with up to 2 decimal places.'), 'warning');
            return;
        }
        const data = {
            sku: $('#sku').val(),
            description: $('#description').val(),
            retail_price: retailPrice,
            quantity: $('#quantity').val(),
            unit: $('#unit').val()
        };
        $.ajax({
            url: '/api/items/',
            type: 'POST',
            contentType: 'application/json',
            data: JSON.stringify(data),
            success: () => {
                W.fetchItems(1);
                $('#sku, #description, #retail_price, #quantity, #unit').val('');
                W.notify(W.tr('Item created successfully.'), 'success');
            },
            error: xhr => {
                W.notify(W.errorDetail(xhr, W.tr('Could not create the item.')), 'error');
            }
        });
    };

    // Update an item using AJAX PUT.
    const updateItem = (tableNum, sku, fieldName, value) => {
        if (tableNum == 1) {
            if (fieldName == 'retail_price' && !/^\d+(\.\d{1,2})?$/.test(value)) {
                W.notify(W.tr('Retail price must be a valid number with up to 2 decimal places.'), 'warning');
                return;
            }
            const data = {
                [fieldName]: value
            };
            $.ajax({
                url: `/api/items/${encodeURIComponent(sku)}/`,
                type: 'PUT',
                contentType: 'application/json',
                data: JSON.stringify(data),
                success: () => W.fetchItems(1),
                error: xhr => {
                    W.notify(W.errorDetail(xhr, W.tr('Could not update the item.')), 'error');
                }
            });
        }
    };

    // Delete an item using AJAX DELETE.
    const deleteItem = async (sku, tableNum) => {
        const confirmed = await W.confirmDialog({
            title: W.tr('Delete item?'),
            message: `Remove "${sku}" from the active warehouse catalogue?\n\nExisting transfer records are unaffected.`,
            confirmLabel: W.tr('Delete'),
            cancelLabel: 'Cancel',
            destructive: true
        });
        if (!confirmed) return;

        const apiUrl = tableNum === 1 ?
            `/api/items/${encodeURIComponent(sku)}/` :
            `/api/shop_items/${encodeURIComponent(sku)}/`;
        $.ajax({
            url: apiUrl,
            type: 'DELETE',
            success: () => {
                W.fetchItems(tableNum);
                W.notify(W.tr('Item deleted.'), 'success');
            },
            error: xhr => {
                W.notify(W.errorDetail(xhr, W.tr('Could not delete the item.')), 'error');
            }
        });
    };

    // Transfer an item using AJAX POST.
    const transferItem = (sku, xfer_qnt) => {
        $.ajax({
            url: '/api/transfer/',
            type: 'POST',
            contentType: 'application/json',
            data: JSON.stringify({ sku, transfer_quantity: xfer_qnt }),
            success: () => {
                W.fetchItems(3);
            },
            error: xhr => {
                W.notify(W.errorDetail(xhr, W.tr('Could not add the transfer.')), 'error');
                W.fetchItems(1);
            }
        });
    };
    // Download stock data.
    const downloadStockData = () => {
        const url = '/api/export_data/';
        window.location.href = url;
    };

    // Download the printable balances sheet (logo, project, one row per code).
    const downloadBalances = () => {
        window.location.href = '/api/export_balances/';
    };

    // Handle stock data upload.
    const uploadStockData = async () => {
        const fileInput = document.getElementById('uploadStockData');
        const file = fileInput.files[0];
        if (!file) {
            W.notify(W.tr('Please select a file to upload.'), 'warning');
            return;
        }

        // Check for destructive upload config.
        if (window.allowUploadDeletions) {
            const confirmed = await W.confirmDialog({
                title: 'Destructive upload',
                message: W.tr('The system is configured to delete all warehouse and site data that is not included in this upload.') + ' ' + W.tr('This action cannot be undone. Continue?'),
                confirmLabel: W.tr('Upload anyway'),
                cancelLabel: 'Cancel',
                destructive: true
            });
            if (!confirmed) {
                return;
            }
        }

        const formData = new FormData();
        formData.append('file', file);

        $.ajax({
            url: '/api/import_data/',
            type: 'POST',
            data: formData,
            processData: false,
            contentType: false,
            success: (data) => {
                let msg = W.tr('Stock data uploaded successfully.');
                if (data && data.detail) msg = data.detail;
                if (data && Array.isArray(data.skipped_skus) && data.skipped_skus.length) {
                    msg += '\n\nThe following SKUs had invalid retail_price values and were skipped:\n' + data.skipped_skus.join(', ');
                }
                W.notify(msg, 'success');
                W.fetchItems(1);
            },
            error: xhr => {
                W.notify(W.errorDetail(xhr, W.tr('Failed to upload stock data.')), 'error');
            }
        });
    };

    // Mark edited fields so unsaved changes are visible.
    const attachQntChangeColorUpdateListener = () => {
        W.tablesOnPage().forEach(tableNum => {
            $(`#itemTable${tableNum}`).on('input', 'input[type="number"]', function () {
                $(this).addClass('is-dirty');
            });
        });
    };

    // Toggle update mode.
    const updateModeChangeHandler = () => {
        let current_lock_status = $('#updateModeToggle').prop('checked');
        W.setEditLockStatus(current_lock_status);
    };

    W.addItem = addItem;
    W.updateItem = updateItem;
    W.deleteItem = deleteItem;
    W.transferItem = transferItem;
    W.updateModeChangeHandler = updateModeChangeHandler;

    window.updateItem = updateItem;
    window.deleteItem = deleteItem;
    window.transferItem = transferItem;
    window.updateModeChangeHandler = updateModeChangeHandler;

    $(document).on("blur", ".wh_editable-field", function () {
        const sku = $(this).data("sku");
        const fieldName = $(this).data("field");
        let newValue = $(this).val();
        if (fieldName === 'xfer_qnt') {
            if (Number.isInteger(parseInt(newValue), 10)) {
                newValue > 0 ? transferItem(sku, newValue) : console.log(W.tr('Enter more than 0!'));
            }
            return;
        }
        if (fieldName === 'sku') {
            newValue = String(newValue || '').trim();
            if (!newValue) {
                $(this).val(sku);
                return;
            }
            if (newValue === String(sku)) {
                $(this).val(newValue);
                return;
            }
            $(this).val(newValue);
        }
        updateItem(1, sku, fieldName, newValue);
    });

    $(document).on("click", ".delete-item-btn", function () {
        deleteItem($(this).attr('data-sku'), 1);
    });

    $(document).ready(() => {
        $('#addItem').click(addItem);
        $('#updateModeToggle').change(updateModeChangeHandler);
        attachQntChangeColorUpdateListener();
        $('#refreshWarehouse').click(() => W.fetchItems(1));
        $('#downloadStockData').click(() => downloadStockData());
        $('#downloadBalances').click(() => downloadBalances());
        $('#uploadStockDataButton').click(uploadStockData);
    });
})();
