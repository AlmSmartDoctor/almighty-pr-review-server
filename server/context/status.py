"""설정 UI용 컨텍스트 소스 준비 상태.

자격 증명 값은 절대 반환하지 않고, 설정 여부와 누락된 환경 변수 이름만 노출한다.
"""

from server import config
from server.repos import repo_repo, settings_repo


_SOURCE_KEYS = (
    "context_static_on",
    "context_jira_on",
    "context_db_schema_on",
    "context_graphify_on",
    "context_feedback_on",
    "context_current_pr_reviews_on",
)


def _effective(repo: dict, settings: dict, key: str) -> bool:
    value = repo.get(key)
    return bool(settings.get(key, 0) if value is None else value)


def context_source_status(conn) -> dict:
    settings = dict(settings_repo.get(conn))
    repos = [dict(row) for row in repo_repo.list_enabled(conn)]

    effective_repos = {
        key: [repo for repo in repos if _effective(repo, settings, key)]
        for key in _SOURCE_KEYS
    }
    enabled_counts = {key: len(rows) for key, rows in effective_repos.items()}
    jira_requirements = {
        "ALMIGHTY_JIRA_BASE_URL": bool(config.JIRA_BASE_URL),
        "ALMIGHTY_JIRA_EMAIL": bool(config.JIRA_EMAIL),
        "ALMIGHTY_JIRA_API_TOKEN": bool(config.JIRA_API_TOKEN),
    }
    jira_missing = [name for name, configured in jira_requirements.items() if not configured]
    live_db_requirements = {
        "ALMIGHTY_MSSQL_GATEWAY_URL": bool(config.MSSQL_GATEWAY_URL),
        "ALMIGHTY_MSSQL_GATEWAY_TOKEN": bool(config.MSSQL_GATEWAY_TOKEN),
    }
    live_db_missing = [
        name for name, configured in live_db_requirements.items() if not configured
    ]
    static_paths = sum(
        bool(repo.get("static_context_path"))
        for repo in effective_repos["context_static_on"]
    )
    db_targets = sum(
        bool(repo.get("db_schema_path") or repo.get("live_db_target_id"))
        for repo in effective_repos["context_db_schema_on"]
    )

    return {
        "total_repos": len(repos),
        "sources": {
            "context_static_on": {
                "available": True,
                "status": "ready",
                "message": "AGENTS.md와 CLAUDE.md를 자동으로 찾습니다.",
                "enabled_repos": enabled_counts["context_static_on"],
                # 고정 경로는 선택 사항이다. effective-on 레포는 기본 지침 자동 탐색이
                # 가능하므로 준비됨으로 세고, 명시 경로 수는 별도 안전 메타로 노출한다.
                "configured_repos": enabled_counts["context_static_on"],
                "explicit_path_repos": static_paths,
                "missing": [],
            },
            "context_jira_on": {
                "available": not jira_missing,
                "status": "ready" if not jira_missing else "needs_server_setup",
                "message": (
                    "Jira 연결 환경 변수가 준비되었습니다."
                    if not jira_missing
                    else "서버에 Jira 연결 정보를 먼저 설정해야 합니다."
                ),
                "enabled_repos": enabled_counts["context_jira_on"],
                "configured_repos": (
                    enabled_counts["context_jira_on"] if not jira_missing else 0
                ),
                "missing": jira_missing,
            },
            "context_db_schema_on": {
                "available": True,
                "status": "ready",
                "message": "스키마 파일 또는 레포에 복사된 Safe-DB 보호 연결을 사용합니다.",
                "enabled_repos": enabled_counts["context_db_schema_on"],
                "configured_repos": db_targets,
                "missing": [],
                "capabilities": {
                    "file_schema": {"available": True, "missing": []},
                    "safe_db": {"vendored": True, "runtime_dependency": False},
                    "live_db": {
                        "available": not live_db_missing,
                        "missing": live_db_missing,
                    },
                },
            },
            "context_graphify_on": {
                "available": True,
                "status": "ready",
                "message": "서버에 저장된 다른 열린 PR의 미결 지적을 사용합니다.",
                "enabled_repos": enabled_counts["context_graphify_on"],
                "configured_repos": enabled_counts["context_graphify_on"],
                "missing": [],
            },
            "context_feedback_on": {
                "available": True,
                "status": "ready",
                "message": "별도 설정 없이 저장된 사람의 판정 이력을 사용합니다.",
                "enabled_repos": enabled_counts["context_feedback_on"],
                "configured_repos": enabled_counts["context_feedback_on"],
                "missing": [],
            },
            "context_current_pr_reviews_on": {
                "available": True,
                "status": "ready",
                "message": "GitHub에서 현재 PR의 기존 리뷰와 댓글을 읽습니다.",
                "enabled_repos": enabled_counts["context_current_pr_reviews_on"],
                "configured_repos": enabled_counts["context_current_pr_reviews_on"],
                "missing": [],
            },
        },
    }
