"""환경 의존성 실측 점검. /api/health(즉답 — dev.sh readiness 폴링용)와 달리
gh 인증·벤더 CLI 존재·DB 접근을 실제로 확인해 대시보드가 정직한 상태를 보여준다."""

import shutil

from server.github.gh import GitHubCliError


def deep_health(conn, gh, *, which=None) -> dict:
    which = which or shutil.which
    out = {
        "gh": {
            "installed": bool(which("gh")),
            "authenticated": False,
            "login": None,
            "error": None,
        },
        "claude": {"installed": bool(which("claude"))},
        "codex": {"installed": bool(which("codex"))},
        "db": {"ok": False},
    }
    if not out["gh"]["installed"]:
        out["gh"]["error"] = "gh CLI가 설치되어 있지 않습니다 (brew install gh)"
    else:
        try:
            out["gh"].update(
                {"authenticated": True, "login": gh.preflight_user().get("login")}
            )
        except GitHubCliError as e:
            out["gh"]["error"] = e.message
        except Exception as e:
            out["gh"]["error"] = str(e)
    try:
        conn.execute("SELECT 1")
        out["db"]["ok"] = True
    except Exception:
        pass
    # 리뷰가 실제로 돌 수 있는 최소 조건: gh 인증 + DB + 벤더 CLI 1개 이상.
    out["ok"] = bool(
        out["gh"]["authenticated"]
        and out["db"]["ok"]
        and (out["claude"]["installed"] or out["codex"]["installed"])
    )
    return out
