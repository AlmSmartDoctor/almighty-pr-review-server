from server import config
from server.context.base import redact_secrets
from server.context.composite import CompositeContextProvider
from server.context.static_provider import StaticContextProvider


# 컨텍스트 렌더/잘림 우선순위(작을수록 먼저 렌더 → 총량 상한 초과 시 나중에 잘림).
_CONTEXT_ORDER = {
    "team_feedback": 0,
    "db_schema": 1,
    "jira": 2,
    "static": 3,
    "graphify": 4,
}


def _effective(repo, settings, key):
    """per-repo 값 우선(NULL=global 상속), 양측 다 없으면 0(off). (D3)"""
    v = repo[key] if key in repo.keys() else None
    if v is not None:
        return v
    return settings[key] if key in settings.keys() else 0


def _ref(repo, key):
    return repo[key] if key in repo.keys() else None


def _compose_sources(*sources):
    """None을 걸러 여러 graph_source(req)->str 를 하나로 합친다. 남는 게 없으면 None
    (→ provider skipped). 각 소스는 독립 예외 격리 후 비어있지 않은 출력만 "\\n\\n"으로 잇는다
    — 한 소스 실패가 다른 소스 기여를 죽이지 않는다."""
    live = [s for s in sources if s is not None]
    if not live:
        return None

    def combined(req) -> str:
        parts = []
        for s in live:
            try:
                out = s(req) or ""
            except Exception:
                out = ""
            if out.strip():
                parts.append(out)
        return "\n\n".join(parts)

    return combined


def build_context_provider(repo, settings):
    """활성 프로바이더 조립. 생성은 절대 예외를 밖으로 던지지 않는다(B-INV-4/D6)."""
    providers = []
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
            providers.append(JiraContextProvider(client=client))
        except Exception as e:  # 생성 실패 = 드롭+redact 로그, never raise
            print(f"[context] jira provider skipped: {redact_secrets(str(e))}")
    if _effective(repo, settings, "context_db_schema_on"):
        try:
            from server.context.db_schema_source import file_schema_source
            from server.context.source_provider import SourceBackedProvider

            db_schema_path = _ref(repo, "db_schema_path")
            source = (
                file_schema_source(path=db_schema_path, root=_ref(repo, "local_path"))
                if db_schema_path
                else None
            )
            providers.append(SourceBackedProvider("db_schema", source=source))
        except Exception as e:  # never raise
            print(f"[context] db_schema provider skipped: {redact_secrets(str(e))}")
    if _effective(repo, settings, "context_graphify_on"):
        try:
            from server.context.graphify_source import file_project_source
            from server.context.server_data_source import open_findings_source
            from server.context.source_provider import SourceBackedProvider

            graphify_path = _ref(repo, "graphify_path")
            doc_source = (
                file_project_source(path=graphify_path, root=_ref(repo, "local_path"))
                if graphify_path
                else None
            )
            # 프로젝트 문서(있으면) + 다른 열린 PR의 미결 지적(교차 PR 일관성)만 주입.
            # open-PR 목록·리뷰 활동 통계는 결함 탐지 신호가 아니라 프롬프트를 희석하므로
            # 주입하지 않는다(어느 경로에도 노출 안 함 — 해당 소스는 제거됨).
            source = _compose_sources(doc_source, open_findings_source())
            providers.append(SourceBackedProvider("graphify", source=source))
        except Exception as e:  # never raise
            print(f"[context] graphify provider skipped: {redact_secrets(str(e))}")
    if _effective(repo, settings, "context_feedback_on"):
        try:
            from server.context.feedback_source import db_feedback_source
            from server.context.source_provider import SourceBackedProvider

            # per-repo 경로 없음 — 소스가 앱 DB에서 이 레포 결정을 읽는다.
            # 결정이 없으면 소스가 ""를 반환해 자동 미주입.
            providers.append(
                SourceBackedProvider("team_feedback", source=db_feedback_source())
            )
        except Exception as e:  # never raise
            print(f"[context] feedback provider skipped: {redact_secrets(str(e))}")
    # 렌더 순서 = 총량 상한(20K) 초과 시 꼬리부터 잘림. 결함 탐지에 가장 유용하고 작은
    # 신호(팀 피드백 보정·diff 선택 스키마)를 앞에, 크고 정적인 문서(graphify)를 뒤에 둬
    # 잘림이 발생해도 고가치 신호가 살아남게 한다.
    providers.sort(key=lambda p: _CONTEXT_ORDER.get(getattr(p, "name", ""), 99))
    return CompositeContextProvider(providers)
