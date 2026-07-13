# Vendor CLI Contract (claude / codex) — verified headless contract

**Status: VERIFIED PASS.** This document is the single source of truth for the
`claude` / `codex` headless adapters (milestones M3.3 / M3.4). It records what was
*empirically* proven on the real machine, not what the docs claim.

- Verification date: 2026-07-08
- `claude` 2.1.198 (`/Users/alm/.local/bin/claude`)
- `codex-cli` 0.142.5 (`/opt/homebrew/bin/codex`)
- Reference script: [`harness/default/preflight.sh`](../harness/default/preflight.sh)
- Final preflight result: `[preflight] PASS — claude/codex 모두 auth-ok + no-global-inherit` (exit 0)

The spike proves the two properties that pull in opposite directions can hold
**simultaneously** for both CLIs:

1. **① Auth holds** under a fully isolated `HOME`/config env, and
2. **② Global config is NOT inherited** (global `CLAUDE.md` / `AGENTS.md` / MCP / skills invisible).

---

## 1. Headless flags (Step 1, verified from `--help`)

| Concern | claude | codex |
| --- | --- | --- |
| Non-interactive exec | `-p, --print` | `codex exec` (alias `e`) |
| Read-only / tool restriction | `--allowedTools` / `--disallowedTools`, `--permission-mode <default\|plan\|acceptEdits\|bypassPermissions>` | `-s, --sandbox read-only` (also `workspace-write`, `danger-full-access`) |
| Structured output | `--output-format <text\|json\|stream-json>` (+ `--include-partial-messages`) | `--json` (JSONL events); `-o, --output-last-message <FILE>` (final message only); `--output-schema <FILE>` |
| Config-dir env var | `CLAUDE_CONFIG_DIR` | `CODEX_HOME` |
| Model select | `--model <name>` | `-m, --model <name>` or `-c model=...` |
| Reasoning effort | (none — effort = model choice) | `-c model_reasoning_effort=<none\|minimal\|low\|medium\|high\|xhigh>` |
| Working dir | (runs in CWD; `--add-dir` to widen) | `-C, --cd <DIR>` |
| Outside a git repo | n/a | `--skip-git-repo-check` (REQUIRED outside a repo) |
| Skip global config natively | `--safe-mode` (disables CLAUDE.md/skills/plugins/hooks/MCP; **auth still works**) | `--ignore-user-config` (skip `$CODEX_HOME/config.toml`, auth still uses `CODEX_HOME`); `--ignore-rules` |
| Ephemeral session | `--no-session-persistence` | `--ephemeral` |

**Do NOT use `claude --bare`**: it disables keychain reads and forces Anthropic auth
to `ANTHROPIC_API_KEY`/`apiKeyHelper` only — it would break our OAuth/keychain auth.
`--safe-mode` is the safe native equivalent (keeps auth, drops CLAUDE.md/MCP), but the
adapter's primary isolation mechanism is **env isolation** (below), not these flags.

**codex reads stdin even with a positional prompt.** Every invocation MUST redirect
`< /dev/null`, otherwise it hangs at 0% CPU waiting on stdin. Output flushes at the end.
codex writes its human banner + turn transcript + `tokens used` to **stderr**; **stdout
is only the final agent message** — so stdout parsing is clean.

---

## 2. Authentication (Step 2, the crux — verified)

| CLI | Auth store | Isolated by env alone? | Injection needed |
| --- | --- | --- | --- |
| claude | **macOS keychain** — generic-password service `Claude Code-credentials` (no `~/.claude/.credentials.json` file exists) | **NO** — breaks ("Not logged in · Please run /login", exit 1) | **YES** — materialize `.credentials.json` from keychain |
| codex | **file** — `~/.codex/auth.json` (0600) | NO — isolating `CODEX_HOME` hides it | **YES** — read-only symlink `auth.json` |

### 2a. Why the "keychain is HOME-independent" assumption is FALSE

`security` resolves the login keychain **via `HOME`** (`$HOME/Library/Keychains/login.keychain-db`).
Once the adapter sets `HOME=<tmp>`, `security` searches the wrong (empty) keychain →
item not found (`rc=44`) → claude reports **"Not logged in"** even though the token exists.

**Fix (verified):** pass the *explicit* real keychain path to `security`. With the
explicit path, the read succeeds (`rc=0`) even under a redirected `HOME`. The adapter
captures the real keychain path **before** redirecting `HOME` (strip only wrapping quotes +
leading/trailing whitespace — a keychain path may legitimately contain spaces):
```sh
KEYCHAIN="$(security default-keychain -d user | sed -E 's/^[[:space:]]*"?//; s/"?[[:space:]]*$//')"   # BEFORE export HOME
```

### 2b. claude injection — auth-only `.credentials.json`

The keychain secret is JSON with two top-level keys:
- `claudeAiOauth` — the Claude OAuth token (`accessToken`, `refreshToken`, `expiresAt`,
  `scopes`, `subscriptionType`, `rateLimitTier`). **This is the only auth we inject.**
- `mcpOAuth` — OAuth tokens for MCP servers (datadog / github / atlassian). **Excluded**
  (auth-only injection; MCP is global state that must stay invisible per test ②).

The adapter writes `{"claudeAiOauth": …}` to `$CLAUDE_CONFIG_DIR/.credentials.json` (0600).
**Verified minimal:** claude authenticates with this file ALONE — no `.claude.json`
account/onboarding state is required, and print mode does not trigger a trust/onboarding prompt.

### 2c. codex injection — `auth.json` symlink only

Read-only symlink `~/.codex/auth.json` → `$CODEX_HOME/auth.json`. **`config.toml` is NOT
injected**: codex resolves its default model (`gpt-5.5`) and provider (`openai`) from the
account without it, and NOT injecting `config.toml` is what keeps `danger-full-access` /
`approval=never` / `project_doc_fallback_filenames=["CLAUDE.md"]` out of the runtime.
(If a future adapter ever needs a model/provider setting, pass it via `-c key=value` /
`--model`, never by copying the global `config.toml`.)

**Reasoning effort (empirically verified, codex-cli 0.144.1):** `codex exec -c
model_reasoning_effort=<value>` is the effort knob. A bad value is rejected by the API
(`invalid_enum_value`) which enumerates the supported set: `none, minimal, low, medium,
high, xhigh`. The adapter injects this from `HarnessProfile.effort` (driven per-repo by
`repo.default_effort`) but ONLY when the value is in that set — an unknown value omits the
flag so codex uses its default, never 400-failing the whole review. `claude` has NO
reasoning/effort flag (see §1); for claude, effort = model choice.

### 2d. Minimal auth env allowlist

Because auth is injected as **files** into the isolated dirs, **no auth-specific env var
needs to pass through.** The runtime only needs `PATH` (to locate the two binaries).
**Explicitly do NOT set `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`** in the runtime — they would
switch auth mode and change billing away from the OAuth subscription.

---

## 3. Isolation model (the runtime the adapter must build)

Per invocation, create a fresh `RT="$(mktemp -d)"` and:

```
trap 'rm -rf "$RT"' EXIT                 # per-invocation cleanup: RT holds a real OAuth token — never leave it behind
unset ANTHROPIC_API_KEY OPENAI_API_KEY   # defensive: an inherited API key would switch auth/billing and false-pass ①
export HOME="$RT"                       # isolates ~/.claude/CLAUDE.md, ~/.codex/AGENTS.md, keychain search
export XDG_CONFIG_HOME="$RT/config"
export CLAUDE_CONFIG_DIR="$RT/claude"   # claude user config → empty
export CODEX_HOME="$RT/codex"           # codex config/auth/AGENTS → empty
cd "$RT"                                 # so project-level CLAUDE.md/AGENTS.md discovery can't confound test ②
# inject auth-only (create .credentials.json atomically at 0600):
ln -sf <real>/.codex/auth.json  "$CODEX_HOME/auth.json"
security find-generic-password -s "Claude Code-credentials" -w "$KEYCHAIN" \
  | python3 -c '...claudeAiOauth only, os.open(...,0o600)...' "$CLAUDE_CONFIG_DIR/.credentials.json"
```

Setting `HOME` alone hides both global instruction files: claude's global memory is
`~/.claude/CLAUDE.md` and codex's global instructions are `~/.codex/AGENTS.md`
(a symlink into `codex-config`); with `HOME`/`CODEX_HOME` redirected neither is read.
**Test ② confirmed CLEAN for both** with the marker planted (see §6).

---

## 4. Contract table (SINGLE SOURCE OF TRUTH for the adapters)

| Field | claude | codex |
| --- | --- | --- |
| **argv (read-only probe)** | `claude -p "<prompt>"` (add `--permission-mode plan` / `--disallowedTools` to lock down; `--model <m>`) | `codex exec --skip-git-repo-check --sandbox read-only "<prompt>"` |
| **stdin** | `< /dev/null` | `< /dev/null` (**mandatory** — else 0% hang) |
| **env: set** | `HOME`, `XDG_CONFIG_HOME`, `CLAUDE_CONFIG_DIR` → tmp | `HOME`, `XDG_CONFIG_HOME`, `CODEX_HOME` → tmp |
| **env: preserve** | `PATH` | `PATH` |
| **env: forbid** | `ANTHROPIC_API_KEY` | `OPENAI_API_KEY` |
| **auth injection** | `$CLAUDE_CONFIG_DIR/.credentials.json` = `{claudeAiOauth}` extracted from keychain (`security … -w "$KEYCHAIN"`, path captured pre-`HOME`), 0600 | `$CODEX_HOME/auth.json` = read-only symlink to `~/.codex/auth.json` |
| **config injection** | none | none (default model `gpt-5.5` resolves from account) |
| **output: final message** | stdout = response text | stdout = final agent message only (banner/transcript/tokens → stderr) |
| **output: structured** | `--output-format json` / `stream-json` | `--json` (JSONL) or `-o <FILE>` (final-message file) |
| **timeout guidance** | probes completed in ~10–30 s; adapter should use a generous per-call timeout (start ~120 s, tune at M3) and kill on expiry | same; codex flushes at end so partial output is not observable before completion |
| **auth-failure signal** | stdout `Not logged in · Please run /login`, **exit 1** (empirically observed) | missing/invalid `auth.json` → non-zero exit, error on stderr (INFERRED — not triggered this spike) |
| **rate-limit detection** | NOT triggered this spike (INFERRED): surfaces as a usage/limit message + non-zero exit; parse via `--output-format json` at M3, and inspect `claudeAiOauth.rateLimitTier` | NOT triggered this spike (INFERRED): stream error on stderr / non-zero exit; parse via `--json` events at M3 |

> Tags: rows marked *(empirically observed)* were seen in this spike; *(INFERRED)*
> rate-limit rows were not triggered and MUST be confirmed when M3 wires real error handling.

---

## 5. Sentinel-parsing convention (verified)

Judgement is by **exact match of a normalized last token**, never a loose `grep`
(a `grep` would false-pass on prose/multi-line logs — codex v5 [MEDIUM]):

```sh
last_token() { awk 'NF{l=$0} END{print l}' | tr -dc 'A-Za-z' | tr 'a-z' 'A-Z'; }
# last non-empty line → letters only → uppercase → compare == OK / CLEAN / LEAKED
```

**One-word compliance (verified, v6 [LOW] guard):** all four probe calls obeyed
"reply with exactly one word" — raw stdout was literally `OK`, `OK`, `CLEAN`, `CLEAN`
(no prose), so `last_token` matched exactly. Because compliance is model-dependent, the
adapter/preflight still echoes raw stdout+stderr on any mismatch so a future
non-compliant reply is diagnosable rather than a silent false-negative.

---

## 6. Marker-planting prerequisite (for test ②)

Test ② (`CLEAN`) is only meaningful if a unique token actually lives in the global files
during the run — otherwise it passes trivially (false green). `preflight.sh` **assumes the
marker is pre-planted**:

- `MARKER = ALMIGHTY_GLOBAL_MARKER_9F3A`
- claude global: `~/.claude/CLAUDE.md`
- codex global: `~/.codex/AGENTS.md` → `…/codex-config/codex/AGENTS.md` (git-tracked; symlink)

Planting may be **transient**: back up both files, append one marker line, run, then
restore byte-for-byte (use `trap … EXIT`). During this spike the planting was transient and
both files were restored (verified: `diff` clean for both; `codex-config` repo showed no
new changes). For CI, prefer a dedicated verification fixture or a transient-plant wrapper
rather than editing the user's real global files.

---

## 7. Residual risks for the M3 adapter

- **Rate-limit / error taxonomy is not yet empirically characterized** (§4). M3 must drive a
  real rate-limit / auth-expiry and record the exact machine-detectable signal.
- **Token refresh:** the injected `.credentials.json` is a point-in-time copy of
  `claudeAiOauth`. If the access token expires mid-run, the isolated runtime cannot write
  back a refresh to the keychain. Re-extract per invocation; watch `expiresAt`.
- **`security` prompt risk:** reading a locked keychain can raise a GUI unlock prompt in
  some sessions. In this spike the login keychain was unlocked and the read was
  non-interactive; M3 should handle a possible prompt/failure path.
- **Platform coupling:** the claude injection is macOS-keychain specific. A Linux runner
  would instead have a real `~/.claude/.credentials.json` file to symlink — the adapter must
  branch on platform.
- **Temp-dir / extracted-secret cleanup:** the isolated `RT` contains a real OAuth token
  (`.credentials.json`). The adapter MUST delete it per invocation (`trap 'rm -rf "$RT"' EXIT`)
  so a plaintext token never lingers in `/tmp`; never reuse an `RT` across runs.
- **python3 + macOS `security` are runtime prerequisites** of `preflight.sh`.
