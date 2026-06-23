/**
 * Quick-fill consignment party fields from saved master data records.
 */
(function () {
  function byName(name) {
    return document.querySelector('[name="' + name + '"]');
  }

  function truncateForField(field, value) {
    const text = String(value || '').trim();
    if (!field || !field.maxLength || field.maxLength < 1) return text;
    return text.slice(0, field.maxLength);
  }

  function setFieldValue(name, value) {
    const field = byName(name);
    if (!field) return;

    const nextValue = truncateForField(field, value);
    if (field.tomselect) {
      if (nextValue && !field.tomselect.options[nextValue]) {
        field.tomselect.addOption({ value: nextValue, text: nextValue });
      }
      field.tomselect.setValue(nextValue, true);
    } else {
      field.value = nextValue;
    }
    field.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function applyPartyMasterData(role, data) {
    const prefix = role === 'buyer' || role === 'seller' ? role : role;
    const streetField = role === 'buyer' || role === 'seller'
      ? prefix + '_street_and_number'
      : prefix + '_street_number';

    if (role === 'buyer') {
      setFieldValue('buyer_same_as_importer', 'no');
      if (typeof window.toggleBuyer === 'function') window.toggleBuyer();
    }
    if (role === 'seller') {
      setFieldValue('seller_same_as_exporter', 'no');
      if (typeof window.toggleSeller === 'function') window.toggleSeller();
    }
    if (['consignor', 'consignee', 'importer', 'exporter'].includes(role)) {
      const hasAddress = Boolean(data.name || data.street || data.city || data.postcode || data.country);
      setFieldValue(role + '_address_required', hasAddress ? 'true' : 'false');
    }

    setFieldValue(prefix + '_eori', data.eori || '');
    setFieldValue(prefix + '_name', data.name || '');
    setFieldValue(streetField, data.street || '');
    setFieldValue(prefix + '_city', data.city || '');
    setFieldValue(prefix + '_postcode', data.postcode || '');
    setFieldValue(prefix + '_country', (data.country || '').toUpperCase());
    if (['consignor', 'consignee', 'importer', 'exporter'].includes(role)
        && typeof window.syncPartyAddressRequired === 'function') {
      window.syncPartyAddressRequired(role);
    }
  }

  function initialiseMasterDataSelects() {
    document.querySelectorAll('.party-master-select').forEach(function (select) {
      select.addEventListener('change', function () {
        const option = select.selectedOptions && select.selectedOptions[0];
        if (!option || !option.value) return;
        applyPartyMasterData(select.dataset.partyRole, option.dataset || {});
      });

      if (window.TomSelect && !select.tomselect) {
        new TomSelect(select, {
          maxOptions: 250,
          placeholder: 'Search master data...',
          onItemAdd: function () { this.blur(); }
        });
      }
    });
  }

  window.initialiseMasterDataSelects = initialiseMasterDataSelects;
  document.addEventListener('DOMContentLoaded', initialiseMasterDataSelects);
})();
