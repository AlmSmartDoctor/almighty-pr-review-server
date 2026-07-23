# Vendor CLI Contract (claude / codex) — verified headless contract

**Status: VERIFIED PASS.** This document is the single source of truth for the
`claude` / `codex` headless adapters (milestones M3.3 / M3.4). It records what was
*empirically* proven on the real machine, not what the docs claim.

- Auth/isolation verification date: 2026-07-08
- Structured telemetry recheck: 2026-07-22
- `claude` 2.1.198 (`/Users/alm/.local/bin/claude`)
- `codex-cli` 0.144.5 (`/Users/alm/.local/bin/codex`)
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
| Reasoning effort | `--effort <low\|medium\|high\|xhigh\|max>` | `-c model_reasoning_effort=<none\|minimal\|low\|medium\|high\|xhigh>` |
| Working dir | (runs in CWD; `--add-dir` to widen) | `-C, --cd <DIR>` |
| Outside a git repo | n/a | `--skip-git-repo-check` (REQUIRED outside a repo) |
| Skip global config natively | `--safe-mode` (disables CLAUDE.md/skills/plugins/hooks/MCP; **auth still works**) | `--ignore-user-config` (skip `$CODEX_HOME/config.toml`, auth still uses `CODEX_HOME`); `--ignore-rules` |
| Ephemeral session | `--no-session-persistence` | `--ephemeral` |

**Do NOT use `claude --bare`**: it disables keychain reads and forces Anthropic auth
to `ANTHROPIC_API_KEY`/`apiKeyHelper` only — it would break our OAuth/keychain auth.
`--safe-mode` is the safe native equivalent (keeps auth, drops CLAUDE.md/MCP), but the
adapter's primary isolation mechanism is **env isolation** (below), not these flags.

**codex reads stdin until EOF.** The server now sends the complete prompt through a PIPE and
closes it with `communicate()`. This avoids OS `ARG_MAX` and prompt exposure in process argv;
leaving stdin open would still hang at 0% CPU. Output flushes at the end.
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
flag so codex uses its default, never 400-failing the whole review. Claude Code 2.1.198
also exposes `--effort <low|medium|high|xhigh|max>`; the adapter passes only this verified
enum and omits unknown values.

### 2d. Vendor-isolated runtime preparation

`HarnessProfile.prepare_runtime(runtime_dir=..., vendor=...)` requires an explicit vendor.
A Codex call creates only the Codex runtime and auth symlink and must not read the Claude
keychain. A Claude call creates only the Claude runtime and auth-only credentials and
must not inspect Codex auth. Review/retry/verification/Wiki/prescreen callers use a
vendor-specific temporary subdirectory; one vendor's auth-preparation failure degrades
only that vendor execution.

### 2e. Minimal auth env allowlist

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
| **argv (read-only probe)** | `claude -p` (`--permission-mode plan --tools "" --disable-slash-commands --model <m>` for no-tool prescreen) | `codex exec --skip-git-repo-check --sandbox read-only` |
| **stdin** | complete prompt via PIPE, then EOF | complete prompt via PIPE, then EOF (**mandatory** — else 0% hang) |
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
(no prose), so `last_token` matched exactly. That historical, operator-controlled spike
used local raw output for diagnosis. Production and the current telemetry preflight never
echo raw stdout/stderr or a non-compliant response; they emit only an allowlisted safe
error and retain diagnosis inside the bounded transient process buffer.

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
  (`.credentials.json`). `HarnessProfile.runtime_credentials()` removes the selected
  vendor credential on success, setup failure, cancellation, and timeout before the
  enclosing temporary directory exits. Residue or unlink failure emits only the safe
  diagnostic `runtime_cleanup_failed`; it cannot be reported as a successful vendor run.
  When cancellation/timeout is already active, that active exception remains primary and
  the cleanup diagnostic is attached/logged rather than masking cancellation. Never reuse
  an `RT` across runs.
- **python3 + macOS `security` are runtime prerequisites** of `preflight.sh`.

---

## 8. Structured telemetry contract (2026-07-22 recheck)

The opt-in probe is `scripts/review-cli-telemetry-preflight.py --live`. It prints only
schema-v2 JSON containing public CLI/schema versions, exit/safe-error status, reviewed
event type/key signatures, capped unknown-name counts, booleans, and telemetry presence.
It never prints prompts, responses, commands, paths, stdout, stderr, or dynamic unknown
key/type names. `--output <file>` writes the same sanitized schema and leaves stdout empty.

### Codex 0.144.5 — verified success

Invocation additions: `--json --output-last-message <confined-file> --ephemeral
--ignore-user-config --ignore-rules`.

Observed JSONL event types for a minimal no-tool call:

- `thread.started`
- `turn.started`
- `item.completed`
- `turn.completed`

`turn.completed.usage` contained numeric `input_tokens`, `cached_input_tokens`,
`output_tokens`, and `reasoning_output_tokens`. The final answer was written to the
confined `--output-last-message` file. Event bodies may contain model text, commands, and
tool output and therefore are transient parser input only; they are not persistence data.

### Claude 2.1.198 — terminal schema observed, successful billing unavailable

`--output-format stream-json` requires `--verbose`. The probe observed `system`,
`assistant`, and terminal `result` events. `result` included `is_error`,
`api_error_status`, `duration_ms`, `num_turns`, `usage`, and `modelUsage` fields. The
current account returned HTTP 403 for both tested aliases, so only the terminal error
schema—not a successful structured review—was verified. Production keeps Claude in
legacy text mode with `telemetry_status=unavailable`: `event_schema()` does not expose
Claude 2.1.198 to production adapters. The parser can recognize that schema only when an
explicit attestation path requests `attestation=True`; this does not activate structured
production execution. Activation requires a separate successful live attestation,
synthetic parser fixture, fresh review, and user-approved commit.

### Bounded preflight contract

- CLI version probes use a 4 KiB synchronous bounded reader; vendor probes share the
  production concurrent bounded runner with 10 MiB stdout/stderr caps and a 180-second
  wall-clock limit. Timeout/cancellation kills the dedicated process group, not only the
  direct CLI process.
- Parsers inspect at most 10 MiB / 20,000 events. Public signatures allow at most 64
  reviewed signatures, 64-character public key names, capped unknown counts, and a
  32 KiB total report.
- Output/final-message overflow produces only `output_limit`,
  `stream_truncated=true`, and the fixed report schema. Raw event bodies and partial final
  output are discarded. Prescreen source is sent only over stdin with `--tools ""`; its
  stdout/stderr and process group use the same bounded runner contract.
- Each selected vendor gets only its own temporary runtime credential; Claude keychain
  stdout is capped at 64 KiB with a 15-second timeout. Cleanup failure is represented by
  the safe `runtime_cleanup_failed` code and cannot produce a successful preflight result.

### Version and privacy rules

- Structured parsers dispatch by exact CLI version/event schema. Unknown versions fall
  back to the existing text path and mark telemetry unavailable.
- Do not parse the human stderr `tokens used` line as a production contract.
- Persist only allowlisted numeric/status fields. Tool commands, paths, prompt/response
  bodies, item payloads, stdout, and stderr are forbidden.
- Rate-limit and overload event taxonomies remain unverified and must fail to a generic
  allowlisted safe error code rather than persisting provider text.

---

## 9. Plain snapshot and read-containment boundary (2026-07-22)

Production review cwd is created from `git archive HEAD` and contains tracked PR-head
files without `.git`, refs, reflogs, or unreachable objects. `git ls-tree` first rejects
blob/file-count/total-byte overflow, then archive stdout is written through a byte-limited
stream so the temporary tar cannot grow past its configured cap. On the SmartDoctor clone the
snapshot preflight extracted 11,080 files / 62,433,844 bytes in about 9.2 seconds.

This is **not** an OS-level read sandbox. The opt-in safe-sentinel probe
`scripts/review-read-containment-preflight.py --live --repo <repo>` empirically returned:

```json
{"exit_code":0,"git_repo":false,"outside_readable":true,"read_containment":"unproven"}
```

Therefore:

- plain snapshot prevents accidental cwd-relative Git history exploration;
- Codex `--sandbox read-only` does not prevent absolute reads outside cwd;
- do not claim that persistent clones, adjacent worktrees, or runtime auth files are
  inaccessible to model-generated tools;
- strong read containment requires a separately verified path-enforcing tool broker or
  external runner architecture that can separate CLI credential access from tool-child
  filesystem access.

The preflight uses only a generated `SAFE_SENTINEL`; it never opens project files or
credentials and never prints provider output, commands, or paths.
