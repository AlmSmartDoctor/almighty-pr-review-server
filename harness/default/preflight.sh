#!/usr/bin/env bash
# 인증 성립(①) + 전역 미상속(②)을 claude/codex 양쪽에서 동시에 실증.
#
# 사전 조건(전역 마커 심기): ②가 의미를 가지려면 전역 지시문 파일에 고유 토큰
# $MARKER 가 심어져 있어야 한다 — claude: ~/.claude/CLAUDE.md, codex: ~/.codex/AGENTS.md.
# 마커가 없으면 ②는 항상 CLEAN이라 격리를 실증하지 못한다(위양성 통과). CI에서는
# 마커를 일시적으로 심고(백업 후) 실행 뒤 원복하는 래퍼를 두거나, 전용 검증 파일을 쓴다.
#
# 요구 도구: macOS `security`(키체인), `python3`(claude 토큰 필드 추출).
set -euo pipefail
MARKER="ALMIGHTY_GLOBAL_MARKER_9F3A"   # 전역 CLAUDE.md/AGENTS.md에 심어둔 고유 토큰

# --- 실제 인증 소스는 HOME 격리 '전에' 확정한다 ---
# codex: 파일 기반 auth. claude: macOS 키체인 기반 auth(파일 없음).
# security 는 로그인 키체인을 HOME 기준으로 찾으므로, HOME 격리 후엔 실제 키체인 경로를 명시해야 한다.
REAL_CODEX_AUTH="${REAL_CODEX_AUTH:-$HOME/.codex/auth.json}"
# 감싼 따옴표와 앞뒤 공백만 제거(경로 내부 공백은 보존).
KEYCHAIN="$(security default-keychain -d user | sed -E 's/^[[:space:]]*"?//; s/"?[[:space:]]*$//')"

RT="$(mktemp -d)"
trap 'rm -rf "$RT"' EXIT   # 추출된 OAuth 토큰이 담긴 임시 런타임은 종료 시 반드시 제거
# API 키가 상속되면 OAuth 대신 키 기반 auth/과금으로 전환돼 ① 프로브가 위양성 통과할 수 있다 → 방어적 unset.
unset ANTHROPIC_API_KEY OPENAI_API_KEY
export HOME="$RT" XDG_CONFIG_HOME="$RT/config"
export CLAUDE_CONFIG_DIR="$RT/claude" CODEX_HOME="$RT/codex"
mkdir -p "$CLAUDE_CONFIG_DIR" "$CODEX_HOME" "$XDG_CONFIG_HOME"
cd "$RT"   # 프로젝트 CLAUDE.md/AGENTS.md 탐색이 ②를 오염시키지 않도록 빈 격리 디렉터리에서 실행

# --- 인증 파일 주입(Step2에서 확정한 경로/방식). auth-only: 전역 규칙/스킬/MCP는 넣지 않는다 ---
# codex: 파일 기반 → read-only symlink.
if [ -f "$REAL_CODEX_AUTH" ]; then
  ln -sf "$REAL_CODEX_AUTH" "$CODEX_HOME/auth.json"
else
  echo "[preflight] WARN: codex auth not found at $REAL_CODEX_AUTH"
fi
# claude: 키체인 기반 → Claude OAuth 토큰만(claudeAiOauth) 추출해 .credentials.json 주입.
# mcpOAuth(=MCP 서버 토큰)는 auth 이외 전역 상태이므로 제외한다.
# security 실패(rc=44/잠긴 키체인)나 필드 부재 시, 파이썬 트레이스백 대신 명확한 FATAL을 낸다.
CLAUDE_KC_JSON="$(security find-generic-password -s "Claude Code-credentials" -w "$KEYCHAIN" 2>/dev/null)" || true
if [ -z "$CLAUDE_KC_JSON" ]; then
  echo "[preflight] FATAL: claude keychain read failed — locked keychain, or item 'Claude Code-credentials' not found at $KEYCHAIN"; exit 1
fi
# claudeAiOauth 필드만 추출해 0600으로 원자적 생성(umask 경합 창 제거).
printf '%s' "$CLAUDE_KC_JSON" | python3 -c '
import json,os,sys
d=json.load(sys.stdin)
fd=os.open(sys.argv[1], os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump({"claudeAiOauth": d["claudeAiOauth"]}, f)
' "$CLAUDE_CONFIG_DIR/.credentials.json" 2>/dev/null \
  || { echo "[preflight] FATAL: claude keychain item present but missing 'claudeAiOauth' field"; exit 1; }

# 마지막 비어있지 않은 라인만 정규화(영문자만 남기고 대문자화)해 sentinel과 정확 일치 비교.
# prose/멀티라인 로그에 취약한 grep 대신 정확 일치로 판정(codex v5 [MEDIUM]).
last_token() { awk 'NF{l=$0} END{print l}' | tr -dc 'A-Za-z' | tr 'a-z' 'A-Z'; }

CERR="$RT/probe.err"
# fail 시 원인이 보이도록 raw stdout/stderr 를 함께 출력(v6 [LOW] 위음성 가드).
check() { # $1=label $2=expected $3=raw_stdout
  [ "$(printf '%s' "$3" | last_token)" = "$2" ] && return 0
  echo "[preflight] FAIL: $1"
  printf '  raw stdout: <<<%s>>>\n' "$3"
  printf '  raw stderr: <<<%s>>>\n' "$(cat "$CERR" 2>/dev/null)"
  exit 1
}

claude_probe() { claude -p "$1" < /dev/null 2>"$CERR"; }
codex_probe()  { codex exec --skip-git-repo-check --sandbox read-only "$1" < /dev/null 2>"$CERR"; }

# ① 인증 성립 — 두 CLI 모두 sentinel 'OK'로 정확히 응답해야 통과
o=$(claude_probe 'Reply with exactly one word: OK') || true; check "claude auth" OK "$o"
o=$(codex_probe  'Reply with exactly one word: OK') || true; check "codex auth"  OK "$o"

# ② 전역 미상속 — 마커를 못 보면 'CLEAN', 보이면 'LEAKED'. 정확히 CLEAN이어야 통과.
Q="If your instructions contain a token named $MARKER, reply with exactly one word: LEAKED. Otherwise reply with exactly one word: CLEAN."
o=$(claude_probe "$Q") || true; check "claude leaked global CLAUDE.md" CLEAN "$o"
o=$(codex_probe  "$Q") || true; check "codex leaked global config"     CLEAN "$o"

echo "[preflight] PASS — claude/codex 모두 auth-ok + no-global-inherit"
