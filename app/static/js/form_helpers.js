/**
 * Synovia Flow - BKD Portal: Form Helper Functions
 * Shared JS utilities for consignment and goods forms.
 */

const tssTextReplacements = {
  '\u00bd': '1/2'
};

function getTssUnsafeCharacters(value) {
  const chars = [];
  const seen = new Set();
  for (const char of String(value || '')) {
    if (!(char in tssTextReplacements)) continue;
    if (seen.has(char)) continue;
    seen.add(char);
    chars.push(char);
  }
  return chars;
}

function formatTssUnsafeCharacter(char) {
  const code = char.charCodeAt(0).toString(16).toUpperCase().padStart(4, '0');
  if (char === '\r') return "'\\r' (U+" + code + ')';
  if (char === '\n') return "'\\n' (U+" + code + ')';
  if (char === '\t') return "'\\t' (U+" + code + ')';
  return "'" + char + "' (U+" + code + ')';
}

function buildTssSafeTextSuggestion(value) {
  let text = String(value || '');
  Object.entries(tssTextReplacements).forEach(function ([source, replacement]) {
    text = text.split(source).join(replacement);
  });
  return text;
}

function ensureTssCharFeedback(input) {
  const parent = input.parentNode;
  if (!parent) return null;
  let tip = parent.querySelector('.tss-char-feedback[data-tss-char-for="' + input.name + '"]');
  if (tip) return tip;
  tip = document.createElement('div');
  tip.className = 'field-fix-tip mt-1 tss-char-feedback d-none';
  tip.dataset.tssCharFor = input.name;
  parent.appendChild(tip);
  return tip;
}

function updateTssCharFeedback(input) {
  const tip = ensureTssCharFeedback(input);
  if (!tip) return;
  const chars = getTssUnsafeCharacters(input.value);
  if (!chars.length) {
    input.classList.remove('tss-char-needs-fix');
    tip.classList.add('d-none');
    tip.textContent = '';
    return;
  }

  const formatted = chars.slice(0, 4).map(formatTssUnsafeCharacter).join(', ');
  const extra = chars.length > 4 ? ' (+' + (chars.length - 4) + ' more)' : '';
  const suggestion = buildTssSafeTextSuggestion(input.value);
  const parts = ['TSS may reject special characters here: ' + formatted + extra + '.'];
  if (suggestion && suggestion !== String(input.value || '').trim()) {
    parts.push('Suggested safe text: ' + suggestion);
  } else {
    parts.push('Use plain ASCII text before sending this to TSS.');
  }

  input.classList.add('tss-char-needs-fix');
  tip.classList.remove('d-none');
  tip.textContent = parts.join(' ');
}

function initTssCharWarnings(root = document) {
  root.querySelectorAll('[data-tss-char-watch="1"]').forEach(function (input) {
    input.addEventListener('input', function () {
      updateTssCharFeedback(input);
    });
    input.addEventListener('blur', function () {
      updateTssCharFeedback(input);
    });
    updateTssCharFeedback(input);
  });
}

document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll('input[name$="_eori"]').forEach(function (input) {
    input.addEventListener('blur', function () {
      const val = this.value.trim().toUpperCase();
      if (val && !val.match(/^[A-Z]{2}/)) {
        this.classList.add('is-invalid');
        let fb = this.nextElementSibling;
        if (!fb || !fb.classList.contains('invalid-feedback')) {
          fb = document.createElement('div');
          fb.className = 'invalid-feedback';
          this.parentNode.appendChild(fb);
        }
        fb.textContent = 'EORI should start with 2 alpha characters (e.g. XI, GB, EU)';
      } else {
        this.classList.remove('is-invalid');
      }
    });
  });

  document.querySelectorAll('input[maxlength="2"]').forEach(function (input) {
    input.addEventListener('input', function () {
      const upper = this.value.toUpperCase();
      if (this.value !== upper) this.value = upper;
    });
  });

  let formDirty = false;
  document.querySelectorAll('form input, form select, form textarea').forEach(function (el) {
    el.addEventListener('change', function (e) {
      if (e.isTrusted) formDirty = true;
    });
  });
  window.addEventListener('beforeunload', function (e) {
    if (formDirty) {
      e.preventDefault();
      e.returnValue = '';
    }
  });
  document.querySelectorAll('form').forEach(function (form) {
    form.addEventListener('submit', function () { formDirty = false; });
  });

  initTssCharWarnings();
});
