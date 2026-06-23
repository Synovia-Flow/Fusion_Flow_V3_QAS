/* Fusion Flow V2 - shared operator UX helpers */

const flashSeen = new Set();
let confirmModal;
let confirmTargetForm = null;
let confirmTargetSubmitter = null;
const jobLoadingToasts = new WeakMap();
const formLoadingToasts = new WeakMap();
const SURFACE_TONE_KEY = 'fusion-surface-tone';

function flashIcon(category) {
    return {
        danger: 'bi-exclamation-triangle-fill',
        success: 'bi-check-circle-fill',
        warning: 'bi-exclamation-circle-fill',
        info: 'bi-info-circle-fill',
    }[category] || 'bi-bell-fill';
}

function flashTheme(category) {
    return {
        danger: 'text-bg-danger',
        success: 'text-bg-success',
        warning: 'text-bg-warning',
        info: 'text-bg-primary',
    }[category] || 'text-bg-secondary';
}

function makeToastCloseButton() {
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'btn-close btn-close-white fusion-toast-close';
    close.setAttribute('data-bs-dismiss', 'toast');
    close.setAttribute('aria-label', 'Close toast');
    close.setAttribute('title', 'Close');
    return close;
}

function pushToast(category, message, technicalUrl = '', technicalLabel = 'Technical') {
    const signature = `${category}:${message}:${technicalUrl}`;
    if (flashSeen.has(signature)) {
        return;
    }
    flashSeen.add(signature);

    const viewport = document.getElementById('toastViewport');
    if (!viewport || !message) {
        return;
    }

    const toast = document.createElement('div');
    toast.className = `toast align-items-center border-0 fusion-toast ${flashTheme(category)}`;
    toast.setAttribute('role', 'alert');
    toast.setAttribute('aria-live', 'assertive');
    toast.setAttribute('aria-atomic', 'true');

    const wrapper = document.createElement('div');
    wrapper.className = 'd-flex';

    const body = document.createElement('div');
    body.className = 'toast-body';

    const messageRow = document.createElement('div');
    messageRow.className = 'fusion-toast-message';

    const icon = document.createElement('i');
    icon.className = `bi ${flashIcon(category)} me-2`;
    messageRow.appendChild(icon);

    const text = document.createElement('span');
    text.textContent = message;
    messageRow.appendChild(text);
    body.appendChild(messageRow);

    if (technicalUrl) {
        const meta = document.createElement('div');
        meta.className = 'fusion-toast-meta';

        const link = document.createElement('a');
        link.className = 'fusion-toast-link';
        link.href = technicalUrl;
        link.textContent = technicalLabel || 'Technical';
        meta.appendChild(link);
        body.appendChild(meta);
    }

    const close = makeToastCloseButton();

    wrapper.appendChild(body);
    wrapper.appendChild(close);
    toast.appendChild(wrapper);

    viewport.appendChild(toast);
    const bsToast = bootstrap.Toast.getOrCreateInstance(toast, { delay: 5500 });
    bsToast.show();
    toast.addEventListener('hidden.bs.toast', () => toast.remove(), { once: true });
}

function hydrateFlashToasts(root = document) {
    root.querySelectorAll('.js-flash-seed').forEach((seed) => {
        pushToast(
            seed.dataset.category || 'info',
            seed.dataset.message || seed.textContent.trim(),
            seed.dataset.technicalUrl || '',
            seed.dataset.technicalLabel || 'Technical'
        );
        seed.remove();
    });
}

function findOrchestratorRunForm(elt) {
    const source = elt instanceof Element ? elt : null;
    const form = source instanceof HTMLFormElement ? source : source?.closest('form');
    if (!(form instanceof HTMLFormElement)) {
        return null;
    }

    const hxPost = form.getAttribute('hx-post') || form.getAttribute('data-hx-post') || '';
    return hxPost.includes('/orchestrate/run') ? form : null;
}

function jobLabelForForm(form) {
    const label = form.closest('.job-card')?.querySelector('.job-label')?.textContent?.trim();
    if (label) {
        return label;
    }
    const phase = form.querySelector('input[name="phase"]')?.value || '';
    return phase === 'all' ? 'Full Cargo Pipeline' : 'Job';
}

function showJobLoadingToast(form) {
    if (jobLoadingToasts.has(form)) {
        return;
    }

    const viewport = document.getElementById('toastViewport');
    if (!viewport || typeof bootstrap === 'undefined') {
        return;
    }

    const label = jobLabelForForm(form);
    const toast = document.createElement('div');
    toast.className = 'toast align-items-center border-0 fusion-toast text-bg-primary';
    toast.setAttribute('role', 'status');
    toast.setAttribute('aria-live', 'polite');
    toast.setAttribute('aria-atomic', 'true');

    const wrapper = document.createElement('div');
    wrapper.className = 'd-flex';

    const body = document.createElement('div');
    body.className = 'toast-body';

    const messageRow = document.createElement('div');
    messageRow.className = 'fusion-toast-message';
    messageRow.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>';

    const text = document.createElement('span');
    text.textContent = `Running ${label}...`;
    messageRow.appendChild(text);
    body.appendChild(messageRow);

    const meta = document.createElement('div');
    meta.className = 'fusion-toast-meta';
    meta.textContent = 'Please keep this page open while the job runs.';
    body.appendChild(meta);

    const close = makeToastCloseButton();

    wrapper.appendChild(body);
    wrapper.appendChild(close);
    toast.appendChild(wrapper);
    viewport.appendChild(toast);

    const submitButton = form.querySelector('button[type="submit"], button:not([type]), input[type="submit"]');
    const previousButtonHtml = submitButton instanceof HTMLButtonElement ? submitButton.innerHTML : '';
    if (submitButton) {
        submitButton.disabled = true;
        if (submitButton instanceof HTMLButtonElement) {
            submitButton.innerHTML = '<span class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span> Running...';
        }
    }

    const bsToast = bootstrap.Toast.getOrCreateInstance(toast, { autohide: false });
    jobLoadingToasts.set(form, { toast, bsToast, submitButton, previousButtonHtml });
    bsToast.show();
}

function hideJobLoadingToast(form) {
    const state = jobLoadingToasts.get(form);
    if (!state) {
        return;
    }

    if (state.submitButton) {
        state.submitButton.disabled = false;
        if (state.submitButton instanceof HTMLButtonElement && state.previousButtonHtml) {
            state.submitButton.innerHTML = state.previousButtonHtml;
        }
    }

    state.toast.addEventListener('hidden.bs.toast', () => state.toast.remove(), { once: true });
    state.bsToast.hide();
    setTimeout(() => state.toast.remove(), 500);
    jobLoadingToasts.delete(form);
}

function wireJobLoadingToasts() {
    document.addEventListener('htmx:beforeRequest', (event) => {
        const form = findOrchestratorRunForm(event.detail?.elt);
        if (form) {
            showJobLoadingToast(form);
        }
    });

    ['htmx:afterRequest', 'htmx:responseError', 'htmx:sendError', 'htmx:timeout'].forEach((eventName) => {
        document.addEventListener(eventName, (event) => {
            const form = findOrchestratorRunForm(event.detail?.elt);
            if (form) {
                hideJobLoadingToast(form);
            }
        });
    });
}

function loadingToastText(form, submitter, key, fallback) {
    if (submitter instanceof HTMLElement && submitter.dataset[key]) {
        return submitter.dataset[key];
    }
    return form.dataset[key] || fallback;
}

function loadingToastDefaults(form) {
    const action = String(form?.getAttribute('action') || '').toLowerCase();
    const phase = String(form?.querySelector('input[name="phase"]')?.value || '').toLowerCase();
    if (action.includes('sync') || phase.includes('sync')) {
        return {
            message: 'Syncing TSS data...',
            meta: 'Fusion is refreshing live TSS state for this record.',
            button: 'Syncing...',
        };
    }
    if (action.includes('ingest')) {
        return {
            message: 'Processing ingest request...',
            meta: 'Please keep this page open while Fusion prepares the next screen.',
            button: 'Working...',
        };
    }
    return {
        message: 'Processing request...',
        meta: 'Please keep this page open while Fusion completes the action.',
        button: 'Working...',
    };
}

function setMotionSubmitButtonLoading(button, buttonText) {
    button.classList.add('fusion-action-motion', 'is-motion-running');
    button.innerHTML = '';

    const lane = document.createElement('span');
    lane.className = 'fusion-action-motion-lane';
    lane.setAttribute('aria-hidden', 'true');

    const truck = document.createElement('span');
    truck.className = 'fusion-action-motion-truck';
    truck.innerHTML = '<span class="fusion-action-motion-cab"></span><span class="fusion-action-motion-box"></span><span class="fusion-action-motion-wheel fusion-action-motion-wheel-left"></span><span class="fusion-action-motion-wheel fusion-action-motion-wheel-right"></span>';
    lane.appendChild(truck);

    const label = document.createElement('span');
    label.className = 'fusion-action-motion-label';
    label.textContent = buttonText;

    button.appendChild(lane);
    button.appendChild(label);
}

function showFormLoadingToast(form, submitter = null) {
    if (formLoadingToasts.has(form)) {
        return;
    }

    const viewport = document.getElementById('toastViewport');
    if (!viewport || typeof bootstrap === 'undefined') {
        return;
    }

    const defaults = loadingToastDefaults(form);
    const message = loadingToastText(form, submitter, 'loadingMessage', defaults.message);
    const metaText = loadingToastText(form, submitter, 'loadingMeta', defaults.meta);
    const buttonText = loadingToastText(form, submitter, 'loadingButtonText', defaults.button);

    const toast = document.createElement('div');
    toast.className = 'toast align-items-center border-0 fusion-toast text-bg-primary';
    toast.setAttribute('role', 'status');
    toast.setAttribute('aria-live', 'polite');
    toast.setAttribute('aria-atomic', 'true');

    const wrapper = document.createElement('div');
    wrapper.className = 'd-flex';

    const body = document.createElement('div');
    body.className = 'toast-body';

    const messageRow = document.createElement('div');
    messageRow.className = 'fusion-toast-message';
    messageRow.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>';

    const text = document.createElement('span');
    text.textContent = message;
    messageRow.appendChild(text);
    body.appendChild(messageRow);

    const meta = document.createElement('div');
    meta.className = 'fusion-toast-meta';
    meta.textContent = metaText;
    body.appendChild(meta);

    const close = makeToastCloseButton();

    wrapper.appendChild(body);
    wrapper.appendChild(close);
    toast.appendChild(wrapper);
    viewport.appendChild(toast);

    const submitButton = submitter instanceof HTMLElement
        ? submitter
        : form.querySelector('button[type="submit"], button:not([type]), input[type="submit"]');
    const previousButtonHtml = submitButton instanceof HTMLButtonElement ? submitButton.innerHTML : '';
    const previousButtonValue = submitButton instanceof HTMLInputElement ? submitButton.value : '';
    const submitterCarriesPayload = submitButton instanceof HTMLElement
        && (submitButton.hasAttribute('name') || submitButton.hasAttribute('formaction') || submitButton.hasAttribute('formmethod'));
    if (submitButton) {
        submitButton.setAttribute('aria-busy', 'true');
        if (!submitterCarriesPayload && (submitButton instanceof HTMLButtonElement || submitButton instanceof HTMLInputElement)) {
            submitButton.disabled = true;
        }
        if (submitButton instanceof HTMLButtonElement && submitButton.matches('[data-motion-submit]')) {
            setMotionSubmitButtonLoading(submitButton, buttonText);
        } else if (submitButton instanceof HTMLButtonElement) {
            submitButton.innerHTML = '<span class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span> ' + buttonText;
        } else if (submitButton instanceof HTMLInputElement) {
            submitButton.value = buttonText;
        }
    }

    const bsToast = bootstrap.Toast.getOrCreateInstance(toast, { autohide: false });
    formLoadingToasts.set(form, { toast, bsToast, submitButton, previousButtonHtml, previousButtonValue });
    bsToast.show();
}

function hideFormLoadingToast(form) {
    const state = formLoadingToasts.get(form);
    if (!state) {
        return;
    }

    if (state.submitButton) {
        state.submitButton.removeAttribute('aria-busy');
        if (state.submitButton instanceof HTMLButtonElement || state.submitButton instanceof HTMLInputElement) {
            state.submitButton.disabled = false;
        }
        if (state.submitButton instanceof HTMLButtonElement && state.previousButtonHtml) {
            state.submitButton.innerHTML = state.previousButtonHtml;
            state.submitButton.classList.remove('fusion-action-motion', 'is-motion-running');
        } else if (state.submitButton instanceof HTMLInputElement) {
            state.submitButton.value = state.previousButtonValue;
        }
    }

    state.toast.addEventListener('hidden.bs.toast', () => state.toast.remove(), { once: true });
    state.bsToast.hide();
    setTimeout(() => state.toast.remove(), 500);
    formLoadingToasts.delete(form);
}

function wireIngestLoadingToasts() {
    document.addEventListener('submit', (event) => {
        const form = event.target;
        if (!(form instanceof HTMLFormElement) || !form.matches('[data-loading-toast]')) {
            return;
        }
        if (event.defaultPrevented || (typeof form.checkValidity === 'function' && !form.checkValidity())) {
            return;
        }
        showFormLoadingToast(form, event.submitter instanceof HTMLElement ? event.submitter : null);
    });

    window.addEventListener('pageshow', () => {
        document.querySelectorAll('form[data-loading-toast]').forEach((form) => {
            if (form instanceof HTMLFormElement) {
                hideFormLoadingToast(form);
            }
        });
    });

    document.body.addEventListener('htmx:afterRequest', (event) => {
        const elt = event.detail?.elt;
        const form = elt instanceof HTMLFormElement
            ? elt
            : (elt instanceof HTMLElement ? elt.closest('form[data-loading-toast]') : null);
        if (form instanceof HTMLFormElement) {
            hideFormLoadingToast(form);
        }
    });
}

function wireConfirmModal() {
    const modalEl = document.getElementById('confirmActionModal');
    if (!modalEl) {
        return;
    }
    confirmModal = bootstrap.Modal.getOrCreateInstance(modalEl);
    const titleEl = modalEl.querySelector('[data-confirm-title]');
    const textEl = modalEl.querySelector('[data-confirm-text]');
    const submitEl = modalEl.querySelector('[data-confirm-submit]');

    const acceptConfirm = () => {
        if (!confirmTargetForm) {
            return;
        }
        confirmTargetForm.dataset.confirmAccepted = '1';
        confirmModal.hide();
        if (typeof confirmTargetForm.requestSubmit === 'function') {
            if (confirmTargetSubmitter instanceof HTMLElement) {
                confirmTargetForm.requestSubmit(confirmTargetSubmitter);
            } else {
                confirmTargetForm.requestSubmit();
            }
        } else {
            HTMLFormElement.prototype.submit.call(confirmTargetForm);
        }
        confirmTargetForm = null;
        confirmTargetSubmitter = null;
    };

    document.addEventListener('submit', (event) => {
        const form = event.target;
        if (!(form instanceof HTMLFormElement)) {
            return;
        }

        const submitter = event.submitter instanceof HTMLElement ? event.submitter : null;
        const confirmSource = submitter?.matches('[data-confirm]')
            ? submitter
            : (form.matches('[data-confirm]') ? form : null);
        if (!confirmSource) {
            return;
        }
        if (form.dataset.confirmAccepted === '1') {
            delete form.dataset.confirmAccepted;
            confirmTargetSubmitter = null;
            return;
        }
        event.preventDefault();
        confirmTargetForm = form;
        confirmTargetSubmitter = submitter;
        titleEl.textContent = confirmSource.dataset.confirmTitle || 'Confirm Action';
        textEl.textContent = confirmSource.dataset.confirmText || 'Are you sure you want to continue?';
        submitEl.textContent = confirmSource.dataset.confirmSubmitLabel || 'Continue';
        confirmModal.show();
    });

    modalEl.addEventListener('shown.bs.modal', () => {
        submitEl.focus();
    });

    modalEl.addEventListener('hidden.bs.modal', () => {
        confirmTargetForm = null;
        confirmTargetSubmitter = null;
    });

    modalEl.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter' || event.isComposing) {
            return;
        }
        const target = event.target instanceof Element ? event.target : null;
        if (target && target.closest('[data-bs-dismiss="modal"]')) {
            return;
        }
        event.preventDefault();
        acceptConfirm();
    });

    submitEl.addEventListener('click', acceptConfirm);
}

function updateRefreshTimestamp() {
    const ts = document.getElementById('last-refresh');
    if (ts) {
        ts.textContent = 'Updated ' + new Date().toLocaleTimeString();
    }
}

function applySurfaceTone(mode) {
    const resolved = mode === 'dark' ? 'dark' : 'light';
    document.documentElement.dataset.surfaceTone = resolved;

    document.querySelectorAll('[data-surface-tone-toggle]').forEach((button) => {
        const isDark = resolved === 'dark';
        button.classList.toggle('active', isDark);
        button.setAttribute('aria-pressed', isDark ? 'true' : 'false');
        button.setAttribute('title', isDark ? 'Switch to light mode' : 'Switch to dark mode');
    });
}

function wireSurfaceToneToggle() {
    const buttons = document.querySelectorAll('[data-surface-tone-toggle]');
    if (!buttons.length) {
        return;
    }

    const storedTone = localStorage.getItem(SURFACE_TONE_KEY) || 'light';
    applySurfaceTone(storedTone);

    buttons.forEach((button) => {
        button.addEventListener('click', () => {
            const nextTone = document.documentElement.dataset.surfaceTone === 'dark' ? 'light' : 'dark';
            localStorage.setItem(SURFACE_TONE_KEY, nextTone);
            applySurfaceTone(nextTone);
        });
    });
}

function wireFilterTabs() {
    document.addEventListener('click', (event) => {
        const link = event.target.closest('.filter-tabs .nav-link');
        if (!(link instanceof HTMLAnchorElement)) {
            return;
        }

        const tabList = link.closest('.filter-tabs');
        if (!tabList) {
            return;
        }

        tabList.querySelectorAll('.nav-link').forEach((item) => {
            item.classList.remove('active');
            item.setAttribute('aria-selected', 'false');
        });

        link.classList.add('active');
        link.setAttribute('aria-selected', 'true');
    });
}

function wireClickableRows(root = document) {
    root.querySelectorAll('[data-row-href]').forEach((row) => {
        if (row.dataset.rowLinkBound === '1') {
            return;
        }

        row.dataset.rowLinkBound = '1';
        row.addEventListener('click', (event) => {
            const target = event.target instanceof Element ? event.target : null;
            const selectModeContainer = row.closest('[data-bulk-selection].is-bulk-selecting');
            if (selectModeContainer && !(target && target.closest('a, button, input, select, textarea, label, [data-no-row-link]'))) {
                const checkbox = row.querySelector('[data-bulk-item]');
                if (checkbox instanceof HTMLInputElement && !checkbox.disabled) {
                    event.preventDefault();
                    checkbox.checked = !checkbox.checked;
                    checkbox.dispatchEvent(new Event('change', { bubbles: true }));
                }
                return;
            }
            if (target && target.closest('a, button, input, select, textarea, label, [data-no-row-link]')) {
                return;
            }
            const href = row.dataset.rowHref;
            if (href) {
                window.location.href = href;
            }
        });

        row.addEventListener('keydown', (event) => {
            if (event.key !== 'Enter' && event.key !== ' ') {
                return;
            }
            const target = event.target instanceof Element ? event.target : null;
            const selectModeContainer = row.closest('[data-bulk-selection].is-bulk-selecting');
            if (selectModeContainer && !(target && target.closest('a, button, input, select, textarea, label, [data-no-row-link]'))) {
                const checkbox = row.querySelector('[data-bulk-item]');
                if (checkbox instanceof HTMLInputElement && !checkbox.disabled) {
                    event.preventDefault();
                    checkbox.checked = !checkbox.checked;
                    checkbox.dispatchEvent(new Event('change', { bubbles: true }));
                }
                return;
            }
            if (target && target.closest('a, button, input, select, textarea, label, [data-no-row-link]')) {
                return;
            }
            const href = row.dataset.rowHref;
            if (href) {
                event.preventDefault();
                window.location.href = href;
            }
        });
    });
}

function focusNextInlineGrossInput(input) {
    const inputs = Array.from(document.querySelectorAll('[data-inline-gross-input]'));
    const index = inputs.indexOf(input);
    const next = index >= 0 ? inputs[index + 1] : null;
    if (next instanceof HTMLInputElement) {
        next.focus();
        next.select();
    }
}

function wireInlineGrossInputs(root = document) {
    root.querySelectorAll('[data-inline-gross-input]').forEach((input) => {
        if (!(input instanceof HTMLInputElement) || input.dataset.inlineGrossBound === '1') {
            return;
        }

        const setState = (state) => {
            input.classList.toggle('sales-order-inline-invalid', state === 'error');
            input.classList.toggle('is-saving', state === 'saving');
        };

        const save = async (options = {}) => {
            const value = normalizeDecimalString(input.value, 2, false);
            input.value = value;
            const initialValue = String(input.dataset.initialValue || '').trim();
            if (value === initialValue) {
                if (options.focusNext) {
                    focusNextInlineGrossInput(input);
                }
                return;
            }
            if (!value || Number(value) <= 0) {
                setState('error');
                return;
            }

            setState('saving');
            try {
                const response = await fetch(input.dataset.inlineGrossUrl, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-Requested-With': 'fetch',
                    },
                    body: JSON.stringify({ gross_mass_kg: value }),
                });
                const data = await response.json().catch(() => ({}));
                if (!response.ok || !data.ok) {
                    throw new Error(data.message || 'Unable to save Gross KG');
                }

                input.value = data.gross_mass_kg || value;
                input.dataset.initialValue = input.value;
                input.title = data.message || '';
                setState('');

                const row = input.closest('tr');
                const badge = row ? row.querySelector('[data-inline-gross-status]') : null;
                if (badge && data.status_label) {
                    badge.className = data.status_class || badge.className;
                    badge.textContent = data.status_label;
                }
                if (row && data.status_label && data.status_label !== 'FAILED') {
                    row.classList.remove('row-danger');
                    const errorRow = row.nextElementSibling instanceof HTMLTableRowElement ? row.nextElementSibling : null;
                    if (errorRow && errorRow.querySelector('.text-danger')) {
                        errorRow.remove();
                    }
                }
                if (options.focusNext) {
                    focusNextInlineGrossInput(input);
                }
            } catch (error) {
                setState('error');
                input.title = error && error.message ? error.message : 'Unable to save Gross KG';
            }
        };

        input.value = normalizeDecimalString(input.value, 2, false);
        input.dataset.initialValue = normalizeDecimalString(input.dataset.initialValue || input.value, 2, false);
        input.dataset.inlineGrossBound = '1';
        input.addEventListener('click', (event) => event.stopPropagation());
        input.addEventListener('keydown', (event) => {
            event.stopPropagation();
            if (event.key === 'Enter') {
                event.preventDefault();
                save({ focusNext: true });
            }
        });
        input.addEventListener('change', () => save());
    });
}

function copyTextToClipboard(text) {
    if (navigator.clipboard && window.isSecureContext) {
        return navigator.clipboard.writeText(text);
    }

    const textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.setAttribute('readonly', '');
    textarea.style.position = 'fixed';
    textarea.style.left = '-9999px';
    document.body.appendChild(textarea);
    textarea.select();
    try {
        document.execCommand('copy');
        return Promise.resolve();
    } catch (error) {
        return Promise.reject(error);
    } finally {
        textarea.remove();
    }
}

function wireCopyButtons(root = document) {
    root.querySelectorAll('[data-copy-value]').forEach((button) => {
        if (!(button instanceof HTMLElement) || button.dataset.copyBound === '1') {
            return;
        }

        const originalTitle = button.getAttribute('title') || '';
        button.dataset.copyBound = '1';
        button.addEventListener('click', async (event) => {
            event.preventDefault();
            event.stopPropagation();

            try {
                await copyTextToClipboard(button.dataset.copyValue || '');
                button.setAttribute('title', 'Copied');
                button.classList.remove('text-muted');
                button.classList.add('text-success');
                window.setTimeout(() => {
                    button.setAttribute('title', originalTitle);
                    button.classList.add('text-muted');
                    button.classList.remove('text-success');
                }, 1200);
            } catch (error) {
                button.setAttribute('title', 'Copy failed');
                button.classList.remove('text-muted');
                button.classList.add('text-danger');
            }
        });
    });
}

function wireBulkSelection(root = document) {
    root.querySelectorAll('[data-bulk-selection]').forEach((container) => {
        if (!(container instanceof HTMLElement) || container.dataset.bulkSelectionBound === '1') {
            return;
        }

        const group = container.dataset.bulkSelection;
        if (!group) {
            return;
        }

        const selector = `[data-bulk-item][data-bulk-group="${group}"]`;
        const allItems = () => Array.from(container.querySelectorAll(selector))
            .filter((item) => item instanceof HTMLInputElement && item.type === 'checkbox');
        const selectAll = container.querySelector(`[data-bulk-select-all][data-bulk-group="${group}"]`);
        const actions = Array.from(container.querySelectorAll(`[data-bulk-action][data-bulk-group="${group}"]`));
        const toggle = container.querySelector(`[data-bulk-select-toggle][data-bulk-group="${group}"]`);
        const activeOnly = Array.from(container.querySelectorAll(`[data-bulk-active-only][data-bulk-group="${group}"]`));
        const countEl = container.querySelector(`[data-bulk-count][data-bulk-group="${group}"]`);
        const toggleLabel = toggle?.querySelector('[data-bulk-toggle-label]');
        let dragState = null;
        let lastPointerX = 0;
        let lastPointerY = 0;
        let clearClickSuppressionTimer = 0;
        let autoScrollFrame = 0;

        const isSelectMode = () => container.dataset.bulkSelectMode === '1';
        const itemForRow = (row) => {
            if (!(row instanceof HTMLElement)) {
                return null;
            }
            const item = row.querySelector(selector);
            return item instanceof HTMLInputElement && item.type === 'checkbox' ? item : null;
        };
        const rowForTarget = (target) => {
            if (!(target instanceof Element)) {
                return null;
            }
            const row = target.closest('tr');
            if (!row || !container.contains(row)) {
                return null;
            }
            return itemForRow(row) ? row : null;
        };
        const isIgnoredDragTarget = (target) => {
            if (!(target instanceof Element)) {
                return true;
            }
            if (target.closest('a, button, select, textarea, [data-no-row-link]')) {
                return true;
            }
            const input = target.closest('input');
            return input && !input.matches('[data-bulk-item]');
        };
        const clearClickSuppressionSoon = () => {
            window.clearTimeout(clearClickSuppressionTimer);
            clearClickSuppressionTimer = window.setTimeout(() => {
                delete container.dataset.bulkSuppressNextClick;
            }, 350);
        };
        const applyDragSelection = (row) => {
            if (!dragState) {
                return;
            }
            const item = itemForRow(row);
            if (!item || item.disabled || dragState.seen.has(item)) {
                return;
            }
            dragState.seen.add(item);
            if (item.checked !== dragState.targetChecked) {
                item.checked = dragState.targetChecked;
                item.dispatchEvent(new Event('change', { bubbles: true }));
            }
        };
        const stopBulkDrag = (event) => {
            if (!dragState || (event && event.pointerId !== dragState.pointerId)) {
                return;
            }
            if (event) {
                event.preventDefault();
                if (container.hasPointerCapture && container.hasPointerCapture(event.pointerId)) {
                    container.releasePointerCapture(event.pointerId);
                }
            }
            dragState = null;
            container.classList.remove('is-bulk-dragging');
            if (autoScrollFrame) {
                window.cancelAnimationFrame(autoScrollFrame);
                autoScrollFrame = 0;
            }
            clearClickSuppressionSoon();
        };
        const tickAutoScroll = () => {
            if (!dragState) {
                autoScrollFrame = 0;
                return;
            }
            const edge = 72;
            const maxSpeed = 18;
            let scrollDelta = 0;
            if (lastPointerY < edge) {
                scrollDelta = -Math.ceil(((edge - lastPointerY) / edge) * maxSpeed);
            } else if (window.innerHeight - lastPointerY < edge) {
                scrollDelta = Math.ceil(((edge - (window.innerHeight - lastPointerY)) / edge) * maxSpeed);
            }
            if (scrollDelta !== 0) {
                window.scrollBy(0, scrollDelta);
                const target = document.elementFromPoint(lastPointerX, lastPointerY);
                const row = rowForTarget(target);
                if (row) {
                    applyDragSelection(row);
                }
            }
            autoScrollFrame = window.requestAnimationFrame(tickAutoScroll);
        };
        const startBulkDrag = (event) => {
            if (!isSelectMode() || (event.pointerType === 'mouse' && event.button !== 0)) {
                return;
            }
            const target = event.target instanceof Element ? event.target : null;
            if (!target || isIgnoredDragTarget(target)) {
                return;
            }
            const row = rowForTarget(target);
            const item = itemForRow(row);
            if (!row || !item || item.disabled) {
                return;
            }
            event.preventDefault();
            window.clearTimeout(clearClickSuppressionTimer);
            container.dataset.bulkSuppressNextClick = '1';
            dragState = {
                pointerId: event.pointerId,
                targetChecked: !item.checked,
                seen: new Set(),
            };
            lastPointerX = event.clientX;
            lastPointerY = event.clientY;
            container.classList.add('is-bulk-dragging');
            if (container.setPointerCapture) {
                container.setPointerCapture(event.pointerId);
            }
            applyDragSelection(row);
            if (!autoScrollFrame) {
                autoScrollFrame = window.requestAnimationFrame(tickAutoScroll);
            }
        };
        const continueBulkDrag = (event) => {
            if (!dragState || event.pointerId !== dragState.pointerId) {
                return;
            }
            event.preventDefault();
            lastPointerX = event.clientX;
            lastPointerY = event.clientY;
            const target = document.elementFromPoint(lastPointerX, lastPointerY);
            const row = rowForTarget(target);
            if (row) {
                applyDragSelection(row);
            }
        };

        const setSelectMode = (enabled) => {
            container.dataset.bulkSelectMode = enabled ? '1' : '0';
            container.classList.toggle('is-bulk-selecting', enabled);
            if (!enabled) {
                stopBulkDrag();
                allItems().forEach((item) => {
                    item.checked = false;
                    item.closest('tr')?.classList.remove('bulk-selected-row');
                });
            }
            update();
        };

        const update = () => {
            const active = isSelectMode();
            const items = allItems();
            const checked = items.filter((item) => item.checked);
            const count = checked.length;

            items.forEach((item) => {
                item.disabled = !active;
                item.closest('tr')?.classList.toggle('bulk-selected-row', active && item.checked);
            });
            actions.forEach((action) => {
                if (action instanceof HTMLButtonElement || action instanceof HTMLInputElement) {
                    action.disabled = !active || count === 0;
                }
            });
            activeOnly.forEach((element) => {
                if (element instanceof HTMLElement) {
                    element.hidden = !active;
                }
            });
            if (countEl) {
                countEl.textContent = String(count);
            }
            if (toggle instanceof HTMLElement) {
                toggle.classList.toggle('active', active);
                if (!toggle.classList.contains('bulk-mode-toggle')) {
                    toggle.classList.toggle('btn-primary', active);
                    toggle.classList.toggle('btn-outline-primary', !active);
                }
                toggle.setAttribute('aria-pressed', active ? 'true' : 'false');
            }
            if (toggleLabel) {
                toggleLabel.textContent = active
                    ? (toggle?.dataset.bulkActiveLabel || 'Exit select')
                    : (toggle?.dataset.bulkInactiveLabel || 'Select mode');
            }
            if (selectAll instanceof HTMLInputElement) {
                selectAll.checked = items.length > 0 && count === items.length;
                selectAll.indeterminate = count > 0 && count < items.length;
                selectAll.disabled = !active || items.length === 0;
            }
        };

        if (toggle instanceof HTMLElement) {
            toggle.addEventListener('click', () => {
                setSelectMode(!isSelectMode());
            });
        }
        if (selectAll instanceof HTMLInputElement) {
            selectAll.addEventListener('change', () => {
                allItems().forEach((item) => {
                    item.checked = selectAll.checked;
                });
                update();
            });
        }

        container.addEventListener('click', (event) => {
            if (container.dataset.bulkSuppressNextClick === '1') {
                event.preventDefault();
                event.stopPropagation();
                delete container.dataset.bulkSuppressNextClick;
            }
        }, true);
        container.addEventListener('pointerdown', startBulkDrag);
        container.addEventListener('pointermove', continueBulkDrag);
        container.addEventListener('pointerup', stopBulkDrag);
        container.addEventListener('pointercancel', stopBulkDrag);
        allItems().forEach((item) => item.addEventListener('change', update));
        container.dataset.bulkSelectionBound = '1';
        setSelectMode(false);
    });
}

function normalizeDecimalString(value, scale = 2, preserveScale = false) {
    if (value == null) {
        return '';
    }
    const raw = String(value).trim();
    if (!raw) {
        return '';
    }

    const num = Number(raw);
    if (!Number.isFinite(num)) {
        return raw;
    }

    const fixed = num.toFixed(scale);
    return preserveScale ? fixed : fixed.replace(/(\.\d*?)0+$/, '$1').replace(/\.$/, '');
}

function wireDecimalScaleInputs(root = document) {
    root.querySelectorAll('input[data-decimal-scale]').forEach((input) => {
        if (!(input instanceof HTMLInputElement) || input.dataset.decimalScaleBound === '1') {
            return;
        }

        const scale = Number(input.dataset.decimalScale || '2');
        const preserveScale = input.dataset.decimalFixed === '1';
        const normalize = () => {
            if (!input.value) {
                return;
            }
            input.value = normalizeDecimalString(input.value, scale, preserveScale);
        };

        input.dataset.decimalScaleBound = '1';
        normalize();
        input.addEventListener('blur', normalize);
        input.addEventListener('change', normalize);
    });
}

function sortableTextValue(value) {
    return String(value || '').trim().toLocaleLowerCase();
}

function sortableNumberValue(value) {
    const normalized = String(value || '').replace(/[^0-9.\-]/g, '');
    const parsed = Number(normalized);
    return Number.isFinite(parsed) ? parsed : Number.NEGATIVE_INFINITY;
}

function sortableDateValue(value) {
    const raw = String(value || '').trim();
    if (!raw) {
        return Number.NEGATIVE_INFINITY;
    }

    const parsed = Date.parse(raw);
    if (Number.isFinite(parsed)) {
        return parsed;
    }

    const match = raw.match(/^(\d{1,2})\/(\d{1,2})(?:\/(\d{2,4}))?(?:\s+(\d{1,2}):(\d{2}))?/);
    if (!match) {
        return Number.NEGATIVE_INFINITY;
    }

    const day = Number(match[1]);
    const month = Number(match[2]) - 1;
    let year = match[3] ? Number(match[3]) : new Date().getFullYear();
    if (year < 100) {
        year += 2000;
    }
    const hour = match[4] ? Number(match[4]) : 0;
    const minute = match[5] ? Number(match[5]) : 0;
    const built = Date.UTC(year, month, day, hour, minute);
    return Number.isFinite(built) ? built : Number.NEGATIVE_INFINITY;
}

function sortableCellValue(cell, type) {
    const raw = cell?.dataset.sortValue ?? cell?.textContent ?? '';
    if (type === 'number') {
        return sortableNumberValue(raw);
    }
    if (type === 'date') {
        return sortableDateValue(raw);
    }
    return sortableTextValue(raw);
}

function wireSortableTables(root = document) {
    root.querySelectorAll('table[data-sortable-table]').forEach((table) => {
        if (!(table instanceof HTMLTableElement) || table.dataset.sortableBound === '1') {
            return;
        }

        const headers = Array.from(table.querySelectorAll('thead th[data-sort-key]'));
        const tbody = table.tBodies && table.tBodies.length ? table.tBodies[0] : null;
        if (!headers.length || !tbody) {
            return;
        }

        const clearSortIndicators = (activeHeader) => {
            headers.forEach((header) => {
                if (header !== activeHeader) {
                    header.setAttribute('aria-sort', 'none');
                    delete header.dataset.sortDirection;
                }
            });
        };

        const sortByHeader = (header) => {
            const columnIndex = Array.from(header.parentElement.children).indexOf(header);
            if (columnIndex < 0) {
                return;
            }

            const nextDirection = header.dataset.sortDirection === 'asc' ? 'desc' : 'asc';
            const multiplier = nextDirection === 'asc' ? 1 : -1;
            const sortType = header.dataset.sortType || 'text';
            const rows = Array.from(tbody.querySelectorAll('tr'))
                .map((row, index) => ({ row, index }))
                .filter(({ row }) => row.children.length > columnIndex && !row.querySelector('td[colspan]'));

            rows.sort((left, right) => {
                const leftValue = sortableCellValue(left.row.children[columnIndex], sortType);
                const rightValue = sortableCellValue(right.row.children[columnIndex], sortType);

                if (sortType === 'text') {
                    const compared = String(leftValue).localeCompare(String(rightValue), undefined, {
                        numeric: true,
                        sensitivity: 'base',
                    });
                    return compared === 0 ? left.index - right.index : compared * multiplier;
                }

                if (leftValue === rightValue) {
                    return left.index - right.index;
                }
                return leftValue > rightValue ? multiplier : -multiplier;
            });

            clearSortIndicators(header);
            header.dataset.sortDirection = nextDirection;
            header.setAttribute('aria-sort', nextDirection === 'asc' ? 'ascending' : 'descending');
            rows.forEach(({ row }) => tbody.appendChild(row));
        };

        headers.forEach((header) => {
            const currentSort = header.getAttribute('aria-sort') || 'none';
            header.setAttribute('aria-sort', currentSort);
            if (currentSort === 'ascending') {
                header.dataset.sortDirection = 'asc';
            } else if (currentSort === 'descending') {
                header.dataset.sortDirection = 'desc';
            }
            header.addEventListener('click', () => sortByHeader(header));
            header.addEventListener('keydown', (event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    sortByHeader(header);
                }
            });
        });

        table.dataset.sortableBound = '1';
    });
}

document.addEventListener('DOMContentLoaded', () => {
    hydrateFlashToasts(document);
    wireConfirmModal();
    wireSurfaceToneToggle();
    wireFilterTabs();
    wireJobLoadingToasts();
    wireIngestLoadingToasts();
    wireClickableRows(document);
    wireInlineGrossInputs(document);
    wireCopyButtons(document);
    wireBulkSelection(document);
    wireDecimalScaleInputs(document);
    wireSortableTables(document);
    updateRefreshTimestamp();
});

document.addEventListener('htmx:afterSwap', (event) => {
    hydrateFlashToasts(event.target || document);
    wireClickableRows(event.target || document);
    wireInlineGrossInputs(event.target || document);
    wireCopyButtons(event.target || document);
    wireBulkSelection(event.target || document);
    wireDecimalScaleInputs(event.target || document);
    wireSortableTables(event.target || document);
    updateRefreshTimestamp();
});
