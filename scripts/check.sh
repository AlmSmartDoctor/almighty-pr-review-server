#!/usr/bin/env bash
# scripts/check.sh — 로컬 실행에 필요한 설정이 모두 갖춰졌는지 점검한다.
#   필수 항목(✗)이 하나라도 빠지면 exit 1. 경고(⚠)는 종료코드에 영향 없음.
#   사용: ./scripts/check.sh   (레포 어디서 실행해도 됨)
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ -t 1 ]; then
  G=$'\033[32m'; R=$'\033[31m'; Y=$'\033[33m'; B=$'\033[1m'; N=$'\033[0m'
else
  G=""; R=""; Y=""; B=""; N=""
fi
fail=0; warn=0
ok()   { printf "  ${G}\xe2\x9c\x93${N} %s\n" "$1"; }
bad()  { printf "  ${R}\xe2\x9c\x97${N} %s\n" "$1"; [ -n "${2:-}" ] && printf "      ${Y}\xe2\x86\x92 %s${N}\n" "$2"; fail=$((fail+1)); }
note() { printf "  ${Y}\xe2\x9a\xa0${N} %s\n" "$1"; [ -n "${2:-}" ] && printf "      \xe2\x86\x92 %s\n" "$2"; warn=$((warn+1)); }
sec()  { printf "\n${B}%s${N}\n" "$1"; }

sec "레포 루트"
ok "$ROOT"

sec "백엔드 (Python)"
PY="$ROOT/.venv/bin/python"
if [ -x "$PY" ]; then
  ok ".venv 존재 ($("$PY" --version 2>&1))"
  if "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,12) else 1)' 2>/dev/null; then
    ok "Python >= 3.12"
  else
    bad "Python < 3.12" "python3.12 -m venv .venv 로 재생성"
  fi
  if "$PY" -c 'import fastapi, uvicorn, pydantic' 2>/dev/null; then
    ok "백엔드 의존성(fastapi/uvicorn/pydantic) 설치됨"
  else
    bad "백엔드 의존성 누락" "$PY -m pip install -e '.[dev]'"
  fi
  if "$PY" -c 'from server.api import app' 2>/dev/null; then
    ok "server.api:app import 성공"
  else
    bad "server.api:app import 실패" "레포 루트에서 실행 중인지 / 의존성 설치 확인"
  fi
else
  bad ".venv 없음 (Python 가상환경)" "python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'"
fi

sec "프론트엔드 (Node)"
if command -v node >/dev/null 2>&1; then ok "node $(node --version)"; else bad "node 없음" "https://nodejs.org 에서 설치"; fi
if command -v npm  >/dev/null 2>&1; then ok "npm $(npm --version)";  else bad "npm 없음" "node와 함께 설치됨"; fi
if [ -d "$ROOT/web/node_modules" ]; then
  ok "web/node_modules 설치됨"
else
  bad "web/node_modules 없음" "cd web && npm install"
fi

sec "외부 CLI (리뷰 실행에 필요)"
if command -v gh >/dev/null 2>&1; then
  ok "gh $(gh --version 2>/dev/null | head -1 | awk '{print $3}')"
  if gh auth status >/dev/null 2>&1; then
    ok "gh 로그인됨"
  else
    bad "gh 미로그인" "gh auth login"
  fi
else
  bad "gh 없음" "brew install gh"
fi
vendor_count=0
for cli in claude codex; do
  if command -v "$cli" >/dev/null 2>&1; then
    ok "$cli ($(command -v "$cli"))"
    vendor_count=$((vendor_count+1))
  else
    note "$cli 없음 (해당 vendor를 쓸 때 필요)" "$cli CLI 설치 또는 설정에서 해당 vendor 끄기"
  fi
done
if [ "$vendor_count" -eq 0 ]; then
  bad "Claude/Codex CLI가 모두 없음" "최소 하나의 vendor CLI 설치 및 로그인"
else
  ok "리뷰 vendor CLI 최소 1개 사용 가능"
fi
note "vendor 인증은 리뷰 시점에 격리 하네스가 키체인/auth.json에서 주입 — 여기선 존재만 점검"

sec "하네스 프로파일 (harness/default)"
for f in config.json tools-allowlist.json review-system-prompt.md; do
  if [ -f "$ROOT/harness/default/$f" ]; then ok "$f"; else bad "harness/default/$f 없음"; fi
done

sec "포트"
for p in 8787 5173; do
  if lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1; then
    note "포트 $p 사용 중" "이미 떠 있거나 다른 프로세스 점유 — 필요시 lsof -ti :$p | xargs kill"
  else
    ok "포트 $p 사용 가능"
  fi
done

printf "\n${B}요약:${N} "
if [ "$fail" -eq 0 ]; then
  printf "${G}준비 완료${N} (경고 %d개)\n" "$warn"
  printf "실행 → ${B}./scripts/dev.sh${N}\n"
  exit 0
else
  printf "${R}필수 %d개 누락${N} (경고 %d개) — 위 \xe2\x86\x92 안내대로 조치 후 다시 실행\n" "$fail" "$warn"
  exit 1
fi
