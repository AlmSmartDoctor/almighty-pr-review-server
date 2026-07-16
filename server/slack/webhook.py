import hashlib
import hmac
import json
import time

_REACTION_EVENTS = frozenset({"reaction_added", "reaction_removed"})
_MAX_AGE_SEC = (
    300  # Slack replay 윈도우(5분) — 이보다 오래된 타임스탬프는 재생 공격으로 거부
)

# 관심 이모지 → verdict. Slack은 이모지 이름을 colon 없이 보낸다("+1", "thumbsup" 등).
# 피부톤 변형("thumbsup::skin-tone-2")은 base 이름으로 정규화한다. 목록 밖 이모지는 무시.
_POSITIVE = frozenset(
    {
        "+1",
        "thumbsup",
        "white_check_mark",
        "heavy_check_mark",
        "tada",
        "raised_hands",
        "100",
    }
)
_NEGATIVE = frozenset({"-1", "thumbsdown", "x", "no_entry", "no_entry_sign"})


def verdict_for(reaction: str) -> "str | None":
    base = (reaction or "").split("::")[0]
    if base in _POSITIVE:
        return "positive"
    if base in _NEGATIVE:
        return "negative"
    return None


def verify_signature(
    secret: str, timestamp: "str | None", body: bytes, header: "str | None"
) -> bool:
    """X-Slack-Signature HMAC 검증. 시크릿/타임스탬프/헤더 없음·불일치 → False.
    GitHub 웹훅과 달리 Slack은 `v0:{timestamp}:{body}`에 서명하고 `v0=` 접두를 붙인다.
    raw body 바이트로 계산(재직렬화 금지). 바이트로 상수시간 비교 — compare_digest(str,str)는
    비ASCII 헤더에 TypeError(→500)를 던지므로 위조 서명이 500이 되는 걸 막는다."""
    if not secret or not header or not timestamp:
        return False
    basestring = b"v0:" + timestamp.encode("utf-8", "surrogatepass") + b":" + body
    expected = "v0=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(
        expected.encode(), header.encode("utf-8", "surrogatepass")
    )


def is_fresh(timestamp: "str | None", *, now=None, max_age_sec=_MAX_AGE_SEC) -> bool:
    """타임스탬프가 replay 윈도우(기본 5분) 안인지. 서명이 유효해도 오래된 요청이면 재생
    공격이므로 거부한다(Slack 규약). 비수치/없음 → False. now 주입으로 테스트 결정성 확보."""
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    now = time.time() if now is None else now
    return abs(now - ts) <= max_age_sec


def _dict(v):
    """중첩 필드가 dict일 때만 그대로, 아니면 {} — 적대적 payload로 인한 .get() 500 방지."""
    return v if isinstance(v, dict) else {}


def parse_event(body: bytes):
    """Slack Events payload에서 관심 이벤트를 뽑는다(url_verification 챌린지 / reaction).
    JSON 파싱 실패·관심 밖 이벤트·필수 필드 없음이면 None(무시). 절대 raise하지 않는다."""
    try:
        p = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(p, dict):
        return None
    if p.get("type") == "url_verification":
        challenge = p.get("challenge")
        return {
            "type": "url_verification",
            "challenge": challenge if isinstance(challenge, str) else "",
        }
    if p.get("type") != "event_callback":
        return None
    ev = _dict(p.get("event"))
    if ev.get("type") not in _REACTION_EVENTS:
        return None
    item = _dict(ev.get("item"))
    channel = item.get("channel")
    ts = item.get("ts")
    reaction = ev.get("reaction")
    if not channel or not ts or not reaction:
        return None
    return {
        "type": "reaction",
        "action": "added" if ev["type"] == "reaction_added" else "removed",
        "channel": channel,
        "ts": ts,
        "reaction": reaction,
        "user": ev.get("user") or "",
    }
