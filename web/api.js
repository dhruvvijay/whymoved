// Thin client for the WhyMoved HTTP API. All requests carry the bearer token; errors are
// thrown as ApiError with the server's {error:{code,message}} message.

export class ApiError extends Error {
  constructor(status, code, message) { super(message); this.status = status; this.code = code; }
}

const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode: stay signed in this tab only */ } },
  del(k) { try { localStorage.removeItem(k); } catch { /* ignore */ } },
};

export const session = {
  token: store.get("wm_token"),
  setToken(t) { this.token = t; t ? store.set("wm_token", t) : store.del("wm_token"); },
};

export function apiBase() {
  const fromQuery = new URLSearchParams(location.search).get("api");
  if (fromQuery) { store.set("wm_api", fromQuery.replace(/\/$/, "")); }
  return store.get("wm_api") || window.WHYMOVED_API || "";
}

async function call(method, path, { body, form, auth = true } = {}) {
  const headers = {};
  if (auth && session.token) headers.Authorization = `Bearer ${session.token}`;
  let payload;
  if (form) payload = form;
  else if (body !== undefined) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
  let res;
  try {
    res = await fetch(apiBase() + path, { method, headers, body: payload });
  } catch {
    throw new ApiError(0, "network", "Cannot reach the server. Is it running?");
  }
  if (res.status === 204) return null;
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON error page */ }
  if (!res.ok) {
    const e = data && data.error;
    if (res.status === 401 && auth) session.setToken(null);
    // FastAPI's own validation errors use {detail: [...]}
    const fallback = res.status === 422 ? "Some input was not valid." : `Request failed (${res.status}).`;
    throw new ApiError(res.status, (e && e.code) || "error", (e && e.message) || fallback);
  }
  return data;
}

export const api = {
  register: (email, password, workspace_name) => call("POST", "/auth/register", { body: { email, password, workspace_name }, auth: false }),
  login: (email, password) => call("POST", "/auth/login", { body: { email, password }, auth: false }),
  logout: () => call("POST", "/auth/logout"),
  me: () => call("GET", "/me"),
  datasets: (wid) => call("GET", `/workspaces/${wid}/datasets`),
  upload: (wid, file, name) => {
    const form = new FormData();
    form.append("file", file);
    if (name) form.append("name", name);
    return call("POST", `/workspaces/${wid}/datasets`, { form });
  },
  demo: (wid) => call("POST", `/workspaces/${wid}/demo`),
  dataset: (did) => call("GET", `/datasets/${did}`),
  deleteDataset: (did) => call("DELETE", `/datasets/${did}`),
  analyse: (did, params) => call("POST", `/datasets/${did}/analyses`, { body: params }),
  analyses: (did) => call("GET", `/datasets/${did}/analyses`),
  analysis: (aid) => call("GET", `/analyses/${aid}`),
  deleteAnalysis: (aid) => call("DELETE", `/analyses/${aid}`),
  share: (aid, days) => call("POST", `/analyses/${aid}/share`, { body: { days: days || null } }),
  unshare: (aid) => call("DELETE", `/analyses/${aid}/share`),
  scan: (did) => call("GET", `/datasets/${did}/scan`),
  addNote: (aid, body, isPublic) => call("POST", `/analyses/${aid}/notes`, { body: { body, public: !!isPublic } }),
  deleteNote: (aid, nid) => call("DELETE", `/analyses/${aid}/notes/${nid}`),
  compare: (a, b) => call("GET", `/compare?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`),
  shared: (token) => call("GET", `/shared/${encodeURIComponent(token)}`, { auth: false }),
};
