"""Opaque, integrity-protected pagination cursor helpers."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from server import config

CURSOR_VERSION = 1
MAX_CURSOR_BYTES = 4096
_RESOURCES = frozenset({"overview", "pr-runs", "run-findings"})


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    if not value or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for ch in value):
        raise ValueError("invalid cursor")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid cursor") from exc


def _key() -> bytes:
    return hashlib.sha256(
        b"almighty-pagination-v1\0" + config.PAGINATION_CURSOR_SECRET.encode("utf-8")
    ).digest()


def encode_cursor(
    *, resource: str, parent: int | None, snapshot_max_id: int,
    position: list[str | int | float | None],
    metadata: dict[str, Any] | None = None,
) -> str:
    if resource not in _RESOURCES or snapshot_max_id < 0:
        raise ValueError("invalid cursor")
    payload = {
        "v": CURSOR_VERSION,
        "resource": resource,
        "parent": parent,
        "snapshot": snapshot_max_id,
        "position": position,
        "metadata": metadata or {},
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    token = f"{_b64encode(raw)}.{_b64encode(hmac.new(_key(), raw, hashlib.sha256).digest())}"
    if len(token.encode("ascii")) > MAX_CURSOR_BYTES:
        raise ValueError("invalid cursor")
    return token


def decode_cursor(
    token: str, *, resource: str, parent: int | None,
) -> dict[str, Any]:
    try:
        if not isinstance(token, str) or len(token.encode("utf-8")) > MAX_CURSOR_BYTES:
            raise ValueError("invalid cursor")
        encoded, signature = token.split(".")
        raw = _b64decode(encoded)
        supplied = _b64decode(signature)
        expected = hmac.new(_key(), raw, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied, expected):
            raise ValueError("invalid cursor")
        payload = json.loads(raw)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"v", "resource", "parent", "snapshot", "position", "metadata"}
            or payload["v"] != CURSOR_VERSION
            or payload["resource"] != resource
            or payload["parent"] != parent
            or not isinstance(payload["snapshot"], int)
            or isinstance(payload["snapshot"], bool)
            or payload["snapshot"] < 0
            or not isinstance(payload["position"], list)
            or len(payload["position"]) > 8
            or not isinstance(payload["metadata"], dict)
        ):
            raise ValueError("invalid cursor")
        for item in payload["position"]:
            if item is not None and (
                isinstance(item, bool)
                or not isinstance(item, (str, int, float))
                or (isinstance(item, str) and len(item) > 1024)
            ):
                raise ValueError("invalid cursor")
        return payload
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid cursor") from exc


def page_payload(
    items: list[dict[str, Any]], *, limit: int, next_cursor: str | None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "items": items,
        "pagination": {
            "limit": limit,
            "has_more": next_cursor is not None,
            "next_cursor": next_cursor,
        },
        **extra,
    }
