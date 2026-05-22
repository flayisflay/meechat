"""WeeChat script entry point for MeeChat.

Install by symlinking this file into WeeChat's Python script directory, for
example:

    ln -s /path/to/meechat/meechat.py ~/.weechat/python/autoload/meechat.py
"""

from __future__ import annotations

import os
import re
import sys
import time
from typing import Any
from urllib.parse import quote

SCRIPT_NAME = "meechat"
SCRIPT_AUTHOR = "MeeChat contributors"
SCRIPT_VERSION = "0.1.0"
SCRIPT_LICENSE = "MIT"
SCRIPT_DESC = "Small Matrix adapter for WeeChat"

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from meechat_matrix.protocol import ProtocolError, dumps, loads

try:
    import weechat
except ImportError:  # Allows syntax/import checks outside WeeChat.
    weechat = None  # type: ignore


DEFAULT_OPTIONS = {
    "auth": "auto",
    "homeserver": "",
    "user_id": "",
    "username": "",
    "access_token": "",
    "password": "",
    "device_id": "",
    "store_path": "",
    "sync_timeout_ms": "30000",
    "backlog_limit": "100",
    "python": "python3",
    "autoconnect": "off",
    "debug": "off",
}

OPTION_DESCRIPTIONS = {
    "auth": "Authentication mode: auto, token, or password",
    "homeserver": "Matrix homeserver URL, for example https://matrix.org",
    "user_id": "Full Matrix user ID, for example @alice:matrix.org",
    "username": "Matrix login username, localpart, email, or full user ID",
    "access_token": "Access token, preferably ${sec.data.meechat_access_token}",
    "password": "Matrix password, preferably ${sec.data.meechat_password}",
    "device_id": "Matrix device ID associated with the access token",
    "store_path": "matrix-nio store directory; empty uses WeeChat config dir",
    "sync_timeout_ms": "Matrix sync long-poll timeout in milliseconds",
    "backlog_limit": "Recent Matrix room messages requested on initial sync",
    "python": "Python executable used to start the Matrix worker",
    "autoconnect": "Connect when the script loads: on/off",
    "debug": "Pass --debug to the worker for exception tracebacks: on/off",
}

WORKER_HOOK = ""
WORKER_STDOUT = ""
ROOM_BUFFERS: dict[str, str] = {}
BUFFER_ROOMS: dict[str, str] = {}
ROOM_NAMES: dict[str, str] = {}
ROOM_TOPICS: dict[str, str] = {}
ROOM_MESSAGES: dict[str, list[dict[str, Any]]] = {}
ROOM_INSPECT_INDEX: dict[str, int] = {}
ROOM_READ_RECEIPTS: dict[str, dict[str, dict[str, Any]]] = {}
ROOM_HIGHLIGHTED_INDEX: dict[str, int] = {}
INSPECT_ACTION_STATE: dict[str, dict[str, Any]] = {}
INSPECT_BAR_ITEM = ""
INSPECT_BAR = ""
TOPIC_BAR = ""
INSPECT_HISTORY_LIMIT = 500
INSPECT_ACTIONS = [
    {
        "key": "r",
        "name": "reply",
        "label": "[r]eply",
        "prompt": "reply text",
    },
    {
        "key": "t",
        "name": "thread",
        "label": "reply in [t]hread",
        "prompt": "thread reply text",
    },
    {
        "key": "e",
        "name": "edit",
        "label": "[e]dit",
        "prompt": "replacement text",
        "seed": "body",
    },
    {
        "key": "a",
        "name": "reaction",
        "label": "[a]dd reaction",
        "prompt": "reaction",
    },
    {
        "key": "v",
        "name": "remove",
        "label": "remo[v]e",
        "prompt": "redaction reason (optional)",
    },
    {
        "key": "p",
        "name": "permalink",
        "label": "[p]ermalink",
        "prompt": "",
    },
]


def _option(name: str, *, expand: bool = False) -> str:
    value = weechat.config_get_plugin(name)
    if expand and value:
        try:
            return weechat.string_eval_expression(value, {}, {}, {})
        except Exception:
            return value
    return value


def _option_bool(name: str) -> bool:
    return _option(name).lower() in {"1", "true", "yes", "on"}


def _log(message: str) -> None:
    weechat.prnt("", "meechat: " + message)


def _redacted_text(value: Any) -> str:
    text = str(value or "")
    if not text:
        return "<empty>"
    if len(text) <= 8:
        return "<set>"
    return text[:4] + "..." + text[-4:]


def _protocol_summary(message: dict[str, Any]) -> str:
    message_type = str(message.get("type") or "")
    parts = [f"type={message_type}"]
    for key in (
        "auth",
        "action",
        "where",
        "room_id",
        "room",
        "event_id",
        "user_id",
        "name",
        "first",
        "member_count",
        "encrypted",
    ):
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
    if key in {"room_id", "event_id", "user_id", "room"}:
        return _redacted_text(value)
    return str(value)


def _redacted_protocol_line(line: str) -> str:
    try:
        return _protocol_summary(loads(line))
    except Exception:
        return f"<unparseable line len={len(line)}>"


def _option_value_summary(name: str, value: Any) -> str:
    if name in {"access_token", "password"}:
        return "<redacted>"
    return _redacted_text(value)


def _command_summary(args: str) -> str:
    argv = args.strip().split()
    if not argv:
        return "<empty>"

    start = 1 if argv[0].lower() == "/meechat" else 0
    if len(argv) > start and argv[start].lower() == "set" and len(argv) > start + 1:
        name = argv[start + 1]
        if name in {"access_token", "password"}:
            prefix = "/meechat " if start else ""
            return f"{prefix}set {name} <redacted>"

    return args.strip()


def _init_options() -> None:
    _log("initializing plugin options")
    for name, default in DEFAULT_OPTIONS.items():
        if not weechat.config_is_set_plugin(name):
            _log(f"creating option plugins.var.python.meechat.{name}={default!r}")
            weechat.config_set_plugin(name, default)
        weechat.config_set_desc_plugin(name, OPTION_DESCRIPTIONS[name])
    _log("plugin options initialized")


def _default_store_path() -> str:
    config_dir = (
        weechat.info_get("weechat_config_dir", "")
        or weechat.info_get("weechat_dir", "")
        or os.path.expanduser("~/.weechat")
    )
    return os.path.join(config_dir, "meechat_matrix_store")


def _worker_path() -> str:
    return os.path.join(SCRIPT_DIR, "meechat_matrix", "worker.py")


def _config_message() -> dict[str, Any] | None:
    _log("building Matrix worker configuration from WeeChat options")
    auth = (_option("auth", expand=True) or "auto").lower()
    user_id = _option("user_id", expand=True)
    username = _option("username", expand=True) or user_id
    message = {
        "type": "configure",
        "auth": auth,
        "homeserver": _option("homeserver", expand=True),
        "user_id": user_id,
        "username": username,
        "access_token": _option("access_token", expand=True),
        "password": _option("password", expand=True),
        "device_id": _option("device_id", expand=True),
        "store_path": _option("store_path", expand=True) or _default_store_path(),
        "sync_timeout_ms": _option("sync_timeout_ms", expand=True),
        "backlog_limit": _option("backlog_limit", expand=True),
    }

    missing = []
    if auth not in {"auto", "token", "password"}:
        missing.append(
            "plugins.var.python.meechat.auth must be auto, token, or password"
        )

    if not str(message["homeserver"]).strip():
        missing.append("plugins.var.python.meechat.homeserver")

    has_access_token = bool(str(message["access_token"]).strip())
    has_password = bool(str(message["password"]).strip())
    use_password = auth == "password" or (auth == "auto" and has_password)
    use_token = auth == "token" or (auth == "auto" and not use_password)
    resolved_auth = "password" if use_password else "token"
    _log(
        "config summary: "
        f"auth={auth} resolved_auth={resolved_auth} "
        f"homeserver={_redacted_text(message['homeserver'])} "
        f"user_id={_redacted_text(message['user_id'])} "
        f"username={_redacted_text(message['username'])} "
        f"device_id={_redacted_text(message['device_id'])} "
        f"access_token_present={has_access_token} password_present={has_password} "
        f"store_path={_redacted_text(message['store_path'])} "
        f"sync_timeout_ms={message['sync_timeout_ms']} "
        f"backlog_limit={message['backlog_limit']}"
    )

    if use_password:
        if not has_password:
            missing.append("plugins.var.python.meechat.password")
        if not str(message["username"]).strip():
            missing.append(
                "plugins.var.python.meechat.username "
                "(or plugins.var.python.meechat.user_id)"
            )
    elif use_token:
        if not has_access_token:
            missing.append("plugins.var.python.meechat.access_token")
        for key in ("user_id", "device_id"):
            if not str(message[key]).strip():
                missing.append("plugins.var.python.meechat." + key)

    if missing:
        _log("configuration blocked; missing " + ", ".join(missing))
        weechat.prnt(
            "",
            "meechat: missing required option(s): " + ", ".join(missing),
        )
        return None
    _log("configuration looks complete; worker configure message ready")
    return message


def _start_worker() -> bool:
    global WORKER_HOOK

    if WORKER_HOOK:
        _log(f"worker already started; hook={WORKER_HOOK}")
        return True

    python = _option("python", expand=True) or "python3"
    options = {"arg1": _worker_path(), "stdin": "1", "buffer_flush": "1"}
    if _option_bool("debug"):
        options["arg2"] = "--debug"

    _log(
        "starting Matrix worker: "
        f"python={python} worker_path={_worker_path()} "
        f"debug={_option_bool('debug')} buffer_flush=1 stdin=1"
    )
    WORKER_HOOK = weechat.hook_process_hashtable(
        python,
        options,
        0,
        "worker_process_cb",
        "",
    )
    if not WORKER_HOOK:
        _log("failed to start Matrix worker; hook_process_hashtable returned empty")
        return False
    _log(f"Matrix worker started; hook={WORKER_HOOK}")
    return True


def _send_to_worker(message: dict[str, Any]) -> bool:
    _log("send to worker requested: " + _protocol_summary(message))
    if not WORKER_HOOK and not _start_worker():
        _log("send to worker aborted because worker could not be started")
        return False

    try:
        payload = dumps(message)
    except ProtocolError as exc:
        weechat.prnt("", f"meechat: internal protocol error: {exc}")
        return False

    weechat.hook_set(WORKER_HOOK, "stdin", payload)
    _log("sent worker message: " + _protocol_summary(message))
    return True


def _connect() -> None:
    _log("/meechat connect invoked")
    message = _config_message()
    if message is None:
        _log("connect aborted before worker startup due to invalid config")
        return

    if not _start_worker():
        _log("connect aborted because worker startup failed")
        return

    _log("sending configure then connect commands to worker")
    _send_to_worker(message)
    _send_to_worker({"type": "connect"})


def _disconnect() -> None:
    _log("/meechat disconnect invoked")
    if WORKER_HOOK:
        _send_to_worker({"type": "shutdown"})
    else:
        _log("disconnect requested but no worker hook is active")


def _consume_worker_output(output: str | bytes) -> None:
    global WORKER_STDOUT

    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")

    if output:
        _log(f"worker stdout chunk received: bytes={len(output)}")
    WORKER_STDOUT += output
    while "\n" in WORKER_STDOUT:
        line, WORKER_STDOUT = WORKER_STDOUT.split("\n", 1)
        if not line.strip():
            continue
        _log("worker stdout line: " + _redacted_protocol_line(line))
        try:
            _handle_worker_message(loads(line))
        except ProtocolError as exc:
            weechat.prnt("", f"meechat: invalid worker output: {exc}")


def _handle_worker_message(message: dict[str, Any]) -> None:
    message_type = message["type"]
    _log("received worker message: " + _protocol_summary(message))

    if message_type == "ready":
        _log(f"worker ready; version={message.get('version', '')}")
        return

    if message_type == "configured":
        _log("worker accepted configuration")
        return

    if message_type == "connected":
        weechat.prnt("", f"meechat: connected as {message.get('user_id', '')}")
        _log("connection established; requesting/expecting room sync")
        return

    if message_type == "disconnected":
        weechat.prnt("", "meechat: disconnected")
        _log("worker reported disconnected")
        return

    if message_type == "sync":
        _log(
            "sync ok: "
            f"first={message.get('first', '')} "
            f"next_batch_present={bool(message.get('next_batch', ''))}"
        )
        return

    if message_type == "room":
        _record_room(
            str(message.get("room_id") or ""),
            str(message.get("name") or ""),
            topic=str(message.get("topic") or ""),
            encrypted=bool(message.get("encrypted", False)),
        )
        return

    if message_type == "message":
        _print_matrix_message(message)
        return

    if message_type == "read_receipts":
        _record_read_receipts(message)
        return

    if message_type == "send_ok":
        _log(f"message send confirmed; event_id={message.get('event_id', '')}")
        return

    if message_type == "action_ok":
        _log(
            "message action confirmed: "
            f"action={message.get('action', '')} "
            f"room_id={_redacted_text(message.get('room_id', ''))} "
            f"event_id={_redacted_text(message.get('event_id', ''))}"
        )
        return

    if message_type == "joined":
        room_id = str(message.get("room_id") or "")
        _record_room(room_id, room_id)
        weechat.prnt("", f"meechat: joined {message.get('room', room_id)}")
        return

    if message_type == "left":
        room_id = str(message.get("room_id") or "")
        weechat.prnt("", f"meechat: left {room_id}")
        return

    if message_type == "error":
        target = _buffer_for_room(str(message.get("room_id") or ""))
        where = message.get("where", "worker")
        weechat.prnt(target, f"meechat: {where}: {message.get('message', '')}")
        _log(
            "worker error: "
            f"where={where} room_id={message.get('room_id', '')} "
            f"message={message.get('message', '')}"
        )
        return

    _log(f"ignored worker message: {_protocol_summary(message)}")


def _record_room(
    room_id: str,
    name: str,
    *,
    topic: str | None = None,
    encrypted: bool = False,
) -> str:
    if not room_id:
        _log("ignoring room record with empty room_id")
        return ""

    ROOM_NAMES[room_id] = name or room_id
    if topic is not None:
        ROOM_TOPICS[room_id] = topic
    buffer = _buffer_for_room(room_id)
    if not buffer:
        _log(
            "creating room buffer: "
            f"room_id={_redacted_text(room_id)} name={name or room_id!r} "
            f"topic_len={len(topic or '')} encrypted={encrypted}"
        )
        buffer = _create_room_buffer(room_id, name or room_id, topic=topic)
    else:
        _log(
            "updating room buffer: "
            f"room_id={_redacted_text(room_id)} buffer={buffer} "
            f"name={name or room_id!r} topic_len={len(topic or '')} "
            f"encrypted={encrypted}"
        )
        _update_room_buffer(
            buffer,
            room_id,
            name or room_id,
            topic=topic,
            encrypted=encrypted,
        )
    return buffer


def _buffer_for_room(room_id: str) -> str:
    return ROOM_BUFFERS.get(room_id, "")


def _create_room_buffer(
    room_id: str,
    name: str,
    *,
    topic: str | None = None,
) -> str:
    buffer_name = "meechat." + _safe_name(room_id)
    buffer = weechat.buffer_new(
        buffer_name,
        "room_input_cb",
        room_id,
        "room_close_cb",
        room_id,
    )
    if not buffer:
        _log(f"failed to create buffer for room_id={_redacted_text(room_id)}")
        return ""

    ROOM_BUFFERS[room_id] = buffer
    BUFFER_ROOMS[buffer] = room_id
    _log(
        "room buffer created: "
        f"buffer={buffer} room_id={_redacted_text(room_id)} "
        f"buffer_name={buffer_name}"
    )
    _update_room_buffer(buffer, room_id, name, topic=topic)
    return buffer


def _update_room_buffer(
    buffer: str,
    room_id: str,
    name: str,
    *,
    topic: str | None = None,
    encrypted: bool = False,
) -> None:
    short_name = name or room_id
    if encrypted:
        short_name += " [encrypted]"

    weechat.buffer_set(buffer, "short_name", short_name)
    if topic is not None:
        ROOM_TOPICS[room_id] = topic
    weechat.buffer_set(buffer, "title", ROOM_TOPICS.get(room_id, ""))
    weechat.buffer_set(buffer, "localvar_set_type", "matrix")
    weechat.buffer_set(buffer, "localvar_set_room_id", room_id)
    weechat.buffer_set(buffer, "key_bind_meta-shift-up", "/meechat inspect-prev")
    weechat.buffer_set(buffer, "key_bind_meta-shift-down", "/meechat inspect-next")
    weechat.buffer_set(buffer, "key_bind_up", "/meechat inspect-up")
    weechat.buffer_set(buffer, "key_bind_down", "/meechat inspect-down")
    weechat.buffer_set(buffer, "key_bind_ctrl-M", "/meechat inspect-enter")
    weechat.buffer_set(buffer, "key_bind_ctrl-G", "/meechat inspect-cancel")
    _log(
        "room buffer updated: "
        f"buffer={buffer} short_name={short_name!r} "
        f"title_len={len(ROOM_TOPICS.get(room_id, ''))}"
    )


def _print_matrix_message(message: dict[str, Any]) -> None:
    room_id = str(message.get("room_id") or "")
    room_name = str(message.get("room_name") or ROOM_NAMES.get(room_id, room_id))
    room_topic = (
        str(message.get("room_topic") or "") if "room_topic" in message else None
    )
    buffer = _record_room(room_id, room_name, topic=room_topic)
    if not buffer:
        _log("dropping Matrix message because no room buffer could be created")
        return

    sender = str(message.get("sender") or "")
    sender_display_name = str(message.get("sender_display_name") or sender)
    body = str(message.get("body") or "")
    timestamp_ms = int(message.get("server_timestamp") or 0)
    timestamp = timestamp_ms // 1000 if timestamp_ms else 0
    event_id = str(message.get("event_id") or "")
    tags = "notify_message"
    if sender:
        tags += ",nick_" + sender
    prefix = _colored_nick(sender_display_name)
    _log(
        "printing Matrix message: "
        f"room_id={_redacted_text(room_id)} buffer={buffer} "
        f"event_id={_redacted_text(event_id)} sender={_redacted_text(sender)} "
        f"display_name={sender_display_name!r} body_len={len(body)} "
        f"timestamp={timestamp}"
    )
    weechat.prnt_date_tags(
        buffer,
        timestamp,
        tags,
        f"{prefix}\t{body}",
    )
    _record_room_message(
        room_id,
        {
            "event_id": event_id,
            "sender": sender,
            "sender_display_name": sender_display_name,
            "body": body,
            "timestamp": timestamp,
            "line_data": _last_printed_line_data(buffer),
            "line_prefix": prefix,
            "line_message": body,
        },
    )


def _record_room_message(room_id: str, message: dict[str, Any]) -> None:
    messages = ROOM_MESSAGES.setdefault(room_id, [])
    selected = ROOM_INSPECT_INDEX.get(room_id)
    was_at_end = selected is None or selected >= len(messages) - 1
    highlight_active = room_id in ROOM_HIGHLIGHTED_INDEX

    messages.append(message)
    _log(
        "cached room message: "
        f"room_id={_redacted_text(room_id)} cached_count={len(messages)} "
        f"event_id={_redacted_text(message.get('event_id', ''))}"
    )
    if len(messages) > INSPECT_HISTORY_LIMIT:
        overflow = len(messages) - INSPECT_HISTORY_LIMIT
        if highlight_active:
            _restore_inspect_highlight(room_id)
        del messages[:overflow]
        if selected is not None:
            ROOM_INSPECT_INDEX[room_id] = max(0, selected - overflow)

    if was_at_end or room_id not in ROOM_INSPECT_INDEX:
        ROOM_INSPECT_INDEX[room_id] = len(messages) - 1

    if highlight_active:
        _highlight_inspect_message(room_id)
    _update_inspect_bar()


def _record_read_receipts(message: dict[str, Any]) -> None:
    room_id = str(message.get("room_id") or "")
    if not room_id:
        _log("ignoring read receipts with empty room_id")
        return

    room_receipts = ROOM_READ_RECEIPTS.setdefault(room_id, {})
    receipt_count = 0
    for receipt in message.get("receipts") or []:
        event_id = str(receipt.get("event_id") or "")
        user_id = str(receipt.get("user_id") or "")
        if not event_id or not user_id:
            continue
        room_receipts[user_id] = {
            "event_id": event_id,
            "user_id": user_id,
            "display_name": str(receipt.get("display_name") or user_id),
            "timestamp": int(receipt.get("timestamp") or 0),
        }
        receipt_count += 1

    _log(
        "recorded read receipts: "
        f"room_id={_redacted_text(room_id)} count={receipt_count} "
        f"room_receipt_users={len(room_receipts)}"
    )
    _update_inspect_bar()


def _show_inspect(buffer: str) -> None:
    room_id = _inspect_room_from_context(buffer)
    _log(
        "inspect requested: "
        f"buffer={buffer} room_id={_redacted_text(room_id)} "
        f"messages={len(ROOM_MESSAGES.get(room_id, [])) if room_id else 0}"
    )
    if not room_id:
        _log("inspect request rejected because buffer is not a Matrix room")
        weechat.prnt(buffer, "meechat: inspect is only available in a Matrix room")
        return

    _ensure_inspect_bar()
    _select_inspect_room(room_id)
    _clear_inspect_selection(buffer, room_id)
    _highlight_inspect_message(room_id)
    _update_inspect_bar()


def _inspect_step(buffer: str, direction: int) -> None:
    room_id = _inspect_room_from_context(buffer)
    _log(
        "inspect navigation requested: "
        f"buffer={buffer} direction={direction} "
        f"room_id={_redacted_text(room_id)}"
    )
    if not room_id:
        _log("inspect navigation rejected because buffer is not a Matrix room")
        weechat.prnt(buffer, "meechat: inspect is only available in a Matrix room")
        return

    _ensure_inspect_bar()
    _select_inspect_room(room_id)
    _clear_inspect_selection(buffer, room_id)
    messages = ROOM_MESSAGES.get(room_id, [])
    if messages:
        index = ROOM_INSPECT_INDEX.get(room_id, len(messages) - 1)
        ROOM_INSPECT_INDEX[room_id] = max(
            0,
            min(len(messages) - 1, index + direction),
        )
        _log(
            "inspect selection changed: "
            f"room_id={_redacted_text(room_id)} previous={index} "
            f"current={ROOM_INSPECT_INDEX[room_id]} count={len(messages)}"
        )
    else:
        _log(
            "inspect navigation found no cached messages: "
            f"room_id={_redacted_text(room_id)}"
        )
    _highlight_inspect_message(room_id)
    _update_inspect_bar()


def _inspect_key_step(buffer: str, direction: int) -> None:
    room_id = _inspect_room_from_context(buffer)
    _log(
        "inspect key navigation requested: "
        f"buffer={buffer} direction={direction} room_id={_redacted_text(room_id)}"
    )
    if not room_id:
        _log("inspect key navigation ignored; buffer is not a Matrix room")
        return

    state = INSPECT_ACTION_STATE.get(room_id, {})
    if state.get("mode") == "menu":
        _log(
            "inspect key navigation moving action menu: "
            f"room_id={_redacted_text(room_id)} direction={direction}"
        )
        _move_inspect_action(room_id, direction)
        _update_inspect_bar()
        return

    if state.get("mode") == "prompt" or _buffer_input(buffer):
        history_command = (
            "/input history_previous" if direction < 0 else "/input history_next"
        )
        _log(
            "inspect key navigation delegated to input history: "
            f"buffer={buffer} command={history_command}"
        )
        weechat.command(buffer, history_command)
        return

    _inspect_step(buffer, direction)


def _inspect_enter(buffer: str) -> None:
    room_id = _inspect_room_from_context(buffer)
    _log(
        "inspect enter requested: "
        f"buffer={buffer} room_id={_redacted_text(room_id)}"
    )
    if not room_id:
        _log("inspect enter ignored; buffer is not a Matrix room")
        return

    state = INSPECT_ACTION_STATE.get(room_id, {})
    mode = str(state.get("mode") or "nav")
    input_text = _buffer_input(buffer)
    _log(
        "inspect enter state: "
        f"room_id={_redacted_text(room_id)} mode={mode} input_len={len(input_text)}"
    )

    if mode == "prompt":
        _complete_inspect_action(buffer, room_id, input_text)
        return

    if mode == "menu":
        _start_selected_inspect_action(buffer, room_id)
        return

    if input_text:
        _set_buffer_input(buffer, "")
        _log("inspect enter sending typed input as room message")
        room_input_cb("", buffer, input_text)
        return

    _open_inspect_action_menu(buffer, room_id)


def _inspect_cancel(buffer: str) -> None:
    room_id = _inspect_room_from_context(buffer)
    _log(
        "inspect cancel requested: "
        f"buffer={buffer} room_id={_redacted_text(room_id)}"
    )
    if not room_id:
        return

    if room_id in INSPECT_ACTION_STATE:
        _log(f"inspect action mode cancelled: room_id={_redacted_text(room_id)}")
        _exit_inspect_action_mode(buffer, room_id)
        _set_buffer_input(buffer, "")
    else:
        _log("inspect cancel ignored; no action mode is active")


def _choose_inspect_action(buffer: str, key: str) -> None:
    room_id = _inspect_room_from_context(buffer)
    _log(
        "inspect action key requested: "
        f"buffer={buffer} key={key!r} room_id={_redacted_text(room_id)}"
    )
    if not room_id:
        return

    state = INSPECT_ACTION_STATE.get(room_id, {})
    if state.get("mode") != "menu":
        _log("inspect action key ignored; menu is not active")
        return

    for index, action in enumerate(INSPECT_ACTIONS):
        if action["key"] == key.lower():
            state["selected"] = index
            _log(
                "inspect action selected by key: "
                f"room_id={_redacted_text(room_id)} action={action['name']} "
                f"index={index}"
            )
            _update_inspect_bar()
            return
    _log(f"inspect action key did not match an action: key={key!r}")


def _open_inspect_action_menu(buffer: str, room_id: str) -> None:
    selected = _selected_inspect_message(room_id)
    if selected is None:
        _log(
            "inspect action menu blocked; no selected message: "
            f"room_id={_redacted_text(room_id)}"
        )
        weechat.prnt(buffer, "meechat: no inspected message is selected")
        return

    index, message = selected
    _log(
        "inspect action menu opened: "
        f"room_id={_redacted_text(room_id)} index={index} "
        f"event_id={_redacted_text(message.get('event_id', ''))}"
    )
    _set_inspect_action_keys(buffer, True)
    INSPECT_ACTION_STATE[room_id] = {"mode": "menu", "selected": 0}
    _highlight_inspect_message(room_id)
    _update_inspect_bar()


def _move_inspect_action(room_id: str, direction: int) -> None:
    state = INSPECT_ACTION_STATE.setdefault(room_id, {"mode": "menu", "selected": 0})
    selected = int(state.get("selected") or 0)
    state["selected"] = (selected + direction) % len(INSPECT_ACTIONS)
    _log(
        "inspect action menu moved: "
        f"room_id={_redacted_text(room_id)} previous={selected} "
        f"current={state['selected']} direction={direction}"
    )


def _start_selected_inspect_action(buffer: str, room_id: str) -> None:
    selected = int(INSPECT_ACTION_STATE.get(room_id, {}).get("selected") or 0)
    action = INSPECT_ACTIONS[selected]
    message_data = _selected_inspect_message(room_id)
    if message_data is None:
        _log(
            "inspect action start blocked; selected message missing: "
            f"room_id={_redacted_text(room_id)} action={action['name']}"
        )
        _exit_inspect_action_mode(buffer, room_id)
        return

    _index, message = message_data
    _log(
        "inspect action starting: "
        f"room_id={_redacted_text(room_id)} action={action['name']} "
        f"event_id={_redacted_text(message.get('event_id', ''))}"
    )
    if action["name"] == "permalink":
        permalink = _matrix_permalink(room_id, str(message.get("event_id") or ""))
        weechat.prnt(buffer, "meechat: permalink: " + permalink)
        _log(
            "inspect permalink emitted: "
            f"room_id={_redacted_text(room_id)} "
            f"event_id={_redacted_text(message.get('event_id', ''))}"
        )
        _exit_inspect_action_mode(buffer, room_id)
        return

    _set_inspect_action_keys(buffer, False)
    seed = str(message.get(str(action.get("seed") or "")) or "")
    _set_buffer_input(buffer, seed)
    INSPECT_ACTION_STATE[room_id] = {
        "mode": "prompt",
        "action": action["name"],
        "prompt": action["prompt"],
    }
    _log(
        "inspect action prompt opened: "
        f"room_id={_redacted_text(room_id)} action={action['name']} "
        f"seed_len={len(seed)}"
    )
    _update_inspect_bar()


def _complete_inspect_action(buffer: str, room_id: str, input_text: str) -> None:
    state = INSPECT_ACTION_STATE.get(room_id, {})
    action = str(state.get("action") or "")
    message_data = _selected_inspect_message(room_id)
    if message_data is None or not action:
        _log(
            "inspect action completion blocked: "
            f"room_id={_redacted_text(room_id)} action={action!r} "
            f"message_present={message_data is not None}"
        )
        _exit_inspect_action_mode(buffer, room_id)
        return

    _index, message = message_data
    event_id = str(message.get("event_id") or "")
    if not event_id:
        _log("inspect action completion blocked; selected message has no event id")
        weechat.prnt(buffer, "meechat: selected message has no event id")
        return

    if action != "remove" and not input_text.strip():
        _log(
            "inspect action completion blocked; input required: "
            f"action={action}"
        )
        weechat.prnt(buffer, "meechat: input is required for " + action)
        return

    _log(
        "inspect action completion sending worker request: "
        f"room_id={_redacted_text(room_id)} action={action} "
        f"event_id={_redacted_text(event_id)} input_len={len(input_text)}"
    )
    payload = {
        "type": "message_action",
        "action": action,
        "room_id": room_id,
        "event_id": event_id,
        "body": input_text,
    }
    if action == "remove":
        payload["reason"] = input_text

    _send_to_worker(payload)
    _set_buffer_input(buffer, "")
    _exit_inspect_action_mode(buffer, room_id)


def _exit_inspect_action_mode(buffer: str, room_id: str) -> None:
    INSPECT_ACTION_STATE.pop(room_id, None)
    _log(f"inspect action mode exited: room_id={_redacted_text(room_id)}")
    _set_inspect_action_keys(buffer, False)
    _update_inspect_bar()


def _clear_inspect_selection(buffer: str, room_id: str) -> None:
    if room_id in INSPECT_ACTION_STATE:
        _log(f"clearing inspect selection: room_id={_redacted_text(room_id)}")
        INSPECT_ACTION_STATE.pop(room_id, None)
        _set_inspect_action_keys(buffer, False)
    _restore_inspect_highlight(room_id)


def _clear_all_inspect_action_modes() -> None:
    _log(f"clearing inspect action modes: count={len(INSPECT_ACTION_STATE)}")
    for buffer in list(ROOM_BUFFERS.values()):
        _set_inspect_action_keys(buffer, False)
    INSPECT_ACTION_STATE.clear()


def _set_inspect_action_keys(buffer: str, enabled: bool) -> None:
    _log(f"setting inspect action key binds: buffer={buffer} enabled={enabled}")
    for action in INSPECT_ACTIONS:
        command = f"/meechat inspect-action {action['key']}" if enabled else ""
        weechat.buffer_set(buffer, "key_bind_" + action["key"], command)


def _selected_inspect_message(room_id: str) -> tuple[int, dict[str, Any]] | None:
    messages = ROOM_MESSAGES.get(room_id, [])
    if not messages:
        _log(
            "selected inspect message unavailable; no cached messages: "
            f"room_id={_redacted_text(room_id)}"
        )
        return None

    index = max(
        0,
        min(len(messages) - 1, ROOM_INSPECT_INDEX.get(room_id, len(messages) - 1)),
    )
    ROOM_INSPECT_INDEX[room_id] = index
    _log(
        "selected inspect message resolved: "
        f"room_id={_redacted_text(room_id)} index={index} "
        f"event_id={_redacted_text(messages[index].get('event_id', ''))}"
    )
    return index, messages[index]


def _buffer_input(buffer: str) -> str:
    if hasattr(weechat, "buffer_get_string"):
        value = str(weechat.buffer_get_string(buffer, "input") or "")
        _log(f"buffer input read: buffer={buffer} input_len={len(value)}")
        return value
    _log("buffer input read skipped; buffer_get_string unavailable")
    return ""


def _set_buffer_input(buffer: str, value: str) -> None:
    _log(f"buffer input set: buffer={buffer} value_len={len(value)}")
    weechat.buffer_set(buffer, "input", value)


def _matrix_permalink(room_id: str, event_id: str) -> str:
    return "https://matrix.to/#/" + quote(room_id, safe="") + "/" + quote(
        event_id,
        safe="",
    )


def _inspect_room_from_context(buffer: str) -> str:
    return BUFFER_ROOMS.get(buffer, "")


def _select_inspect_room(room_id: str) -> None:
    messages = ROOM_MESSAGES.get(room_id, [])
    if messages and room_id not in ROOM_INSPECT_INDEX:
        ROOM_INSPECT_INDEX[room_id] = len(messages) - 1
        _log(
            "inspect room selected at latest message: "
            f"room_id={_redacted_text(room_id)} index={len(messages) - 1}"
        )


def _highlight_inspect_message(room_id: str) -> None:
    _restore_inspect_highlight(room_id)

    messages = ROOM_MESSAGES.get(room_id, [])
    if not messages:
        _log(
            "inspect highlight skipped; no cached messages: "
            f"room_id={_redacted_text(room_id)}"
        )
        return

    index = max(
        0,
        min(len(messages) - 1, ROOM_INSPECT_INDEX.get(room_id, len(messages) - 1)),
    )
    _log(
        "inspect highlight requested: "
        f"room_id={_redacted_text(room_id)} index={index} "
        f"event_id={_redacted_text(messages[index].get('event_id', ''))}"
    )
    if _update_message_line(messages[index], selected=True):
        ROOM_HIGHLIGHTED_INDEX[room_id] = index
        _log(
            "inspect highlight applied: "
            f"room_id={_redacted_text(room_id)} index={index}"
        )
    else:
        _log(
            "inspect highlight could not update line: "
            f"room_id={_redacted_text(room_id)} index={index}"
        )


def _restore_inspect_highlight(room_id: str) -> None:
    index = ROOM_HIGHLIGHTED_INDEX.pop(room_id, None)
    messages = ROOM_MESSAGES.get(room_id, [])
    if index is None or index >= len(messages):
        _log(
            "inspect highlight restore skipped: "
            f"room_id={_redacted_text(room_id)} index={index} "
            f"message_count={len(messages)}"
        )
        return

    restored = _update_message_line(messages[index], selected=False)
    _log(
        "inspect highlight restored: "
        f"room_id={_redacted_text(room_id)} index={index} restored={restored}"
    )


def _restore_all_inspect_highlights() -> None:
    _log(f"restoring all inspect highlights: count={len(ROOM_HIGHLIGHTED_INDEX)}")
    for room_id in list(ROOM_HIGHLIGHTED_INDEX):
        _restore_inspect_highlight(room_id)


def _update_message_line(message: dict[str, Any], *, selected: bool) -> bool:
    line_data = str(message.get("line_data") or "")
    if not line_data or not hasattr(weechat, "hdata_update"):
        _log(
            "line update unavailable: "
            f"selected={selected} line_data_present={bool(line_data)} "
            f"hdata_update_present={hasattr(weechat, 'hdata_update')}"
        )
        return False

    prefix = str(message.get("line_prefix") or "")
    body = str(message.get("line_message") or message.get("body") or "")
    if selected:
        reset = weechat.color("reset")
        prefix = f"{weechat.color('reverse')}>> {prefix}{reset}"
        body = f"{weechat.color('reverse')}{body}{reset}"

    try:
        updated = weechat.hdata_update(
            weechat.hdata_get("line_data"),
            line_data,
            {"prefix": prefix, "message": body},
        )
    except Exception as exc:
        _log(f"line update failed: selected={selected} error={exc}")
        return False
    _log(
        "line update completed: "
        f"selected={selected} line_data={line_data} updated_fields={updated}"
    )
    return bool(updated)


def _last_printed_line_data(buffer: str) -> str:
    for name in ("own_lines", "lines"):
        line_data = _last_printed_line_data_from_lines(buffer, name)
        if line_data:
            _log(f"last printed line data found: buffer={buffer} source={name}")
            return line_data
    _log(f"last printed line data not found: buffer={buffer}")
    return ""


def _last_printed_line_data_from_lines(buffer: str, lines_name: str) -> str:
    if not hasattr(weechat, "hdata_pointer"):
        _log("hdata_pointer unavailable while looking up last printed line")
        return ""

    try:
        lines = weechat.hdata_pointer(weechat.hdata_get("buffer"), buffer, lines_name)
        if not lines:
            _log(f"no WeeChat lines pointer: buffer={buffer} source={lines_name}")
            return ""
        line = weechat.hdata_pointer(weechat.hdata_get("lines"), lines, "last_line")
        if not line:
            _log(f"no last_line pointer: buffer={buffer} source={lines_name}")
            return ""
        return str(weechat.hdata_pointer(weechat.hdata_get("line"), line, "data"))
    except Exception as exc:
        _log(
            "failed looking up last printed line data: "
            f"buffer={buffer} source={lines_name} error={exc}"
        )
        return ""


def _ensure_inspect_bar() -> None:
    global INSPECT_BAR, INSPECT_BAR_ITEM

    if not INSPECT_BAR_ITEM:
        _log("creating inspect bar item")
        INSPECT_BAR_ITEM = weechat.bar_item_new(
            "(extra)meechat_inspect",
            "inspect_bar_item_cb",
            "",
        )
        _log(f"inspect bar item result: item={INSPECT_BAR_ITEM}")
    else:
        _log(f"inspect bar item already exists: item={INSPECT_BAR_ITEM}")

    if not INSPECT_BAR:
        _log("creating inspect bar")
        INSPECT_BAR = weechat.bar_new(
            "meechat_inspect",
            "off",
            "100",
            "window",
            "${buffer.local_variables.type} == matrix",
            "top",
            "vertical",
            "vertical",
            "0",
            "9",
            "default",
            "default",
            "default",
            "default",
            "on",
            "meechat_inspect",
        )
        if not INSPECT_BAR:
            weechat.prnt("", "meechat: failed to create inspect bar")
            _log("inspect bar creation failed")
        else:
            _log(f"inspect bar created: bar={INSPECT_BAR}")
    else:
        _log(f"inspect bar already exists: bar={INSPECT_BAR}")

    _ensure_topic_bar()


def _ensure_topic_bar() -> None:
    global TOPIC_BAR

    if TOPIC_BAR:
        _log(f"topic bar already exists: bar={TOPIC_BAR}")
        return

    _log("creating topic bar")
    TOPIC_BAR = weechat.bar_new(
        "meechat_topic",
        "off",
        "99",
        "window",
        "${buffer.local_variables.type} == matrix",
        "top",
        "horizontal",
        "vertical",
        "0",
        "1",
        "default",
        "default",
        "darkblue",
        "default",
        "on",
        "buffer_title",
    )
    if not TOPIC_BAR:
        weechat.prnt("", "meechat: failed to create topic bar")
        _log("topic bar creation failed")
    else:
        _log(f"topic bar created: bar={TOPIC_BAR}")


def _update_inspect_bar() -> None:
    if INSPECT_BAR_ITEM:
        weechat.bar_item_update("meechat_inspect")
        _log(f"inspect bar item update requested: item={INSPECT_BAR_ITEM}")
    else:
        _log("inspect bar item update skipped; item does not exist")


def inspect_bar_item_cb(
    data: str,
    item: str,
    window: str,
    buffer: str,
    extra_info: dict[str, str],
) -> str:
    room_id = BUFFER_ROOMS.get(buffer, "")
    _log(
        "inspect bar item render: "
        f"buffer={buffer} room_id={_redacted_text(room_id)} "
        f"item={item} window={window}"
    )
    if not room_id:
        return ""
    return _inspect_text(room_id)


def _inspect_text(room_id: str) -> str:
    messages = ROOM_MESSAGES.get(room_id, [])
    if not messages:
        _log(f"inspect text requested with no messages: room_id={_redacted_text(room_id)}")
        return "\n".join(
            [
                "message: none",
                "read receipts:",
            ]
        )

    index = max(
        0,
        min(len(messages) - 1, ROOM_INSPECT_INDEX.get(room_id, len(messages) - 1)),
    )
    ROOM_INSPECT_INDEX[room_id] = index
    message = messages[index]
    sender = str(message.get("sender") or "")
    display_name = str(message.get("sender_display_name") or sender)
    event_id = str(message.get("event_id") or "")
    timestamp = int(message.get("timestamp") or 0)
    body = _single_line_body(str(message.get("body") or ""))
    receipts = _read_receipts_for_message(room_id, str(event_id), index)
    _log(
        "inspect text rendered: "
        f"room_id={_redacted_text(room_id)} index={index} "
        f"messages={len(messages)} receipts={len(receipts)} "
        f"event_id={_redacted_text(event_id)} body_len={len(body)}"
    )

    lines = [
        f"message: {index + 1}/{len(messages)}",
        f"time: {_format_inspect_time(timestamp)} | sender: {display_name}",
    ]
    if sender and sender != display_name:
        lines.append(f"user: {sender}")
    if event_id:
        lines.append(f"event: {event_id}")
    lines.append(f"body: {body}")
    lines.append("read receipts: " + _format_read_receipts(receipts))
    lines.extend(_inspect_action_lines(room_id))
    return "\n".join(lines)


def _inspect_action_lines(room_id: str) -> list[str]:
    state = INSPECT_ACTION_STATE.get(room_id, {})
    mode = state.get("mode")
    if mode == "menu":
        return [
            "selected: message options",
            "actions: " + _format_inspect_action_menu(room_id),
            "enter: choose | letters/arrows: select | Ctrl-G: cancel",
        ]
    if mode == "prompt":
        action = str(state.get("action") or "")
        prompt = str(state.get("prompt") or action)
        return [
            f"action: {action} | input: {prompt}",
            "enter: finish | Ctrl-G: cancel",
        ]
    return ["enter: select message"]


def _format_inspect_action_menu(room_id: str) -> str:
    state = INSPECT_ACTION_STATE.get(room_id, {})
    selected = int(state.get("selected") or 0)
    reset = weechat.color("reset")
    parts = []
    for index, action in enumerate(INSPECT_ACTIONS):
        label = str(action["label"])
        if index == selected:
            label = f"{weechat.color('reverse')}{label}{reset}"
        parts.append(label)
    return "  ".join(parts)


def _read_receipts_for_message(
    room_id: str,
    event_id: str,
    message_index: int,
) -> list[dict[str, Any]]:
    if not event_id:
        _log(
            "read receipt lookup skipped; event_id empty: "
            f"room_id={_redacted_text(room_id)} index={message_index}"
        )
        return []

    event_positions = {
        str(message.get("event_id")): index
        for index, message in enumerate(ROOM_MESSAGES.get(room_id, []))
        if message.get("event_id")
    }
    receipts = []
    for receipt in ROOM_READ_RECEIPTS.get(room_id, {}).values():
        receipt_event_id = str(receipt.get("event_id") or "")
        receipt_index = event_positions.get(receipt_event_id)
        if receipt_event_id == event_id or (
            receipt_index is not None and receipt_index >= message_index
        ):
            receipts.append(receipt)

    sorted_receipts = sorted(
        receipts,
        key=lambda receipt: (
            int(receipt.get("timestamp") or 0),
            str(receipt.get("display_name") or receipt.get("user_id") or ""),
        ),
    )
    _log(
        "read receipts matched for inspect message: "
        f"room_id={_redacted_text(room_id)} "
        f"event_id={_redacted_text(event_id)} "
        f"message_index={message_index} receipts={len(sorted_receipts)}"
    )
    return sorted_receipts


def _format_read_receipts(receipts: list[dict[str, Any]]) -> str:
    _log(f"formatting read receipts: count={len(receipts)}")
    return " ".join(
        _colored_receipt_label(str(receipt.get("display_name") or ""))
        for receipt in receipts
    )


def _colored_receipt_label(display_name: str) -> str:
    label = display_name.strip()[:3]
    if not label:
        _log("read receipt label skipped for empty display name")
        return ""

    color = weechat.info_get("nick_color_name", display_name)
    if not color:
        _log(f"read receipt label uncolored: display_name={display_name!r}")
        return label

    _log(
        "read receipt label colored: "
        f"display_name={display_name!r} label={label!r} color={color}"
    )
    return f"{weechat.color(color)}{label}{weechat.color('reset')}"


def _single_line_body(body: str) -> str:
    body = " / ".join(line.strip() for line in body.splitlines()).strip()
    if len(body) <= 240:
        return body
    return body[:237] + "..."


def _remove_inspect_bar() -> None:
    global INSPECT_BAR, INSPECT_BAR_ITEM, TOPIC_BAR

    if INSPECT_BAR:
        _log(f"removing inspect bar: bar={INSPECT_BAR}")
        weechat.bar_remove(INSPECT_BAR)
        INSPECT_BAR = ""
    else:
        _log("inspect bar removal skipped; no inspect bar")
    if TOPIC_BAR:
        _log(f"removing topic bar: bar={TOPIC_BAR}")
        weechat.bar_remove(TOPIC_BAR)
        TOPIC_BAR = ""
    else:
        _log("topic bar removal skipped; no topic bar")
    if INSPECT_BAR_ITEM:
        _log(f"removing inspect bar item: item={INSPECT_BAR_ITEM}")
        weechat.bar_item_remove(INSPECT_BAR_ITEM)
        INSPECT_BAR_ITEM = ""
    else:
        _log("inspect bar item removal skipped; no item")


def _format_inspect_time(timestamp: int) -> str:
    if not timestamp:
        return "unknown"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
    except Exception:
        return str(timestamp)


def _safe_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")
    return safe[:80] or "room"


def _colored_nick(nick: str) -> str:
    if not nick:
        return ""

    color = weechat.info_get("nick_color_name", nick)
    if not color:
        return nick

    return f"{weechat.color(color)}{nick}{weechat.color('reset')}"


def worker_process_cb(data: str, command: str, return_code: int, out: Any, err: Any) -> int:
    global WORKER_HOOK

    return_code = int(return_code)
    out_len = len(out) if out else 0
    err_len = len(err) if err else 0
    _log(
        "worker process callback: "
        f"command={command} return_code={return_code} "
        f"out_bytes={out_len} err_bytes={err_len} data={data!r}"
    )

    if out:
        _consume_worker_output(out)
    if err:
        if isinstance(err, bytes):
            err = err.decode("utf-8", errors="replace")
        for line in str(err).splitlines():
            weechat.prnt("", "meechat worker stderr: " + line)

    if return_code == weechat.WEECHAT_HOOK_PROCESS_ERROR:
        WORKER_HOOK = ""
        weechat.prnt("", f"meechat: failed to launch worker command {command}")
        _log(f"worker launch failed: command={command}")
    elif return_code >= 0:
        WORKER_HOOK = ""
        _log(f"worker exited with code {return_code}")

    return weechat.WEECHAT_RC_OK


def room_input_cb(data: str, buffer: str, input_data: str) -> int:
    _log(
        "room input received: "
        f"buffer={buffer} data_room={_redacted_text(data)} "
        f"input_len={len(input_data)} slash_command={input_data.startswith('/')}"
    )
    if input_data.startswith("/"):
        _log(f"room input dispatching WeeChat command: {_command_summary(input_data)}")
        weechat.command(buffer, input_data)
        return weechat.WEECHAT_RC_OK

    room_id = data or BUFFER_ROOMS.get(buffer, "")
    if not room_id:
        _log("room input rejected because buffer is not mapped to a Matrix room")
        weechat.prnt(buffer, "meechat: this buffer is not mapped to a Matrix room")
        return weechat.WEECHAT_RC_OK

    _log(
        "room input sending Matrix message: "
        f"room_id={_redacted_text(room_id)} body_len={len(input_data)}"
    )
    _send_to_worker({"type": "send_message", "room_id": room_id, "body": input_data})
    return weechat.WEECHAT_RC_OK


def room_close_cb(data: str, buffer: str) -> int:
    room_id = data or BUFFER_ROOMS.get(buffer, "")
    _log(
        "room close callback: "
        f"buffer={buffer} room_id={_redacted_text(room_id)} data={_redacted_text(data)}"
    )
    if room_id:
        _restore_inspect_highlight(room_id)
        ROOM_BUFFERS.pop(room_id, None)
        ROOM_MESSAGES.pop(room_id, None)
        ROOM_INSPECT_INDEX.pop(room_id, None)
        ROOM_READ_RECEIPTS.pop(room_id, None)
        ROOM_TOPICS.pop(room_id, None)
        INSPECT_ACTION_STATE.pop(room_id, None)
        _update_inspect_bar()
        _log(
            "room state removed: "
            f"room_id={_redacted_text(room_id)} remaining_rooms={len(ROOM_BUFFERS)}"
        )
    BUFFER_ROOMS.pop(buffer, None)
    return weechat.WEECHAT_RC_OK


def script_unload_cb() -> int:
    _log("script unload requested")
    _restore_all_inspect_highlights()
    _clear_all_inspect_action_modes()
    if WORKER_HOOK:
        _log(f"script unload sending worker shutdown: hook={WORKER_HOOK}")
        _send_to_worker({"type": "shutdown"})
    else:
        _log("script unload found no active worker hook")
    _remove_inspect_bar()
    _log("script unload complete")
    return weechat.WEECHAT_RC_OK


def command_cb(data: str, buffer: str, args: str) -> int:
    argv = args.strip().split()
    subcommand = argv[0].lower() if argv else "help"
    _log(
        "command received: "
        f"buffer={buffer} data={data!r} subcommand={subcommand} "
        f"args={_command_summary(args)!r}"
    )

    if subcommand == "connect":
        _log("command dispatch: connect")
        _connect()
        return weechat.WEECHAT_RC_OK

    if subcommand == "disconnect":
        _log("command dispatch: disconnect")
        _disconnect()
        return weechat.WEECHAT_RC_OK

    if subcommand == "rooms":
        _log("command dispatch: rooms")
        _send_to_worker({"type": "rooms"})
        return weechat.WEECHAT_RC_OK

    if subcommand == "inspect":
        _log("command dispatch: inspect")
        _show_inspect(buffer)
        return weechat.WEECHAT_RC_OK

    if subcommand == "inspect-prev":
        _log("command dispatch: inspect-prev")
        _inspect_step(buffer, -1)
        return weechat.WEECHAT_RC_OK

    if subcommand == "inspect-next":
        _log("command dispatch: inspect-next")
        _inspect_step(buffer, 1)
        return weechat.WEECHAT_RC_OK

    if subcommand == "inspect-up":
        _log("command dispatch: inspect-up")
        _inspect_key_step(buffer, -1)
        return weechat.WEECHAT_RC_OK

    if subcommand == "inspect-down":
        _log("command dispatch: inspect-down")
        _inspect_key_step(buffer, 1)
        return weechat.WEECHAT_RC_OK

    if subcommand == "inspect-enter":
        _log("command dispatch: inspect-enter")
        _inspect_enter(buffer)
        return weechat.WEECHAT_RC_OK

    if subcommand == "inspect-action":
        _log("command dispatch: inspect-action")
        if len(argv) > 1:
            _choose_inspect_action(buffer, argv[1])
        else:
            _log("inspect-action command ignored; missing key argument")
        return weechat.WEECHAT_RC_OK

    if subcommand == "inspect-cancel":
        _log("command dispatch: inspect-cancel")
        _inspect_cancel(buffer)
        return weechat.WEECHAT_RC_OK

    if subcommand == "join":
        if len(argv) < 2:
            _log("join command rejected; missing room argument")
            weechat.prnt(buffer, "usage: /meechat join <room-id-or-alias>")
        else:
            _log(f"join command sending room={_redacted_text(argv[1])}")
            _send_to_worker({"type": "join", "room": argv[1]})
        return weechat.WEECHAT_RC_OK

    if subcommand in {"part", "leave"}:
        room_id = argv[1] if len(argv) > 1 else BUFFER_ROOMS.get(buffer, "")
        if not room_id:
            _log("leave command rejected; no room id argument or buffer mapping")
            weechat.prnt(buffer, "usage: /meechat leave <room-id>")
        else:
            _log(f"leave command sending room_id={_redacted_text(room_id)}")
            _send_to_worker({"type": "leave", "room_id": room_id})
        return weechat.WEECHAT_RC_OK

    if subcommand == "set":
        if len(argv) < 3:
            _log("set command rejected; missing option or value")
            weechat.prnt(buffer, "usage: /meechat set <option> <value>")
        else:
            name = argv[1]
            if name not in DEFAULT_OPTIONS:
                _log(f"set command rejected; unknown option={name}")
                weechat.prnt(buffer, f"meechat: unknown option {name}")
            else:
                weechat.config_set_plugin(name, args.split(None, 2)[2])
                weechat.prnt(buffer, f"meechat: set {name}")
                _log(
                    "set command applied: "
                    f"option={name} value={_option_value_summary(name, argv[2])}"
                )
        return weechat.WEECHAT_RC_OK

    if subcommand == "status":
        status = "running" if WORKER_HOOK else "stopped"
        weechat.prnt(buffer, f"meechat: worker {status}")
        _log(f"status command reported worker={status} hook={WORKER_HOOK}")
        return weechat.WEECHAT_RC_OK

    _log(f"unknown or help command; printing help for subcommand={subcommand}")
    _print_help(buffer)
    return weechat.WEECHAT_RC_OK


def _print_help(buffer: str) -> None:
    _log(f"printing help into buffer={buffer}")
    weechat.prnt(buffer, "MeeChat commands:")
    weechat.prnt(buffer, "  /meechat connect")
    weechat.prnt(buffer, "  /meechat disconnect")
    weechat.prnt(buffer, "  /meechat rooms")
    weechat.prnt(buffer, "  /meechat inspect")
    weechat.prnt(buffer, "  /meechat inspect-prev")
    weechat.prnt(buffer, "  /meechat inspect-next")
    weechat.prnt(buffer, "  /meechat inspect-enter")
    weechat.prnt(buffer, "  /meechat join <room-id-or-alias>")
    weechat.prnt(buffer, "  /meechat leave [room-id]")
    weechat.prnt(buffer, "  /meechat set <option> <value>")
    weechat.prnt(buffer, "  /meechat status")


def _register() -> None:
    if weechat is None:
        return

    _log(
        "startup: loading MeeChat "
        f"name={SCRIPT_NAME} version={SCRIPT_VERSION} "
        f"script_dir={SCRIPT_DIR} worker_path={_worker_path()} "
        f"python={sys.executable} cwd={os.getcwd()}"
    )
    if not weechat.register(
        SCRIPT_NAME,
        SCRIPT_AUTHOR,
        SCRIPT_VERSION,
        SCRIPT_LICENSE,
        SCRIPT_DESC,
        "script_unload_cb",
        "",
    ):
        _log("startup: weechat.register returned false; script is not registered")
        return

    _log("startup: weechat.register succeeded")
    _init_options()
    _log("startup: registering /meechat command")
    weechat.hook_command(
        "meechat",
        "Matrix client adapter",
        "connect | disconnect | rooms | inspect | inspect-prev | inspect-next | inspect-enter | "
        "join <room> | leave [room-id] | set <option> <value> | status",
        "connect: connect to Matrix\n"
        "disconnect: stop the Matrix worker\n"
        "rooms: refresh joined room buffers\n"
        "inspect: show the message inspector monitor for the current Matrix room\n"
        "inspect-prev: inspect the previous message in the current Matrix room\n"
        "inspect-next: inspect the next message in the current Matrix room\n"
        "inspect-enter: open or advance the selected message action menu\n"
        "join: join a Matrix room by alias or room ID\n"
        "leave: leave the current room buffer or provided room ID\n"
        "set: set a MeeChat plugin option\n"
        "status: show worker status",
        "connect || disconnect || rooms || inspect || inspect-prev || "
        "inspect-next || inspect-enter || join || leave || set || status",
        "command_cb",
        "",
    )
    _log("startup: /meechat command registered")
    _ensure_inspect_bar()

    if _option_bool("autoconnect"):
        _log("startup: autoconnect is on; connecting")
        _connect()
    else:
        _log("startup: autoconnect is off")


_register()
