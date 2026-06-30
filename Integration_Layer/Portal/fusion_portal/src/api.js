const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || '';

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

export function getConsignments({ clientCode, status = 'ALL', q = '', limit = 100 }) {
  const params = new URLSearchParams({ client_code: clientCode, status, q, limit: String(limit) });
  return request(`/api/consignments?${params.toString()}`);
}

export function getTssConnections(clientCode) {
  const params = new URLSearchParams();
  if (clientCode) params.set('client_code', clientCode);
  const query = params.toString();
  return request(`/api/tss/connections${query ? `?${query}` : ''}`);
}

export function getAdminSettings(clientCode) {
  return request(`/api/admin/settings?client_code=${encodeURIComponent(clientCode)}`);
}

export function saveAdminSettings({ clientCode, updates }) {
  return request('/api/admin/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ clientCode, updates }),
  });
}
export function previewConsignmentUpload({ clientCode, files }) {
  const body = new FormData();
  body.append('client_code', clientCode);
  Array.from(files || []).forEach((file) => body.append('files', file));
  return request('/api/uploads/consignments/preview', { method: 'POST', body });
}
export function prepareTssConsignmentSubmit({ clientCode, consignmentRowId }) {
  const params = new URLSearchParams({ client_code: clientCode, dry_run: 'true' });
  return request(`/api/tss/consignments/${encodeURIComponent(consignmentRowId)}/submit?${params.toString()}`, { method: 'POST' });
}
