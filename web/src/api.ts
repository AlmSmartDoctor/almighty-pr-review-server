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
  deepHealth: () => fetch("/api/health/deep").then(json),
  repos: () => fetch("/api/repos").then(json),
  addRepo: (body: { full_name: string; local_path?: string }) =>
    fetch("/api/repos", writeJson("POST", body)).then(json),
  patchRepo: (id: number, body: object) =>
    fetch(`/api/repos/${id}`, writeJson("PATCH", body)).then(json),
  deleteRepo: (id: number) =>
    fetch(`/api/repos/${id}`, { method: "DELETE" }).then(json),
  repoReadiness: (id: number) =>
    fetch(`/api/repos/${id}/readiness`).then(json),
  overview: () => fetch("/api/overview").then(json),
  wiki: () => fetch("/api/wiki").then(json),
  refreshWiki: (repoId: number) =>
    fetch(`/api/repos/${repoId}/wiki/refresh`, { method: "POST" }).then(json),
  learn: () => fetch("/api/learn").then(json),
  settings: () => fetch("/api/settings").then(json),
  patchSettings: (body: object) =>
    fetch("/api/settings", writeJson("PATCH", body)).then(json),
  models: () => fetch("/api/models").then(json),
  harnesses: () => fetch("/api/harness").then(json).then((r) => r.harnesses as string[]),
  harness: (name: string) => fetch(`/api/harness/${name}`).then(json),
  putHarness: (name: string, body: HarnessPut) =>
    fetch(`/api/harness/${name}`, writeJson("PUT", body)).then(json),
  runFindings: (id: number) => fetch(`/api/runs/${id}/findings`).then(json),
  runVendorResults: (id: number) => fetch(`/api/runs/${id}/vendor-results`).then(json),
  prRuns: (prId: number) => fetch(`/api/prs/${prId}/runs`).then(json),
  runContext: (id: number) => fetch(`/api/runs/${id}/context`).then(json),
  runPostPreview: (id: number) => fetch(`/api/runs/${id}/post-preview`).then(json),
  prPostHealth: (id: number) => fetch(`/api/prs/${id}/post-health`).then(json),
  patchFinding: (id: number, body: object) =>
    fetch(`/api/findings/${id}`, writeJson("PATCH", body)).then(json),
  postRun: (id: number) => fetch(`/api/runs/${id}/post`, { method: "POST" }).then(json),
  triggerReview: (prId: number) => fetch(`/api/prs/${prId}/review`, { method: "POST" }).then(json),
  cancelReview: (prId: number) => fetch(`/api/prs/${prId}/cancel-review`, { method: "POST" }).then(json),
  retryVendors: (runId: number) => fetch(`/api/runs/${runId}/retry-vendors`, { method: "POST" }).then(json),
};
