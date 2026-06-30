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

export function previewConsignmentUpload({ clientCode, file }) {
  const body = new FormData();
  body.append('client_code', clientCode);
  body.append('file', file);
  return request('/api/uploads/consignments/preview', { method: 'POST', body });
}
export function prepareTssConsignmentSubmit({ clientCode, consignmentRowId }) {
  const params = new URLSearchParams({ client_code: clientCode, dry_run: 'true' });
  return request(`/api/tss/consignments/${encodeURIComponent(consignmentRowId)}/submit?${params.toString()}`, { method: 'POST' });
}
