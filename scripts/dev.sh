#!/usr/bin/env bash
# scripts/dev.sh — 백엔드(:8787)와 프론트(:5173)를 한 번에 띄운다. Ctrl-C로 둘 다 종료.
#   사용: ./scripts/dev.sh            # 백엔드 + 프론트 (대시보드)
#         ./scripts/dev.sh --backend  # 백엔드(API)만
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"

BACKEND_ONLY=0
[ "${1:-}" = "--backend" ] && BACKEND_ONLY=1

# --- 최소 가드 (자세한 점검은 ./scripts/check.sh) ---
[ -x "$PY" ] || { echo "✗ .venv 없음 — 먼저 ./scripts/check.sh 실행"; exit 1; }
if [ "$BACKEND_ONLY" -eq 0 ] && [ ! -d "$ROOT/web/node_modules" ]; then
  echo "✗ web/node_modules 없음 — 'cd web && npm install' 후 재시도 (또는 --backend)"; exit 1
fi
for p in 8787 5173; do
  { [ "$p" = 5173 ] && [ "$BACKEND_ONLY" -eq 1 ]; } && continue
  if lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "✗ 포트 $p 이미 사용 중 — lsof -ti :$p | xargs kill 후 재시도"; exit 1
  fi
done

pids=""
cleanup() {
  trap - INT TERM EXIT
  printf "\n▸ 종료 중...\n"
  for pid in $pids; do kill "$pid" 2>/dev/null; done
  # npm이 띄운 vite 등 자식이 남아 있으면 포트로 마저 정리
  for p in 8787 5173; do
    leftover="$(lsof -nP -iTCP:"$p" -sTCP:LISTEN -t 2>/dev/null)"
    [ -n "$leftover" ] && kill $leftover 2>/dev/null
  done
  wait 2>/dev/null
  exit 0
}
trap cleanup INT TERM EXIT

echo "▸ 백엔드 기동: http://127.0.0.1:8787"
"$PY" -m server.main & BE=$!; pids="$pids $BE"

# 헬스 준비 대기 (최대 20초)
up=0
for _ in $(seq 1 40); do
  if curl -sf http://127.0.0.1:8787/api/health >/dev/null 2>&1; then up=1; break; fi
  kill -0 "$BE" 2>/dev/null || { echo "✗ 백엔드가 기동 중 종료됨 (로그 확인)"; exit 1; }
  sleep 0.5
done
[ "$up" -eq 1 ] && echo "  ✓ 백엔드 준비됨" || { echo "✗ 백엔드 헬스체크 20초 내 실패"; exit 1; }

if [ "$BACKEND_ONLY" -eq 0 ]; then
  echo "▸ 프론트 기동: http://localhost:5173"
  ( cd "$ROOT/web" && exec npm run dev ) & FE=$!; pids="$pids $FE"
fi

echo ""
echo "  대시보드 → http://localhost:5173     (API → http://127.0.0.1:8787)"
echo "  Ctrl-C 로 종료"
wait
