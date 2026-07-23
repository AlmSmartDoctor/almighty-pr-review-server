import json
import os
import re
import shutil
import signal
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from server import config

_HARNESS_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_HARNESS_FILES = ("config.json", "tools-allowlist.json", "review-system-prompt.md")
VENDORS = ("claude", "codex")
_KEYCHAIN_MAX_BYTES = 64 * 1024
_KEYCHAIN_TIMEOUT_SEC = 15


class RuntimeCredentialError(RuntimeError):
    def __init__(self, safe_error_code: str):
        self.safe_error_code = safe_error_code
        super().__init__(safe_error_code)


def cleanup_failure_code(exc: BaseException) -> str | None:
    codes = {"runtime_cleanup_failed", "snapshot_cleanup_failed"}
    direct = getattr(exc, "safe_error_code", None)
    if direct in codes:
        return direct
    for note in getattr(exc, "__notes__", ()):
        if note in codes:
            return note
    return None


def validate_harness_name(name: str) -> str:
    """디렉토리 traversal/임의 경로 주입 차단 — 소문자 kebab/snake만 허용."""
    if not _HARNESS_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid harness name: {name!r}")
    return name


def _vendor_prompt_file(vendor: str) -> str:
    if vendor not in VENDORS:
        raise ValueError(f"invalid vendor: {vendor!r}")
    return f"review-system-prompt.{vendor}.md"


def set_vendor_prompt(name: str, vendor: str, text: str) -> None:
    """하네스에 벤더별 system prompt 오버라이드를 기록한다. text가 비면 오버라이드
    파일을 제거해 공통 지침으로 되돌린다."""
    validate_harness_name(name)
    fname = _vendor_prompt_file(vendor)
    base = config.HARNESS_DIR / name
    if not base.is_dir():
        raise ValueError(f"harness not found: {name!r}")
    dest = base / fname
    if text:
        dest.write_text(text)
    elif dest.exists():
        dest.unlink()


def list_harnesses() -> list[str]:
    """HARNESS_DIR에서 필수 3파일을 모두 갖춘 하네스 디렉토리 이름을 정렬 반환."""
    base = config.HARNESS_DIR
    if not base.is_dir():
        return []
    return sorted(
        d.name
        for d in base.iterdir()
        if d.is_dir() and all((d / f).exists() for f in _HARNESS_FILES)
    )


def create_harness(name: str, *, system_prompt: str | None = None) -> None:
    """default에서 config/tools를 복사해 새 하네스를 스캐폴드(이미 있으면 ValueError).
    system_prompt 미지정 시 default의 리뷰 지침을 상속한다."""
    validate_harness_name(name)
    dest = config.HARNESS_DIR / name
    if dest.exists():
        raise ValueError(f"harness already exists: {name!r}")
    src = config.HARNESS_DIR / "default"
    dest.mkdir(parents=True)
    for f in ("config.json", "tools-allowlist.json"):
        shutil.copyfile(src / f, dest / f)
    prompt = (
        system_prompt
        if system_prompt is not None
        else (src / "review-system-prompt.md").read_text()
    )
    (dest / "review-system-prompt.md").write_text(prompt)


@dataclass
class HarnessProfile:
    name: str
    system_prompt: str
    claude_allowed_tools: list[str]
    codex_sandbox: str
    model: str  # pipeline._apply_models가 레포·전역 설정으로 채움(하네스엔 미보관)
    effort: str  # 〃 claude reasoning effort(--effort)
    codex_model: str  # 〃 "" = codex CLI 자체 기본 모델(--model 미전달)
    codex_effort: str = ""  # 〃 codex reasoning effort(-c model_reasoning_effort)
    # 벤더별 system prompt 오버라이드(파일 있을 때만). 없으면 공통 지침으로 폴백.
    vendor_prompts: dict[str, str] = field(default_factory=dict)

    def system_prompt_for(self, vendor: str) -> str:
        """벤더별 오버라이드가 있으면 그것, 없으면 공통 지침으로 폴백."""
        return self.vendor_prompts.get(vendor) or self.system_prompt

    @classmethod
    def load(cls, name: str) -> "HarnessProfile":
        # 하네스 = system_prompt + 도구 allowlist + 샌드박스. 모델/effort는 보관하지 않고
        # pipeline._apply_models가 레포·전역 설정으로 전량 채운다(그래서 여기선 "").
        base = config.HARNESS_DIR / name
        tools = json.loads((base / "tools-allowlist.json").read_text())
        vendor_prompts = {
            v: (base / _vendor_prompt_file(v)).read_text()
            for v in VENDORS
            if (base / _vendor_prompt_file(v)).exists()
        }
        return cls(
            name=name,
            system_prompt=(base / "review-system-prompt.md").read_text(),
            claude_allowed_tools=tools["claude_allowed_tools"],
            codex_sandbox=tools["codex_sandbox"],
            model="",
            effort="",
            codex_model="",
            codex_effort="",
            vendor_prompts=vendor_prompts,
        )

    # 인증에 필요한 env allowlist(키체인 접근 등). 정확한 목록은 Task 0.5 실증값.
    AUTH_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "TERM", "SHELL", "USER", "LOGNAME")

    def isolated_env(self, *, runtime_dir: str) -> dict:
        """전역 프로파일 미상속 + 인증은 유지(★개정). HOME/config dir을 runtime로
        재지정해 전역 rules/skills/MCP는 차단하되, 인증에 필요한 최소 env만 allowlist로 통과."""
        rt = Path(runtime_dir)
        env = {k: os.environ[k] for k in self.AUTH_ENV_KEYS if k in os.environ}
        env.update(
            {
                "HOME": str(rt),
                "XDG_CONFIG_HOME": str(rt / "config"),
                "CLAUDE_CONFIG_DIR": str(rt / "claude"),
                "CODEX_HOME": str(rt / "codex"),
            }
        )
        return env

    def prepare_runtime(self, *, runtime_dir: str, vendor: str) -> None:
        """선택 vendor의 runtime/auth만 준비한다.

        전역 rules/skills/MCP는 복사하지 않으며, 한 vendor 실행이 다른 vendor의
        credential을 materialize하거나 keychain을 읽지 않도록 fail-closed한다.
        """
        if vendor not in VENDORS:
            raise ValueError(f"invalid vendor: {vendor!r}")
        rt = Path(runtime_dir)
        (rt / "config").mkdir(parents=True, exist_ok=True)
        vendor_dir = rt / vendor
        vendor_dir.mkdir(parents=True, exist_ok=True)
        if vendor == "codex":
            _link_codex_auth(vendor_dir, Path.home() / ".codex" / "auth.json")
        else:
            _write_claude_credentials(vendor_dir, _read_claude_keychain())

    def cleanup_runtime(self, *, runtime_dir: str, vendor: str) -> None:
        if vendor not in VENDORS:
            raise ValueError(f"invalid vendor: {vendor!r}")
        credential = Path(runtime_dir) / vendor / (
            ".credentials.json" if vendor == "claude" else "auth.json"
        )
        try:
            credential.unlink(missing_ok=True)
        except OSError as exc:
            raise RuntimeCredentialError("runtime_cleanup_failed") from exc
        if credential.exists() or credential.is_symlink():
            raise RuntimeCredentialError("runtime_cleanup_failed")

    @contextmanager
    def runtime_credentials(self, *, runtime_dir: str, vendor: str):
        """Prepare one vendor credential and prove its removal on every exit path."""
        if vendor not in VENDORS:
            raise ValueError(f"invalid vendor: {vendor!r}")
        try:
            self.prepare_runtime(runtime_dir=runtime_dir, vendor=vendor)
        except Exception:
            try:
                self.cleanup_runtime(runtime_dir=runtime_dir, vendor=vendor)
            except RuntimeCredentialError:
                raise
            raise RuntimeCredentialError("runtime_setup_failed") from None
        active_error = None
        try:
            yield
        except BaseException as exc:
            active_error = exc
            raise
        finally:
            try:
                self.cleanup_runtime(runtime_dir=runtime_dir, vendor=vendor)
            except RuntimeCredentialError as cleanup_error:
                if active_error is None:
                    raise
                active_error.add_note(cleanup_error.safe_error_code)
                print(f"[runtime] {cleanup_error.safe_error_code}")


def _read_claude_keychain() -> str:
    """Read a bounded keychain payload without exposing provider stderr or secret bytes."""
    limit = _KEYCHAIN_MAX_BYTES
    proc = subprocess.Popen(
        ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=(os.name == "posix"),
    )
    kept = bytearray()
    truncated = False

    def drain():
        nonlocal truncated
        try:
            while True:
                chunk = proc.stdout.read(16 * 1024)
                if not chunk:
                    break
                remaining = limit - len(kept)
                if remaining > 0:
                    kept.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated = True
        except (OSError, ValueError):
            truncated = True

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        proc.wait(timeout=_KEYCHAIN_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        else:
            proc.kill()
        proc.wait()
        raise RuntimeError("claude keychain read failed") from None
    finally:
        reader.join(timeout=1)
        if reader.is_alive():
            truncated = True
            if os.name == "posix":
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
            proc.stdout.close()
            reader.join(timeout=1)
    value = bytes(kept).decode("utf-8", "replace")
    if proc.returncode != 0 or truncated or not value.strip():
        raise RuntimeError("claude keychain read failed")
    return value


def _write_claude_credentials(claude_dir: Path, keychain_json: str) -> None:
    """claudeAiOauth만 추출(mcpOAuth 제외)해 .credentials.json에 0600으로 원자적 기록."""
    data = json.loads(keychain_json)
    if "claudeAiOauth" not in data:
        raise RuntimeError("claude keychain item missing 'claudeAiOauth' field")
    dest = claude_dir / ".credentials.json"
    fd = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"claudeAiOauth": data["claudeAiOauth"]}, f)


def _link_codex_auth(codex_dir: Path, source: Path) -> None:
    """파일 기반 codex auth를 read-only symlink로 주입(source 없으면 skip)."""
    if source.exists():
        link = codex_dir / "auth.json"
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(source)
