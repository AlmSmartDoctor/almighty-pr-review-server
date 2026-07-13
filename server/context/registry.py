import re

from server import config
from server.context.base import redact_secrets
from server.context.composite import CompositeContextProvider
from server.context.static_provider import StaticContextProvider


def _effective(repo, settings, key):
    """per-repo 값 우선(NULL=global 상속), 양측 다 없으면 0(off). (D3)"""
    v = repo[key] if key in repo.keys() else None
    if v is not None:
        return v
    return settings[key] if key in settings.keys() else 0


def _ref(repo, key):
    return repo[key] if key in repo.keys() else None


def _parse_keys(s):
    return tuple(
        t
        for t in (s or "").replace(",", " ").split()
        if re.fullmatch(r"[A-Z][A-Z0-9]+", t)
    )


def build_context_provider(repo, settings):
    """활성 프로바이더 조립. 생성은 절대 예외를 밖으로 던지지 않는다(B-INV-4/D6)."""
    providers = []
    jira_project_keys = _parse_keys(_ref(repo, "jira_project_keys"))
    if _effective(repo, settings, "context_static_on") and _ref(
        repo, "static_context_path"
    ):
        try:
            providers.append(
                StaticContextProvider(
                    path=_ref(repo, "static_context_path"), root=repo["local_path"]
                )
            )
        except Exception as e:  # 생성 실패 = 드롭+로그, never raise
            print(f"[context] static provider skipped: {redact_secrets(str(e))}")
    if (
        _effective(repo, settings, "context_jira_on")
        and config.JIRA_BASE_URL
        and config.JIRA_EMAIL
        and config.JIRA_API_TOKEN
        and jira_project_keys
    ):
        try:
            from server.context.jira_client import JiraClient
            from server.context.jira_provider import JiraContextProvider

            client = JiraClient(
                base_url=config.JIRA_BASE_URL,
                email=config.JIRA_EMAIL,
                token=config.JIRA_API_TOKEN,
                acceptance_criteria_field=config.JIRA_ACCEPTANCE_CRITERIA_FIELD,
            )
            providers.append(
                JiraContextProvider(
                    client=client,
                    project_keys=jira_project_keys,
                )
            )
        except Exception as e:  # 생성 실패 = 드롭+redact 로그, never raise
            print(f"[context] jira provider skipped: {redact_secrets(str(e))}")
    if _effective(repo, settings, "context_db_schema_on"):
        try:
            from server.context.db_schema_provider import DBSchemaProvider

            providers.append(DBSchemaProvider())
        except Exception as e:  # never raise
            print(f"[context] db_schema provider skipped: {redact_secrets(str(e))}")
    if _effective(repo, settings, "context_graphify_on"):
        try:
            from server.context.graphify_provider import GraphifyProvider

            providers.append(GraphifyProvider())
        except Exception as e:  # never raise
            print(f"[context] graphify provider skipped: {redact_secrets(str(e))}")
    return CompositeContextProvider(providers)
