const BASE = '/api';

async function request(path, options = {}) {
  const response = await fetch(`${BASE}${path}`, options);
  if (!response.ok) {
    let detail = `Request failed with status ${response.status}`;
    try {
      const body = await response.json();
      if (body.detail) detail = body.detail;
    } catch {
      /* the body was not JSON; the status is all we have */
    }
    throw new Error(detail);
  }
  return response.json();
}

const query = (params) => {
  const search = new URLSearchParams();
  Object.entries(params || {}).forEach(([key, value]) => {
    if (value !== '' && value !== null && value !== undefined) search.set(key, value);
  });
  const text = search.toString();
  return text ? `?${text}` : '';
};

export const getStats = () => request('/system/stats');
export const getDocuments = () => request('/documents');
export const getDocument = (id) => request(`/documents/${id}`);
export const getPage = (id, page) => request(`/documents/${id}/pages/${page}`);
export const deleteDocument = (id) => request(`/documents/${id}`, { method: 'DELETE' });
export const getFacts = (params) => request(`/facts${query(params)}`);
export const getFact = (id) => request(`/facts/${id}`);
export const getSchema = () => request('/facts/schema');
export const getRelationships = (params) => request(`/relationships${query(params)}`);
export const getCases = () => request('/showcase/cases');
export const recompute = () => request('/relationships/recompute', { method: 'POST' });

export const uploadDocument = (file) => {
  const body = new FormData();
  body.append('file', file);
  return request('/documents/upload', { method: 'POST', body });
};
