export function formatDuration(ms?: number | null) {
  if (ms == null) return null;
  if (ms < 1000) return `${ms}ms`;
  const sec = ms / 1000;
  return sec < 10 ? `${sec.toFixed(1)}초` : `${Math.round(sec)}초`;
}

export function formatDateTime(value: string) {
  const matched = value.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/);
  if (matched) return `${matched[1]} ${matched[2]}`;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
