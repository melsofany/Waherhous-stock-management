/*
 * The warehouse table engine: renders the stock table and the site-request
 * queue from the API, including grouped internal codes and pagination.
 */
(() => {
    'use strict';

    const W = window.Warehouse;

    const currentFetchRequests = {
        1: null,
        2: null,
        3: null
    };

    // Per-table DOM handles, labels and KPI tile targets. Table 2 (site stock)
    // is rendered by the server on the items page, so it has no entry here.
    const TABLE_META = {
        1: {
            tbody: '#itemTable1',
            empty: '#empty1',
            count: '#count1',
            pagination: '#paginationControls1',
            kpi: 'kpiWarehouse',
            url: '/api/items/',
            label: 'warehouse item',
            labelPlural: 'warehouse items'
        },
        3: {
            tbody: '#itemTable3',
            empty: '#empty3',
            count: '#count3',
            pagination: '#paginationControls3',
            kpi: 'kpiTransfers',
            url: '/api/transfer_items/',
            label: 'pending transfer',
            labelPlural: 'pending transfers'
        }
    };

    // The tables this page actually renders.
    const tablesOnPage = () => Object.keys(TABLE_META)
        .map(Number)
        .filter((number) => document.querySelector(TABLE_META[number].tbody));
    /* ==============================================================
       Table rendering
       ============================================================== */

    // Fetch items via AJAX and populate the corresponding table.
    const fetchItems = async (tableNum) => {
        const meta = TABLE_META[tableNum];
        if (!meta || !document.querySelector(meta.tbody)) return;
        try {
            await W.getEditLockStatus();

            const state = window.currentPageGlobals[tableNum - 1];
            const isManager = !!(window.userGroups && window.userGroups.includes("managers"));
            const isShopUser = !!(window.userGroups && window.userGroups.includes("shop_users"));
            // The stock table merges the rows that share an internal code.
            // Sorting by another column would separate them, so the merge is
            // only asked for while the table is in its grouped order. The
            // default `last_updated` is not a header the operator can sort by,
            // so it counts as "no chosen order" and keeps the groups together.
            const groupedMode = tableNum === 1 && (!state.sort || state.sort === 'last_updated');

            let apiUrl = meta.url;
            if (tableNum === 3 && isShopUser) {
                apiUrl += `?shop_user_username=${encodeURIComponent(window.loggedInUsername || '')}`;
            }
            const separator = apiUrl.includes('?') ? '&' : '?';
            apiUrl += `${separator}page=${state.page}&page_size=${window.recordsPerPage}&search=${encodeURIComponent(state.search)}`;
            if (state.sort && !groupedMode) {
                apiUrl += `&ordering=${state.order === 'asc' ? '' : '-'}${state.sort}`;
            }
            if (groupedMode) {
                apiUrl += '&grouped=1';
            }

            // Cancel any existing request for this table.
            if (currentFetchRequests[tableNum]) {
                currentFetchRequests[tableNum].abort();
            }

            // Make the AJAX call and store its jqXHR.
            const xhr = $.get(apiUrl, data => {
                const $tableBody = $(meta.tbody);
                $tableBody.empty();

                // Rowspan of the merged internal-code cell: the first row of a
                // group carries the span, the rows below it carry no code cell.
                const groupSpans = {};
                if (groupedMode) {
                    let current = null;
                    let start = 0;
                    data.results.forEach((row, index) => {
                        const key = row.internal_code || `sku:${row.sku}`;
                        if (key !== current) {
                            current = key;
                            start = index;
                            groupSpans[index] = { span: 1, total: row.group_total };
                        } else {
                            groupSpans[start].span += 1;
                        }
                    });
                }

                data.results.forEach((item, index) => {
                    let row = '';
                    if (tableNum === 1) {
                        const safeSku = W.escapeHtml(item.sku);
                        const safeInternal = W.escapeHtml(item.internal_code || '—');
                        const internalCell = groupedMode
                            ? (groupSpans[index]
                                ? `<td class="sap-table__group" rowspan="${groupSpans[index].span}" dir="ltr">${safeInternal}</td>`
                                : '')
                            : `<td class="sap-table__group" dir="ltr">${safeInternal}</td>`;
                        const totalCell = groupedMode
                            ? (groupSpans[index]
                                ? `<td class="sap-table__group sap-table__num" rowspan="${groupSpans[index].span}">${groupSpans[index].total}</td>`
                                : '')
                            : `<td class="sap-table__group sap-table__num">${item.group_total}</td>`;
                        const quantityField = isManager ?
                            `<input type="number" min="0" value="${item.quantity}" class="sap-input wh_editable-field" data-sku="${safeSku}" data-field="quantity">` :
                            item.quantity;
                        const retailPriceField = isManager ?
                            `<input type="number" min="0" step="0.01" value="${item.retail_price}" class="sap-input wh_editable-field" data-sku="${safeSku}" data-field="retail_price">` :
                            parseFloat(item.retail_price).toFixed(2);
                        const descriptionField = isManager ?
                            `<input type="text" value="${W.escapeHtml(item.description)}" class="sap-input wh_editable-field" data-sku="${safeSku}" data-field="description">` :
                            W.escapeHtml(item.description);
                        const unitSuffix = item.unit ? `<span class="sap-muted"> · ${W.escapeHtml(item.unit)}</span>` : '';
                        const skuField = isManager
                            ? `<input type="text" value="${safeSku}" class="sap-input wh_editable-field wh_sku-field" data-sku="${safeSku}" data-field="sku" dir="ltr" autocomplete="off">`
                            : safeSku;
                        const transferQuantityField = `<input type="number" min="0" class="sap-input xferQntField wh_editable-field" ${window.editLock ? 'disabled' : ''} value="" placeholder="${W.tr('How many?')}" data-sku="${safeSku}" data-field="xfer_qnt">`;
                        row = `
<tr class="${groupedMode && groupSpans[index] ? 'sap-table__groupstart' : ''}">
    ${internalCell}
    <td class="sap-table__sku">${skuField}</td>
    <td>${descriptionField}${unitSuffix}</td>
    <td class="sap-table__num">${retailPriceField}</td>
    <td class="sap-table__num">${quantityField}</td>
    ${totalCell}
    ${isShopUser ? `<td class="sap-table__num">${transferQuantityField}</td>` : ''}
    ${isManager ? `<td class="actions"></td>` : ''}
</tr>`;
                    } else if (tableNum === 3) {
                        const highlightClass = item.ordered && isShopUser ? 'highlight-ordered' : '';
                        const shop_user = item.shop_user && item.shop_user.username ? item.shop_user.username : window.loggedInUsername;
                        const safeSku = W.escapeHtml(item.item.sku);
                        const safeShopUser = W.escapeHtml(shop_user);
                        row = `
<tr class="${highlightClass}" data-ordered="${item.ordered}" data-sku="${safeSku}" data-quantity="${item.quantity}">
    ${isManager ? `<td>${safeShopUser}</td>` : ''}
    <td class="sap-table__sku">${safeSku}</td>
    <td>${W.escapeHtml(item.item.description)}</td>
    <td class="sap-table__num">${item.quantity}</td>
    <td class="actions">
    <button class="sap-btn sap-btn--sm sap-btn--negative cancel-btn" type="button" data-sku="${safeSku}" data-shop-user="${safeShopUser}" ${window.userGroups && !isManager && item.ordered ? 'disabled' : ''} id="deleteButton-${safeSku}">Cancel</button>
    </td>
</tr>`;
                    }
                    $tableBody.append(row);
                    if (tableNum === 1 && isManager) {
                        $tableBody.find('tr:last .actions').append(`
<button class="sap-btn sap-btn--sm sap-btn--negative delete-item-btn" type="button" data-sku="${W.escapeHtml(item.sku)}">${W.tr('Delete')}</button>
`);
                    }
                });

                // Empty-state panel + record counter + KPI tile.
                $(meta.empty).toggleClass('d-none', data.results.length > 0);
                const total = typeof data.count === 'number' ? data.count : data.results.length;
                $(meta.count).text(
                    total === 1 ? `1 ${W.tr(meta.label)}` : `${total} ${W.tr(meta.labelPlural)}`
                );
                const kpi = document.getElementById(meta.kpi);
                if (kpi && typeof data.count === 'number') {
                    kpi.textContent = Number(data.count).toLocaleString('en-US');
                }

                W.renderSortIndicators(tableNum);
                setupPagination(tableNum, data);
                W.markUpdated();
            });

            // Store the active request.
            currentFetchRequests[tableNum] = xhr;

        } catch (error) {
            console.error("Error in fetchItems:", error);
        }
    };

    // Build and attach pagination controls.
    const setupPagination = (tn, data) => {
        const meta = TABLE_META[tn];
        const $paginationDiv = $(meta.pagination);
        $paginationDiv.empty();

        const currentPage = data.current_page || 1;
        const totalPages = data.total_pages || 1;
        const total = typeof data.count === 'number' ? data.count : data.results.length;
        const perPage = Math.min(window.recordsPerPage || 25, 100);
        // Grouped pages hold whole internal codes, so the first/last row are
        // reported by the server rather than assumed from the page number.
        const firstRow = typeof data.first_row === 'number'
            ? data.first_row
            : (total === 0 ? 0 : (currentPage - 1) * perPage + 1);
        const lastRow = typeof data.last_row === 'number'
            ? data.last_row
            : (total === 0 ? 0 : firstRow + data.results.length - 1);

        const $info = $('<span class="sap-pagination__info"></span>').text(
            total === 0 ? W.tr('No records') :
                `${W.tr('Showing')} \u2066${firstRow}\u2013${lastRow}\u2069 ${W.tr('of')} ${total}`
        );

        const $controls = $('<div class="sap-pagination__controls"></div>');

        const addButton = (label, page, options) => {
            const opts = options || {};
            const $btn = $('<button type="button" class="sap-pagination__btn"></button>');
            if (opts.html) {
                $btn.html(label);
            } else {
                $btn.text(label);
            }
            if (opts.active) {
                $btn.addClass('is-active').attr('aria-current', 'page');
            }
            if (opts.disabled) {
                $btn.addClass('is-disabled').attr('disabled', 'disabled');
            }
            if (page !== null && !opts.disabled) {
                $btn.on('click', () => W.updatePageGlobals(tn, page, null, null, null));
            }
            $controls.append($btn);
        };

        const startPage = Math.floor((currentPage - 1) / 5) * 5 + 1;
        const endPage = Math.min(startPage + 4, totalPages);

        addButton('<svg class="sap-icon sap-icon--sm" aria-hidden="true"><use href="#i-chevron-left"></use></svg>', Math.max(1, startPage - 5), {
            html: true,
            disabled: currentPage <= 5
        });

        for (let i = startPage; i <= endPage; i++) {
            addButton(String(i), i, { active: i === currentPage });
        }

        addButton('<svg class="sap-icon sap-icon--sm" aria-hidden="true"><use href="#i-chevron-right"></use></svg>', startPage + 5, {
            html: true,
            disabled: endPage >= totalPages
        });

        if (total === 0) {
            $paginationDiv.append($info);
            return;
        }
        $paginationDiv.append($info).append($controls);
    };


    W.tablesOnPage = tablesOnPage;
    W.fetchItems = fetchItems;
    window.fetchItems = fetchItems;
})();
