"""Read-only readiness checks for a registered review repository."""

import shutil
import subprocess
from pathlib import Path

from server.github.gh import GitHubCliError
from server.review.harness import list_harnesses


def _check(ok: bool, message: str) -> dict:
    return {"ok": ok, "message": message}


def check_repo_readiness(repo, gh, *, which=None, runner=None) -> dict:
    """GitHub/source/harness/vendor prerequisites without changing repository state."""
    which = which or shutil.which
    runner = runner or subprocess.run
    checks = {
        "enabled": _check(
            bool(repo["enabled"]),
            "레포 활성화됨" if repo["enabled"] else "레포가 비활성화되어 있습니다",
        )
    }

    try:
        found = gh.preflight_repo(repo["full_name"])
        canonical = found.get("full_name") or repo["full_name"]
        checks["github"] = _check(True, f"GitHub 접근 가능: {canonical}")
    except GitHubCliError as exc:
        checks["github"] = _check(False, exc.message)
    except Exception as exc:
        checks["github"] = _check(False, f"GitHub 확인 실패: {exc}")

    local_path = (repo["local_path"] or "").strip()
    if not local_path:
        checks["source"] = _check(True, "서비스 전용 clone 사용")
    else:
        path = Path(local_path).expanduser()
        if not path.is_dir():
            checks["source"] = _check(False, "로컬 경로가 존재하는 디렉터리가 아닙니다")
        else:
            try:
                proc = runner(
                    ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                ok = proc.returncode == 0 and proc.stdout.strip() == "true"
                checks["source"] = _check(
                    ok,
                    "로컬 Git 저장소 확인됨"
                    if ok
                    else "로컬 경로가 Git 저장소가 아닙니다",
                )
            except (OSError, subprocess.SubprocessError) as exc:
                checks["source"] = _check(False, f"로컬 Git 확인 실패: {exc}")

    harness_name = repo["harness_name"] or "default"
    harness_ok = harness_name in list_harnesses()
    checks["harness"] = _check(
        harness_ok,
        f"하네스 확인됨: {harness_name}"
        if harness_ok
        else f"하네스를 찾을 수 없습니다: {harness_name}",
    )

    enabled_vendors = [
        vendor
        for vendor in ("claude", "codex")
        if bool(repo[f"vendor_{vendor}_on"])
    ]
    if not enabled_vendors:
        checks["vendors"] = _check(False, "활성 vendor가 없습니다")
    else:
        missing = [vendor for vendor in enabled_vendors if not which(vendor)]
        checks["vendors"] = _check(
            not missing,
            "활성 vendor CLI 확인됨: " + ", ".join(enabled_vendors)
            if not missing
            else "설치되지 않은 활성 vendor CLI: " + ", ".join(missing),
        )

    return {
        "repo_id": repo["id"],
        "repo": repo["full_name"],
        "ready": all(item["ok"] for item in checks.values()),
        "checks": checks,
    }
