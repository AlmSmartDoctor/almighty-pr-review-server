const json = async (r: Response) => {
  const data = await r.json().catch(() => null);
  if (!r.ok) {
    const message =
      data?.detail?.message ??
      (typeof data?.detail === "string" ? data.detail : null) ??
      data?.message ??
      `HTTP ${r.status}`;
    throw new Error(message);
  }
  return data;
};

const ADMIN_TOKEN_KEY = "almighty-admin-token";
export const setAdminToken = (token: string) => sessionStorage.setItem(ADMIN_TOKEN_KEY, token);
export const clearAdminToken = () => sessionStorage.removeItem(ADMIN_TOKEN_KEY);
export const hasAdminToken = () => Boolean(sessionStorage.getItem(ADMIN_TOKEN_KEY));

const request = (url: string, init: RequestInit = {}) => {
  const headers = new Headers(init.headers);
  const token = sessionStorage.getItem(ADMIN_TOKEN_KEY);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  return fetch(url, { ...init, headers });
};

const writeJson = (method: string, body: object) => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export type HarnessPut = {
  system_prompt?: string;
  vendor_prompts?: Record<string, string>;
};

export const api = {
  health: () => fetch("/api/health").then(json),
  deepHealth: () => request("/api/health/deep").then(json),
  repos: () => request("/api/repos").then(json),
  addRepo: (body: { full_name: string; local_path?: string }) =>
    request("/api/repos", writeJson("POST", body)).then(json),
  patchRepo: (id: number, body: object) =>
    request(`/api/repos/${id}`, writeJson("PATCH", body)).then(json),
  deleteRepo: (id: number) => request(`/api/repos/${id}`, { method: "DELETE" }).then(json),
  repoReadiness: (id: number) => request(`/api/repos/${id}/readiness`).then(json),
  syncRepo: (id: number) => request(`/api/repos/${id}/sync`, { method: "POST" }).then(json),
  syncRepos: () => request("/api/repos/sync", { method: "POST" }).then(json),
  overview: () => request("/api/overview").then(json),
  wiki: () => request("/api/wiki").then(json),
  refreshWiki: (repoId: number) => request(`/api/repos/${repoId}/wiki/refresh`, { method: "POST" }).then(json),
  learn: () => request("/api/learn").then(json),
  proposeReviewRules: (repoId: number) => request(`/api/repos/${repoId}/review-rules/propose`, { method: "POST" }).then(json),
  patchReviewRule: (ruleId: number, status: "active" | "disabled") => request(`/api/review-rules/${ruleId}`, writeJson("PATCH", { status })).then(json),
  settings: () => request("/api/settings").then(json),
  contextStatus: () => request("/api/settings/context-status").then(json),
  patchSettings: (body: object) => request("/api/settings", writeJson("PATCH", body)).then(json),
  models: () => request("/api/models").then(json),
  harnesses: () => request("/api/harness").then(json).then((r) => r.harnesses as string[]),
  harness: (name: string) => request(`/api/harness/${name}`).then(json),
  putHarness: (name: string, body: HarnessPut) => request(`/api/harness/${name}`, writeJson("PUT", body)).then(json),
  runFindings: (id: number) => request(`/api/runs/${id}/findings`).then(json),
  runVendorResults: (id: number) => request(`/api/runs/${id}/vendor-results`).then(json),
  prRuns: (prId: number) => request(`/api/prs/${prId}/runs`).then(json),
  runContext: (id: number) => request(`/api/runs/${id}/context`).then(json),
  runPostPreview: (id: number) => request(`/api/runs/${id}/post-preview`).then(json),
  prPostHealth: (id: number) => request(`/api/prs/${id}/post-health`).then(json),
  patchFinding: (id: number, body: object) => request(`/api/findings/${id}`, writeJson("PATCH", body)).then(json),
  postRun: (id: number) => request(`/api/runs/${id}/post`, { method: "POST" }).then(json),
  triggerReview: (prId: number) => request(`/api/prs/${prId}/review`, { method: "POST" }).then(json),
  cancelReview: (prId: number) => request(`/api/prs/${prId}/cancel-review`, { method: "POST" }).then(json),
  retryVendors: (runId: number) => request(`/api/runs/${runId}/retry-vendors`, { method: "POST" }).then(json),
};
