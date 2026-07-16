import re

from server.context.base import ContextRequest

_KEY_RE = re.compile(r"[A-Z][A-Z0-9]+-\d+")


def find_keys(*texts: str | None) -> list[str]:
    """주어진 텍스트들에서 Jira 이슈 키를 첫 등장 순서로 중복 없이 추출한다."""
    keys: list[str] = []
    for text in texts:
        for m in _KEY_RE.findall(text or ""):
            if m not in keys:
                keys.append(m)
    return keys


def extract_keys(req: ContextRequest) -> list[str]:
    """PR에서 Jira 이슈 키를 추출한다. 우선순위 head_ref → title → body.
    base_ref는 파싱하지 않는다(릴리스/베이스 브랜치 오탐 차단, B-INV-7).
    중복은 첫 등장 순서를 보존해 제거. 미발견 시 []."""
    return find_keys(req.head_ref, req.title, req.body)
