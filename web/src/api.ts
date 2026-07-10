const json = async (r: Response) => {
  const data = await r.json().catch(() => null);
  if (!r.ok) {
    const message = data?.detail?.message ?? data?.message ?? `HTTP ${r.status}`;
    throw new Error(message);
  }
  return data;
};

const writeJson = (method: string, body: object) => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export const api = {
  repos: () => fetch("/api/repos").then(json),
  addRepo: (body: { full_name: string; local_path?: string }) =>
    fetch("/api/repos", writeJson("POST", body)).then(json),
  patchRepo: (id: number, body: object) =>
    fetch(`/api/repos/${id}`, writeJson("PATCH", body)).then(json),
  overview: () => fetch("/api/overview").then(json),
  settings: () => fetch("/api/settings").then(json),
  patchSettings: (body: object) =>
    fetch("/api/settings", writeJson("PATCH", body)).then(json),
  harness: (name: string) => fetch(`/api/harness/${name}`).then(json),
  putHarness: (name: string, body: object) =>
    fetch(`/api/harness/${name}`, writeJson("PUT", body)).then(json),
  runFindings: (id: number) => fetch(`/api/runs/${id}/findings`).then(json),
  runVendorResults: (id: number) => fetch(`/api/runs/${id}/vendor-results`).then(json),
  runPostPreview: (id: number) => fetch(`/api/runs/${id}/post-preview`).then(json),
  prPostHealth: (id: number) => fetch(`/api/prs/${id}/post-health`).then(json),
  patchFinding: (id: number, body: object) =>
    fetch(`/api/findings/${id}`, writeJson("PATCH", body)).then(json),
  postRun: (id: number) => fetch(`/api/runs/${id}/post`, { method: "POST" }).then(json),
  triggerReview: (prId: number) => fetch(`/api/prs/${prId}/review`, { method: "POST" }).then(json),
};
