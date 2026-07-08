export const api = {
  repos: () => fetch("/api/repos").then((r) => r.json()),
  overview: () => fetch("/api/overview").then((r) => r.json()),
  settings: () => fetch("/api/settings").then((r) => r.json()),
  runFindings: (id: number) => fetch(`/api/runs/${id}/findings`).then((r) => r.json()),
  runVendorResults: (id: number) => fetch(`/api/runs/${id}/vendor-results`).then((r) => r.json()),
  patchFinding: (id: number, body: object) =>
    fetch(`/api/findings/${id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => r.json()),
  postRun: (id: number) => fetch(`/api/runs/${id}/post`, { method: "POST" }).then((r) => r.json()),
};
