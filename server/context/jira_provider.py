import time

from server.context.base import ContextRequest, ContextResult
from server.context.jira_keys import extract_keys

_MAX_ISSUES = (
    5  # 아웃바운드 호출 상한(B-INV-5/8 정신): PR이 많은 키를 참조해도 폭주 방지
)
# Jira 총 wall-clock 예산 — gather 총예산(15s) 아래로 묶어, 느린 Jira가 all-or-nothing
# 타임아웃을 유발해 이미 준비된 로컬 컨텍스트까지 폐기시키는 것을 방지(부분 결과 반환).
_TOTAL_BUDGET_SEC = 8


class JiraContextProvider:
    """추출된 Jira 키 → JiraClient.get_issue → 마크다운 블록. best-effort degrade.
    project_keys(레포 allowlist)가 있으면 해당 프로젝트 접두어만 조회한다."""

    name = "jira"

    def __init__(self, *, client, project_keys=()):
        self._client = client
        self._project_keys = tuple(project_keys)

    def fetch(self, req: ContextRequest) -> ContextResult:
        if not self._project_keys:
            return ContextResult(
                provider=self.name,
                status="skipped",
                text="",
                error="jira project allowlist required",
            )
        keys = extract_keys(req)
        keys = [k for k in keys if k.split("-")[0] in self._project_keys]
        if not keys:
            return ContextResult(provider=self.name, status="empty", text="")
        blocks = []
        deadline = time.monotonic() + _TOTAL_BUDGET_SEC
        for key in keys[:_MAX_ISSUES]:
            if time.monotonic() > deadline:  # 예산 초과 → 남은 키 스킵(부분 결과)
                break
            try:
                issue = self._client.get_issue(key)
                body = issue.get("description") or ""
                acceptance = issue.get("acceptance_criteria") or ""
                block = f"**{issue['key']}: {issue.get('summary', '')}**\n\n{body}"
                if acceptance:
                    block += f"\n\n**Acceptance criteria**\n\n{acceptance}"
                blocks.append(block.rstrip())
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
