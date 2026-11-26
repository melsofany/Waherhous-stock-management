/*
 * Shared warehouse page runtime: state, AJAX helpers, notifications, the app
 * configuration and edit lock, paging/sorting, the transfer cache and the
 * role tailoring both pages use.
 *
 * Page-specific behaviour lives in warehouse-items.js and
 * warehouse-dashboard.js; the table renderer lives in warehouse-tables.js.
 */
(() => {
    'use strict';

    const W = window.Warehouse = window.Warehouse || {};

    /* ==============================================================
       State
       ============================================================== */

    // Global variable to hold the edit lock status.
    window.editLock = false;

    // Translations for the runtime messages, keyed by the English text.
    const source = document.getElementById('i18n-js');
    const I18N = source ? JSON.parse(source.textContent) : {};
    const tr = (text) => I18N[text] || text;

    // Global variable for records per page (default fallback).
    window.recordsPerPage = 25;

    // Globals to hold page, sort column & sort order.
    window.currentPageGlobals = [{
        page: 1,
        sort: 'last_updated',
        order: '',
        search: ''
    }, {
        page: 1,
        sort: 'last_updated',
        order: '',
        search: ''
    }, {
        page: 1,
        sort: 'last_updated',
        order: '',
        search: ''
    }];

    // Global cache for transfer quantities.
    window.cache = {
        'xfer_qnt': new Map()
    };

    /* ==============================================================
       Helpers
       ============================================================== */

    // Escape server-supplied values before interpolating them into HTML.
    // Covers element-content and quoted-attribute contexts so stored data
    // (e.g. item SKU / description) cannot inject markup or break out of an
    // attribute (XSS).
    const escapeHtml = (value) => {
        if (value === null || value === undefined) return '';
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    };

    // Get CSRF token from cookies.
    const getCSRFToken = () => {
        return document.cookie.split('; ')
            .find(row => row.startsWith('csrftoken='))?.split('=')[1];
    };

    // Ensure CSRF token is included in all AJAX requests.
    $.ajaxSetup({
        beforeSend: function (xhr, settings) {
            xhr.setRequestHeader("X-CSRFToken", getCSRFToken());
        }
    });

    // Extract a human-readable message from a failed AJAX response.
    const errorDetail = (xhr, fallback) => {
        let body = xhr && xhr.responseJSON;
        if (!body && xhr && xhr.responseText) {
            try {
                body = JSON.parse(xhr.responseText);
            } catch (e) {
                body = null;
            }
        }
        if (typeof body === 'string' && body.trim()) return body;
        if (body && typeof body === 'object') {
            if (typeof body.detail === 'string') return body.detail;
            if (typeof body.error === 'string') return body.error;
            const first = Object.keys(body).map(k => body[k])[0];
            if (Array.isArray(first) && first.length) return String(first[0]);
            if (typeof first === 'string') return first;
        }
        return fallback || tr('An error occurred.');
    };

    /* ==============================================================
       SAP-style message toast and confirmation dialog
       ============================================================== */

    const TOAST_ICONS = {
        info: '#i-info',
        success: '#i-check',
        error: '#i-x-circle',
        warning: '#i-alert'
    };

    const notify = (message, variant) => {
        const tone = variant || 'info';
        const stack = document.getElementById('toastStack');
        if (!stack) {
            window.alert(message);
            return null;
        }
        const toast = document.createElement('div');
        toast.className = 'sap-toast sap-toast--' + tone;
        toast.setAttribute('role', tone === 'error' ? 'alert' : 'status');

        const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        icon.setAttribute('class', 'sap-icon sap-toast__icon');
        icon.setAttribute('aria-hidden', 'true');
        const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
        use.setAttribute('href', TOAST_ICONS[tone] || TOAST_ICONS.info);
        icon.appendChild(use);

        const body = document.createElement('div');
        body.className = 'sap-toast__body';
        body.textContent = message === null || message === undefined ? '' : String(message);

        const close = document.createElement('button');
        close.type = 'button';
        close.className = 'sap-toast__close';
        close.setAttribute('aria-label', tr('Dismiss notification'));
        close.textContent = '\u2715';

        const dismiss = () => {
            window.clearTimeout(timer);
            toast.classList.add('is-leaving');
            window.setTimeout(() => toast.remove(), 200);
        };
        close.addEventListener('click', dismiss);
        const timer = window.setTimeout(dismiss, tone === 'error' ? 9000 : 5000);

        toast.appendChild(icon);
        toast.appendChild(body);
        toast.appendChild(close);
        stack.appendChild(toast);
        return toast;
    };

    // Promise-based replacement for window.confirm().
    const confirmDialog = (options) => new Promise((resolve) => {
        const opts = options || {};
        const previouslyFocused = document.activeElement;
        const backdrop = document.createElement('div');
        backdrop.className = 'sap-dialog-backdrop';
        backdrop.innerHTML = `
<div class="sap-dialog" role="dialog" aria-modal="true" aria-labelledby="sapDialogTitle">
    <div class="sap-dialog__header">
        <span class="sap-dialog__icon">
            <svg class="sap-icon" aria-hidden="true"><use href="#i-alert"></use></svg>
        </span>
        <h2 class="sap-dialog__title" id="sapDialogTitle"></h2>
    </div>
    <div class="sap-dialog__body"></div>
    <div class="sap-dialog__footer">
        <button type="button" class="sap-btn sap-btn--transparent" data-dialog-cancel></button>
        <button type="button" class="sap-btn sap-btn--emphasized" data-dialog-confirm></button>
    </div>
</div>`;

        backdrop.querySelector('.sap-dialog__title').textContent =
            opts.title || tr('Please confirm');
        backdrop.querySelector('.sap-dialog__body').textContent = opts.message || '';

        const cancelBtn = backdrop.querySelector('[data-dialog-cancel]');
        const confirmBtn = backdrop.querySelector('[data-dialog-confirm]');
        cancelBtn.textContent = opts.cancelLabel || 'Cancel';
        confirmBtn.textContent = opts.confirmLabel || 'Continue';
        if (opts.destructive) {
            confirmBtn.className = 'sap-btn sap-btn--negative-emphasized';
        }

        const close = (result) => {
            document.removeEventListener('keydown', onKey);
            backdrop.remove();
            if (previouslyFocused && previouslyFocused.focus) {
                previouslyFocused.focus();
            }
            resolve(result);
        };
        const onKey = (event) => {
            if (event.key === 'Escape') {
                event.preventDefault();
                close(false);
            } else if (event.key === 'Enter') {
                event.preventDefault();
                close(true);
            }
        };
        cancelBtn.addEventListener('click', () => close(false));
        confirmBtn.addEventListener('click', () => close(true));
        backdrop.addEventListener('click', (event) => {
            if (event.target === backdrop) close(false);
        });
        document.addEventListener('keydown', onKey);
        document.body.appendChild(backdrop);
        confirmBtn.focus();
    });

    /* ==============================================================
       Configuration & lock status
       ============================================================== */

    // Fetch app config (records_per_page) from backend.
    const fetchAppConfig = () => {
        return $.get('/api/app_config/', data => {
            if (data.records_per_page) {
                window.recordsPerPage = data.records_per_page;
            }
            // Store allow_upload_deletions in a global variable.
            window.allowUploadDeletions = !!data.allow_upload_deletions;
        });
    };

    // Set edit lock status on the server.
    const setEditLockStatus = (status) => {
        if (window.userGroups && window.userGroups.includes("managers")) {
            $.ajax({
                url: '/api/set_edit_lock_status/',
                type: 'POST',
                contentType: 'application/json',
                data: JSON.stringify({ edit_lock_status: status }),
                success: response => {
                    window.editLock = response.edit_lock;
                    updateUIBasedOnEditLock();
                    W.fetchAllTables();
                }
            });
        }
    };

    // Get the edit lock status from the server.
    const getEditLockStatus = () => {
        return new Promise((resolve, reject) => {
            $.ajax({
                url: '/api/get_edit_lock_status/',
                type: 'GET',
                success: response => {
                    window.editLock = response.edit_lock;
                    updateUIBasedOnEditLock();
                    resolve();
                },
                error: xhr => {
                    console.error("Failed to fetch edit lock status:", xhr.responseText);
                    reject(xhr);
                }
            });
        });
    };

    // Reflect the lock state in the banner and the maintenance switch.
    const updateUIBasedOnEditLock = () => {
        const isLocked = window.editLock;
        $('#itemTable1 input, #itemTable1 button').prop('disabled', isLocked);
        $('#updateModeToggle').prop('checked', isLocked);
        $('.maintenanceMode').toggleClass('d-none', !isLocked);
    };

    /* ==============================================================
       Paging / sorting / search
       ============================================================== */

    // Update global page variables and trigger a new fetch.
    const updatePageGlobals = (tableNum, page = null, sort = null, order = null, search = null) => {
        if (page !== null) {
            window.currentPageGlobals[tableNum - 1]['page'] = page;
        }
        if (sort !== null) {
            window.currentPageGlobals[tableNum - 1]['sort'] = sort;
        }
        if (order !== null) {
            window.currentPageGlobals[tableNum - 1]['order'] = order;
        }
        if (search !== null) {
            window.currentPageGlobals[tableNum - 1]['search'] = search;
        }
        W.fetchItems(tableNum);
    };

    // Search function for tables.
    const search = (tableNum, query) => {
        updatePageGlobals(tableNum, 1, null, null, query);
    };

    // Sorting functionality.
    const sortTable = (tableNum, column) => {
        const $table = $(`#itemTable${tableNum}`);
        const currentOrder = $table.data('sortOrder') || 'asc';
        const newOrder = currentOrder === 'asc' ? 'desc' : 'asc';
        $table.data('sortOrder', newOrder).data('sortColumn', column);
        const searchVal = $(`#searchBox${tableNum}`).val();
        updatePageGlobals(tableNum, 1, column, newOrder, searchVal);
    };

    // Paint the sort direction on the column headers.
    const renderSortIndicators = (tableNum) => {
        const state = window.currentPageGlobals[tableNum - 1];
        const $table = $(`#itemTable${tableNum}`).closest('table');
        $table.find('thead th[data-sort-col]').each(function () {
            const $th = $(this);
            const column = $th.attr('data-sort-col');
            const $indicator = $th.find('.sap-sort');
            if (state.sort === column && state.order) {
                $th.attr('aria-sort', state.order === 'asc' ? 'ascending' : 'descending');
                if ($indicator.length) {
                    $indicator.text(state.order === 'asc' ? '\u25B2' : '\u25BC');
                }
            } else {
                $th.attr('aria-sort', 'none');
                if ($indicator.length) $indicator.text('\u2195');
            }
        });
    };

    // Show the time of the last successful refresh.
    const markUpdated = () => {
        const el = document.getElementById('lastUpdated');
        if (el) {
            el.textContent = ' \u00b7 ' + tr('Updated') + ' ' + new Date().toLocaleTimeString();
        }
    };

    /* ==============================================================
       Transfer quantity cache
       ============================================================== */

    // Set nested cache map values for transfer quantities.
    const setXferVals = (shop_user, sku, qty) => {
        const map = window.cache.xfer_qnt;
        if (!map.has(shop_user)) {
            map.set(shop_user, new Map());
        }
        map.get(shop_user).set(sku, qty);
    };

    // Get nested cache map values for transfer quantities.
    const getXferVals = (shop_user, sku) => {
        return window.cache.xfer_qnt.get(shop_user)?.get(sku);
    };

    // Delete transfer quantity values from cache.
    const deleteXferVals = (shop_user, sku) => {
        const map = window.cache.xfer_qnt;
        if (!map.has(shop_user)) return;
        const userMap = map.get(shop_user);
        userMap.delete(sku);
        if (userMap.size === 0) {
            map.delete(shop_user);
        }
    };

    /* ==============================================================
       Role tailoring
       ============================================================== */

    // Show the controls this reader may use. The shell (name, role, menu) is
    // rendered by the server, so only the page's own controls are tailored.
    const tailor_dashboard = () => {
        const groups = window.userGroups || [];
        const isManager = groups.includes("managers");
        const isShopUser = groups.includes("shop_users");

        if (isManager) {
            setEditLockStatus(false);
            $("#addItemSection, #updateModeSwitch").removeClass("d-none");
            $("#downloadStockData, #uploadStockData, #uploadStockDataButton, #importPanel").removeClass("d-none");
            $("#refreshWarehouse").removeClass("d-none");
        } else {
            $("#importPanel").addClass("d-none");
            if (isShopUser) {
                $("#submitTransferRequest").removeClass("d-none");
                $("#refreshWarehouse").removeClass("d-none");
                $("#downloadStockData").removeClass("d-none");
            }
        }
        if (!isShopUser) {
            $("th#whXferQnt").hide();
        }
        if (isShopUser) {
            $("th#whActions").hide();
        }
        updateUIBasedOnEditLock();
    };

    // Learn who is reading, then draw the tables this page has.
    const loadViewer = () => {
        return $.ajax({
            url: '/api/auth/user/',
            method: 'GET',
            xhrFields: { withCredentials: true },
            success: data => {
                window.loggedInUsername = data.username || '';
                window.userGroups = data.groups || [];
                tailor_dashboard();
                W.fetchAllTables();
            },
            error: () => {
                window.userGroups = [];
                tailor_dashboard();
                W.fetchAllTables();
            }
        });
    };

    /* ==============================================================
       Exports
       ============================================================== */

    W.tr = tr;
    W.escapeHtml = escapeHtml;
    W.errorDetail = errorDetail;
    W.notify = notify;
    W.confirmDialog = confirmDialog;
    W.fetchAppConfig = fetchAppConfig;
    W.setEditLockStatus = setEditLockStatus;
    W.getEditLockStatus = getEditLockStatus;
    W.updateUIBasedOnEditLock = updateUIBasedOnEditLock;
    W.updatePageGlobals = updatePageGlobals;
    W.search = search;
    W.sortTable = sortTable;
    W.renderSortIndicators = renderSortIndicators;
    W.markUpdated = markUpdated;
    W.setXferVals = setXferVals;
    W.getXferVals = getXferVals;
    W.deleteXferVals = deleteXferVals;
    W.tailor_dashboard = tailor_dashboard;
    W.loadViewer = loadViewer;
    W.fetchAllTables = () => W.tablesOnPage().forEach((tn) => W.fetchItems(tn));

    // Inline handlers in the markup call these names.
    window.sortTable = sortTable;
    window.search = search;
    window.updatePageGlobals = updatePageGlobals;
    window.notify = notify;
    window.tailor_dashboard = tailor_dashboard;

    $(document).ready(() => {
        W.fetchAppConfig().always(() => W.loadViewer());
    });
})();
