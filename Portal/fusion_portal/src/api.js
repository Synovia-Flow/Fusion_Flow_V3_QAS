const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/$/, '');

export function getApiDocsUrl() {
  return `${API_BASE_URL}/docs`;
}

async function request(path, options = {}) {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers: {
      ...(options.body instanceof FormData ? {} : { Accept: 'application/json' }),
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = await response.json();
      detail = payload?.detail?.message || payload?.detail || detail;
    } catch {
      // Keep the HTTP status text when the server does not return JSON.
    }
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return response.json();
}

export function loginPortal({ username, password }) {
  return request('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  });
}
export function getSession(clientCode) {
  return request(`/api/session?client_code=${encodeURIComponent(clientCode)}`);
}

export function getDashboard(clientCode) {
  return request(`/api/dashboard?client_code=${encodeURIComponent(clientCode)}`);
}

export function getControlTower(clientCode) {
  return request(`/api/control-tower?client_code=${encodeURIComponent(clientCode)}`);
}

export function getDeclarations({ clientCode, limit = 100 }) {
  const params = new URLSearchParams({ client_code: clientCode, limit: String(limit) });
  return request(`/api/declarations?${params.toString()}`);
}

// page/pageSize cut the page in SQL, so a deep page costs what page 1 costs.
// apiDateRange filters on the TSS arrival date/time and cannot be paged in SQL,
// so the API scans a bounded window for it and reports pagination.scanTruncated.
export function getConsignments({ clientCode, status = 'ALL', q = '', limit = 100, page = 1, pageSize, apiDateRange = 'all' }) {
  const params = new URLSearchParams({ client_code: clientCode, status, q, limit: String(limit) });
  if (page && page > 1) params.set('page', String(page));
  if (pageSize) params.set('page_size', String(pageSize));
  if (apiDateRange && apiDateRange !== 'all') params.set('api_date_range', apiDateRange);
  return request(`/api/consignments?${params.toString()}`);
}

export function logoutPortalSession({ username, clientCode, envCode, authCorrelationId } = {}) {
  return request('/api/auth/logout', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, clientCode, envCode, authCorrelationId }),
  });
}

// Keyed by the TSS DEC reference: it is the only consignment key every tenant
// carries. CWF has no numeric id, so a row id cannot be the contract.
export function getConsignmentDetail(reference, clientCode) {
  const params = clientCode ? `?client_code=${encodeURIComponent(clientCode)}` : '';
  return request(`/api/consignments/${encodeURIComponent(reference)}${params}`);
}

export function getTssConnections(clientCode, envCode) {
  const params = new URLSearchParams();
  if (clientCode) params.set('client_code', clientCode);
  if (envCode) params.set('env_code', envCode);
  const query = params.toString();
  return request(`/api/tss/connections${query ? `?${query}` : ''}`);
}

export function testTssConnection({ clientCode, envCode }) {
  const params = new URLSearchParams();
  if (clientCode) params.set('client_code', clientCode);
  if (envCode) params.set('env_code', envCode);
  const query = params.toString();
  return request(`/api/tss/connections/test${query ? `?${query}` : ''}`);
}

export function getAdminSettings(clientCode) {
  return request(`/api/admin/settings?client_code=${encodeURIComponent(clientCode)}`);
}

export function getValidationDiagnostics(clientCode) {
  return request(`/api/admin/validation-diagnostics?client_code=${encodeURIComponent(clientCode)}`);
}

export function saveAdminSettings({ clientCode, updates }) {
  return request('/api/admin/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ clientCode, updates }),
  });
}
export function previewConsignmentUpload({ clientCode, files, demoMode = false, demoEnsReference = '' }) {
  const body = new FormData();
  body.append('client_code', clientCode);
  body.append('demo_mode', demoMode ? 'true' : 'false');
  if (demoEnsReference) body.append('demo_ens_reference', demoEnsReference);
  Array.from(files || []).forEach((file) => body.append('files', file));
  return request('/api/uploads/consignments/preview', { method: 'POST', body });
}
export function validateConsignmentPreview({ clientCode, processingPreview, demoMode = false }) {
  return request('/api/uploads/consignments/preview/validate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ clientCode, processingPreview, demoMode }),
  });
}
export function prepareTssConsignmentSubmit({ clientCode, consignmentRowId }) {
  const params = new URLSearchParams({ client_code: clientCode, dry_run: 'true' });
  return request(`/api/tss/consignments/${encodeURIComponent(consignmentRowId)}/submit?${params.toString()}`, { method: 'POST' });
}
