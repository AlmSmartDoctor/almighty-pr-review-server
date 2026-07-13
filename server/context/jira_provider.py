from server.context.base import ContextRequest, ContextResult
from server.context.jira_keys import extract_keys

_MAX_ISSUES = (
    5  # 아웃바운드 호출 상한(B-INV-5/8 정신): PR이 많은 키를 참조해도 폭주 방지
)


class JiraContextProvider:
    """추출된 Jira 키 → JiraClient.get_issue → 마크다운 블록. best-effort degrade.
    project_keys(레포 allowlist)가 있으면 해당 프로젝트 접두어만 조회한다."""

    name = "jira"

    def __init__(self, *, client, project_keys=()):
        self._client = client
        self._project_keys = tuple(project_keys)

    def fetch(self, req: ContextRequest) -> ContextResult:
        keys = extract_keys(req)
        if self._project_keys:
            keys = [k for k in keys if k.split("-")[0] in self._project_keys]
        if not keys:
            return ContextResult(provider=self.name, status="empty", text="")
        blocks = []
        for key in keys[:_MAX_ISSUES]:
            try:
                issue = self._client.get_issue(key)
                body = issue.get("description") or ""
                blocks.append(
                    f"**{issue['key']}: {issue.get('summary', '')}**\n\n{body}".rstrip()
                )
            except Exception:  # per-key degrade; error 세부는 노출 안 함(B-INV-4)
                continue
        if not blocks:
            # 키는 있었으나 전부 실패 → 비밀 없는 카운트만
            return ContextResult(
                provider=self.name,
                status="error",
                text="",
                error=f"jira fetch failed for {len(keys)} key(s)",
            )
        return ContextResult(
            provider=self.name, status="ok", text="\n\n---\n\n".join(blocks)
        )
