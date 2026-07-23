import time

from server.context.base import ContextBlock, ContextRequest, ContextResult
from server.context.jira_keys import extract_keys

_MAX_ISSUES = (
    5  # 아웃바운드 호출 상한(B-INV-5/8 정신): PR이 많은 키를 참조해도 폭주 방지
)
# Jira 총 wall-clock 예산 — gather 총예산(15s) 아래로 묶어, 느린 Jira가 all-or-nothing
# 타임아웃을 유발해 이미 준비된 로컬 컨텍스트까지 폐기시키는 것을 방지(부분 결과 반환).
_TOTAL_BUDGET_SEC = 8


class JiraContextProvider:
    """추출된 Jira 키 → JiraClient.get_issue → 마크다운 블록. best-effort degrade.
    존재하지 않는 키는 조회 실패로 조용히 버려지므로 별도 프로젝트 필터를 두지 않는다."""

    name = "jira"

    def __init__(self, *, client):
        self._client = client

    def fetch(self, req: ContextRequest) -> ContextResult:
        keys = extract_keys(req)
        if not keys:
            return ContextResult(provider=self.name, status="empty", text="")
        blocks = []
        semantic_blocks = []
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
                summary = str(issue.get("summary") or "")
                if summary:
                    semantic_blocks.append(
                        ContextBlock(
                            source=self.name,
                            block_id=f"{issue['key']}:summary",
                            text=f"**{issue['key']}** {summary}",
                            priority=10,
                            recoverable_from_repo=False,
                            trust_class="untrusted_external",
                            sensitivity="internal",
                            retention="short",
                        )
                    )
                if acceptance:
                    semantic_blocks.append(
                        ContextBlock(
                            source=self.name,
                            block_id=f"{issue['key']}:acceptance",
                            text=str(acceptance),
                            priority=10,
                            recoverable_from_repo=False,
                            trust_class="untrusted_external",
                            sensitivity="internal",
                            retention="short",
                        )
                    )
                if body:
                    semantic_blocks.append(
                        ContextBlock(
                            source=self.name,
                            block_id=f"{issue['key']}:description",
                            text=str(body),
                            priority=20,
                            recoverable_from_repo=False,
                            trust_class="untrusted_external",
                            sensitivity="sensitive",
                            retention="short",
                        )
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
            provider=self.name,
            status="ok",
            text="\n\n---\n\n".join(blocks),
            blocks=tuple(semantic_blocks),
        )
