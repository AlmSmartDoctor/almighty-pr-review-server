"""현재 PR에 이미 남은 GitHub 리뷰/댓글을 제한된 참고 컨텍스트로 만든다."""

import re
from concurrent.futures import ThreadPoolExecutor

from server.context.base import ContextBlock, ContextResult
from server.formatter import MARKER

_MAX_ITEMS = 20
_MAX_BODY_CHARS = 600
_MAX_OUTPUT_CHARS = 4_000
_NOISE_MARKERS = (
    "slack 쓰레드:",
    "usage limits have been reached",
    "review_stack_entry_start",
    "walkthrough_start",
    "<!-- auto-generated comment --> walkthrough",
)
_AUTOMATED_REVIEW_MARKERS = (
    ("<!-- codex-auto-review -->", "codex"),  # legacy external reviewer marker
    (MARKER.format(vendor="codex").lower(), "codex"),
    (MARKER.format(vendor="claude").lower(), "claude"),
)


def _one_line(value: str) -> str:
    text = " ".join(value.split()) if isinstance(value, str) else ""
    return text[:_MAX_BODY_CHARS] + ("…" if len(text) > _MAX_BODY_CHARS else "")


def _author_data(item: dict) -> dict:
    value = item.get("author") or item.get("user")
    return value if isinstance(value, dict) else {}


def _author(item: dict) -> str:
    return _author_data(item).get("login", "unknown")


def _is_bot(item: dict) -> bool:
    author = _author_data(item)
    login = str(author.get("login") or "").lower()
    return author.get("__typename") == "Bot" or login.endswith("[bot]") or login in {
        "coderabbitai",
        "chatgpt-codex-connector",
    }


def _is_noise(body: str, *, keep_unresolved: bool = False) -> bool:
    lowered = body.lower()
    if any(marker in lowered for marker in _NOISE_MARKERS):
        return True
    # generic third-party boilerplate는 대화/요약에서는 버리되, 미해결 인라인 thread는
    # provenance와 무관하게 보존한다. 자동 생성 문구만으로 실제 미해결 논의를 잃지 않는다.
    return "auto-generated comment" in lowered and not keep_unresolved


def _automated_review_vendor(body: str) -> str | None:
    lowered = body.lower()
    for marker, vendor in _AUTOMATED_REVIEW_MARKERS:
        if marker in lowered:
            return vendor
    return None


def _without_automated_review_marker(body: str) -> str:
    cleaned = body
    for marker, _ in _AUTOMATED_REVIEW_MARKERS:
        cleaned = re.sub(re.escape(marker), "", cleaned, flags=re.IGNORECASE)
    return cleaned


def _summarize(*, reviews, inline_comments, conversation_comments) -> tuple[str, int, int]:
    items = []
    for review in reviews or []:
        if not isinstance(review, dict):
            continue
        raw_body = review.get("body", "")
        if not isinstance(raw_body, str) or not raw_body or _is_noise(raw_body):
            continue
        body = _one_line(_without_automated_review_marker(raw_body))
        state = str(review.get("state") or "COMMENTED").lower()
        automated_vendor = _automated_review_vendor(raw_body)
        automated = bool(automated_vendor) or _is_bot(review)
        label = f"자동 리뷰 {automated_vendor or '외부'}" if automated else "리뷰"
        items.append({
            "created_at": review.get("submitted_at") or review.get("submittedAt") or "",
            "identity": ("review", review.get("id")),
            "priority": 5 if automated else 2,
            "automated": automated,
            "line": f"- [{label} {state}] @{_author(review)}: {body}",
        })
    for comment in inline_comments or []:
        if not isinstance(comment, dict):
            continue
        raw_body = comment.get("body", "")
        resolved = comment.get("is_resolved")
        if (
            not isinstance(raw_body, str)
            or not raw_body
            or _is_noise(raw_body, keep_unresolved=resolved is False)
        ):
            continue
        body = _one_line(_without_automated_review_marker(raw_body))
        path = str(comment.get("path") or "파일 위치 미상")
        line = comment.get("line") or comment.get("original_line")
        location = f"{path}:{line}" if line else path
        state = "해결" if resolved is True else "미해결" if resolved is False else "상태 미상"
        automated_vendor = _automated_review_vendor(raw_body)
        automated = bool(automated_vendor) or _is_bot(comment)
        if resolved is False:
            priority = 1 if automated else 0
        elif resolved is True:
            priority = 5 if automated else 3
        else:
            priority = 5 if automated else 2
        label = f"자동 리뷰 {automated_vendor or '외부'} 인라인" if automated else "인라인"
        items.append({
            "created_at": comment.get("created_at") or "",
            "identity": ("inline", comment.get("id")),
            "priority": priority,
            "automated": automated,
            "line": f"- [{label} · {state} · {location}] @{_author(comment)}: {body}",
        })
    for comment in conversation_comments or []:
        if not isinstance(comment, dict):
            continue
        raw_body = comment.get("body", "")
        if not isinstance(raw_body, str) or not raw_body or _is_noise(raw_body):
            continue
        body = _one_line(_without_automated_review_marker(raw_body))
        automated_vendor = _automated_review_vendor(raw_body)
        automated = bool(automated_vendor) or _is_bot(comment)
        label = f"자동 리뷰 {automated_vendor or '외부'} 대화" if automated else "대화"
        items.append({
            "created_at": comment.get("created_at") or "",
            "identity": ("conversation", comment.get("id")),
            "priority": 5 if automated else 4,
            "automated": automated,
            "line": f"- [{label}] @{_author(comment)}: {body}",
        })
    if not items:
        return "", 0, 0

    # 같은 우선순위 안에서는 최신 의견부터. 미해결 thread와 사람 리뷰를 먼저 보존한다.
    items.sort(key=lambda item: item["created_at"], reverse=True)
    items.sort(key=lambda item: item["priority"])
    preamble = (
        "현재 PR에 이미 남은 리뷰와 댓글 — 중복 지적을 피하고 기존 논의의 반영 여부를 "
        "확인할 때만 참고:\n\n"
    )
    lines = []
    automated_selected = 0
    seen = set()
    used = len(preamble)
    for item in items:
        identity = item["identity"]
        line = item["line"]
        key = identity if identity[1] is not None else line
        if key in seen:
            continue
        extra = len(line) + (1 if lines else 0)
        if used + extra > _MAX_OUTPUT_CHARS:
            continue
        seen.add(key)
        lines.append(line)
        automated_selected += int(bool(item.get("automated")))
        used += extra
        if len(lines) >= _MAX_ITEMS:
            break
    return (
        (preamble + "\n".join(lines), len(lines), automated_selected)
        if lines else ("", 0, 0)
    )


def summarize_current_pr_reviews(*, reviews, inline_comments, conversation_comments) -> str:
    return _summarize(
        reviews=reviews,
        inline_comments=inline_comments,
        conversation_comments=conversation_comments,
    )[0]


def _collect_rest(gh, req) -> tuple[dict[str, list], list[str], int]:
    specs = (
        ("reviews", "list_pr_reviews"),
        ("inline_comments", "list_pr_review_comments"),
        ("conversation_comments", "list_pr_conversation_comments"),
    )
    collected: dict[str, list] = {}
    failures = []
    successes = 0
    # 각 gh 호출은 자체 3초 timeout을 갖는다. 병렬 실행해 GraphQL 5초 실패 뒤에도
    # fallback 총시간을 약 3초로 제한하고 다른 context provider의 15초 예산을 보존한다.
    def call(method_name):
        return getattr(gh, method_name)(req.repo, req.pr_number)

    with ThreadPoolExecutor(max_workers=len(specs), thread_name_prefix="gh-review-context") as pool:
        futures = {
            name: pool.submit(call, method_name) for name, method_name in specs
        }
        for name, _ in specs:
            try:
                value = futures[name].result()
                if not isinstance(value, list):
                    raise TypeError("GitHub REST response must be a list")
                collected[name] = value
                successes += 1
            except Exception:
                collected[name] = []
                failures.append(name)
    return collected, failures, successes


def _collect(gh, req) -> tuple[str, dict]:
    failures = []
    collected = None
    graphql = getattr(gh, "get_pr_review_context", None)
    if callable(graphql):
        try:
            candidate = graphql(req.repo, req.pr_number)
            if not isinstance(candidate, dict):
                raise TypeError("GitHub GraphQL response must be a mapping")
            collected = candidate
        except Exception:
            failures.append("graphql")

    if collected is None:
        collected, rest_failures, rest_successes = _collect_rest(gh, req)
        failures.extend(rest_failures)
        all_failed = rest_successes == 0
    else:
        all_failed = False

    reviews = collected.get("reviews") or []
    inline = collected.get("inline_comments") or []
    conversation = collected.get("conversation_comments") or []
    reviews = reviews if isinstance(reviews, list) else []
    inline = inline if isinstance(inline, list) else []
    conversation = conversation if isinstance(conversation, list) else []
    text, selected, automated_selected = _summarize(
        reviews=reviews,
        inline_comments=inline,
        conversation_comments=conversation,
    )
    meta = {
        "items_read": len(reviews) + len(inline) + len(conversation),
        "items_selected": selected,
        "automated_items_selected": automated_selected,
        "failed_sources": failures,
    }
    if all_failed:
        meta["error"] = "GitHub review context unavailable: " + ", ".join(failures)
    return text, meta


class CurrentPRReviewsProvider:
    name = "current_pr_reviews"

    def __init__(self, gh):
        self._gh = gh

    def fetch(self, req) -> ContextResult:
        text, meta = _collect(self._gh, req)
        error = meta.pop("error", None)
        return ContextResult(
            provider=self.name,
            status="error" if error else "ok" if text else "empty",
            text=text,
            meta=meta,
            error=error,
            blocks=(
                ContextBlock(
                    source=self.name,
                    block_id="current-pr-discussion",
                    text=text,
                    priority=0,
                    recoverable_from_repo=False,
                    trust_class="untrusted_external",
                    sensitivity="internal",
                    retention="short",
                ),
            ) if text else (),
        )


def current_pr_reviews_source(gh):
    """기존 source(req)->str seam과의 호환용 어댑터."""

    def source(req) -> str:
        return _collect(gh, req)[0]

    return source
