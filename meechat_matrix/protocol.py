"""NDJSON protocol helpers for the WeeChat adapter and Matrix worker."""

from __future__ import annotations

import json
from typing import Any, Mapping


class ProtocolError(ValueError):
    """Raised when an inbound or outbound protocol message is invalid."""


DEFAULT_SUMMARY_KEYS = (
    "auth",
    "where",
    "room_id",
    "room",
    "event_id",
    "user_id",
    "first",
    "member_count",
    "encrypted",
)
REDACTED_SUMMARY_KEYS = {"room_id", "event_id", "user_id", "room"}


def dumps(message: Mapping[str, Any]) -> str:
    """Serialize a protocol message as one newline-terminated JSON object."""

    _validate_message(message)
    return json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"


def loads(line: str | bytes) -> dict[str, Any]:
    """Parse a single protocol line."""

    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")

    payload = line.strip()
    if not payload:
        raise ProtocolError("empty protocol line")

    try:
        message = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSON: {exc.msg}") from exc

    if not isinstance(message, dict):
        raise ProtocolError("protocol message must be a JSON object")

    _validate_message(message)
    return message


def _validate_message(message: Mapping[str, Any]) -> None:
    message_type = message.get("type")
    if not isinstance(message_type, str) or not message_type:
        raise ProtocolError("protocol message requires a non-empty string type")


def redacted_text(value: Any) -> str:
    text = str(value or "")
    if not text:
        return "<empty>"
    if len(text) <= 8:
        return "<set>"
    return text[:4] + "..." + text[-4:]


def message_summary(
    message: Mapping[str, Any],
    *,
    keys: tuple[str, ...] = DEFAULT_SUMMARY_KEYS,
) -> str:
    message_type = str(message.get("type") or "")
    parts = [f"type={message_type}"]
    for key in keys:
        if key in message:
            parts.append(f"{key}={_summary_value(key, message[key])}")
    if "receipts" in message:
        parts.append(f"receipts={len(message.get('receipts') or [])}")
    if "body" in message:
        parts.append(f"body_len={len(str(message.get('body') or ''))}")
    if "message" in message:
        parts.append(f"message={message.get('message', '')}")
    return " ".join(parts)


def _summary_value(key: str, value: Any) -> str:
    if key in REDACTED_SUMMARY_KEYS:
        return redacted_text(value)
    return str(value)
