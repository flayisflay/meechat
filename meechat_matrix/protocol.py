"""NDJSON protocol helpers for the WeeChat adapter and Matrix worker."""

from __future__ import annotations

import json
from typing import Any, Mapping


class ProtocolError(ValueError):
    """Raised when an inbound or outbound protocol message is invalid."""


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
