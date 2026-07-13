import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from server import config

_HARNESS_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_HARNESS_FILES = ("config.json", "tools-allowlist.json", "review-system-prompt.md")


def validate_harness_name(name: str) -> str:
    """디렉토리 traversal/임의 경로 주입 차단 — 소문자 kebab/snake만 허용."""
    if not _HARNESS_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid harness name: {name!r}")
    return name


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
    mcp: str
    model: str
    effort: str
    prescreen_model: str
    codex_model: str  # "" = codex CLI 자체 기본 모델(--model 미전달)

    @classmethod
    def load(cls, name: str) -> "HarnessProfile":
        base = config.HARNESS_DIR / name
        tools = json.loads((base / "tools-allowlist.json").read_text())
        cfg = json.loads((base / "config.json").read_text())
        return cls(
            name=name,
            system_prompt=(base / "review-system-prompt.md").read_text(),
            claude_allowed_tools=tools["claude_allowed_tools"],
            codex_sandbox=tools["codex_sandbox"],
            mcp=tools.get("mcp", "none"),
            model=cfg["model"],
            effort=cfg["effort"],
            prescreen_model=cfg.get("prescreen_model", "haiku"),
            codex_model=cfg.get("codex_model", ""),
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

    def prepare_runtime(self, *, runtime_dir: str) -> None:
        """runtime config dir 생성 + 인증 자격만 주입(전역 rules/skills/MCP는 안 함).
        부모 프로세스(실제 HOME)에서 review/prescreen 호출 전 1회 호출한다."""
        rt = Path(runtime_dir)
        for sub in ("claude", "codex", "config"):
            (rt / sub).mkdir(parents=True, exist_ok=True)
        _link_codex_auth(rt / "codex", Path.home() / ".codex" / "auth.json")
        _write_claude_credentials(rt / "claude", _read_claude_keychain())


def _read_claude_keychain() -> str:
    """macOS 키체인에서 'Claude Code-credentials' 원본 JSON을 반환(부모 프로세스, 실제 HOME).
    실패 시 secret 미노출로 명확히 raise."""
    proc = subprocess.run(
        ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(
            "claude keychain read failed — locked keychain or item "
            "'Claude Code-credentials' not found"
        )
    return proc.stdout


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
