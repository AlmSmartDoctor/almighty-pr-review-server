import hashlib
import hmac
import json

# 새 head sha가 생기거나(open/synchronize) PR이 재개(reopened)될 때만 리뷰 트리거.
_REVIEW_ACTIONS = frozenset({"opened", "synchronize", "reopened"})


def verify_signature(secret: str, body: bytes, header: "str | None") -> bool:
    """X-Hub-Signature-256 HMAC 검증. 시크릿 미설정/헤더 없음/불일치 → False.
    raw body 바이트로 계산해야 한다(JSON 재직렬화 금지 — 바이트가 달라짐). 상수시간 비교.
    바이트로 비교한다 — compare_digest(str,str)는 비ASCII 헤더에 TypeError를 던지므로
    (Starlette가 헤더 raw 바이트를 latin-1로 디코드) 위조 서명이 500을 유발할 수 있다."""
    if not secret or not header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(
        expected.encode(), header.encode("utf-8", "surrogatepass")
    )


def _dict(v):
    """중첩 필드가 dict일 때만 그대로, 아니면 {} — 적대적 payload(비-dict 중첩)로 인한
    `.get()` AttributeError(→500)를 막는다. `x or {}`는 truthy 비-dict를 통과시켜 부족."""
    return v if isinstance(v, dict) else {}


def parse_pull_request_event(body: bytes):
    """pull_request 이벤트 payload에서 리뷰 대상 PR 필드를 뽑는다.
    리뷰 트리거 action이 아니거나(JSON 파싱 실패 포함) 필수 필드가 없으면 None(무시).
    중첩 필드는 _dict()로 감싸 비-dict 값이 와도 절대 raise하지 않는다."""
    try:
        p = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(p, dict) or p.get("action") not in _REVIEW_ACTIONS:
        return None
    pr = _dict(p.get("pull_request"))
    repo = _dict(p.get("repository"))
    full_name = repo.get("full_name")
    head = _dict(pr.get("head")).get("sha")
    number = pr.get("number")
    if not full_name or not head or number is None:
        return None
    return {
        "full_name": full_name,
        "number": number,
        "head_sha": head,
        "base_ref": _dict(pr.get("base")).get("ref", "") or "",
        "head_ref": _dict(pr.get("head")).get("ref", "") or "",
        "title": pr.get("title") or "",
        "author": _dict(pr.get("user")).get("login", "") or "",
        "url": pr.get("html_url") or "",
        "state": pr.get("state") or "open",
        "body": pr.get("body") or "",
    }
