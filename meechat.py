"""WeeChat script entry point for MeeChat.

Install by symlinking this file into WeeChat's Python script directory, for
example:

    ln -s /path/to/meechat/meechat.py ~/.weechat/python/autoload/meechat.py
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any

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
    "python": "Python executable used to start the Matrix worker",
    "autoconnect": "Connect when the script loads: on/off",
    "debug": "Print worker stderr and protocol details: on/off",
}

WORKER_HOOK = ""
WORKER_STDOUT = ""
ROOM_BUFFERS: dict[str, str] = {}
BUFFER_ROOMS: dict[str, str] = {}
ROOM_NAMES: dict[str, str] = {}


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


def _init_options() -> None:
    for name, default in DEFAULT_OPTIONS.items():
        if not weechat.config_is_set_plugin(name):
            weechat.config_set_plugin(name, default)
        weechat.config_set_desc_plugin(name, OPTION_DESCRIPTIONS[name])


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
        weechat.prnt(
            "",
            "meechat: missing required option(s): " + ", ".join(missing),
        )
        return None
    return message


def _start_worker() -> bool:
    global WORKER_HOOK

    if WORKER_HOOK:
        return True

    python = _option("python", expand=True) or "python3"
    options = {"arg1": _worker_path(), "stdin": "1", "buffer_flush": "1"}
    if _option_bool("debug"):
        options["arg2"] = "--debug"

    WORKER_HOOK = weechat.hook_process_hashtable(
        python,
        options,
        0,
        "worker_process_cb",
        "",
    )
    if not WORKER_HOOK:
        weechat.prnt("", "meechat: failed to start Matrix worker")
        return False
    return True


def _send_to_worker(message: dict[str, Any]) -> bool:
    if not WORKER_HOOK and not _start_worker():
        return False

    try:
        payload = dumps(message)
    except ProtocolError as exc:
        weechat.prnt("", f"meechat: internal protocol error: {exc}")
        return False

    weechat.hook_set(WORKER_HOOK, "stdin", payload)
    return True


def _connect() -> None:
    message = _config_message()
    if message is None:
        return

    if not _start_worker():
        return

    _send_to_worker(message)
    _send_to_worker({"type": "connect"})


def _disconnect() -> None:
    if WORKER_HOOK:
        _send_to_worker({"type": "shutdown"})


def _consume_worker_output(output: str | bytes) -> None:
    global WORKER_STDOUT

    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")

    WORKER_STDOUT += output
    while "\n" in WORKER_STDOUT:
        line, WORKER_STDOUT = WORKER_STDOUT.split("\n", 1)
        if not line.strip():
            continue
        try:
            _handle_worker_message(loads(line))
        except ProtocolError as exc:
            weechat.prnt("", f"meechat: invalid worker output: {exc}")


def _handle_worker_message(message: dict[str, Any]) -> None:
    message_type = message["type"]

    if message_type == "ready":
        _debug(f"worker ready {message.get('version', '')}")
        return

    if message_type == "configured":
        _debug("worker configured")
        return

    if message_type == "connected":
        weechat.prnt("", f"meechat: connected as {message.get('user_id', '')}")
        return

    if message_type == "disconnected":
        weechat.prnt("", "meechat: disconnected")
        return

    if message_type == "sync":
        _debug("sync ok")
        return

    if message_type == "room":
        _record_room(
            str(message.get("room_id") or ""),
            str(message.get("name") or ""),
            encrypted=bool(message.get("encrypted", False)),
        )
        return

    if message_type == "message":
        _print_matrix_message(message)
        return

    if message_type == "send_ok":
        _debug(f"sent {message.get('event_id', '')}")
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
        return

    _debug(f"ignored worker message: {message}")


def _record_room(room_id: str, name: str, *, encrypted: bool = False) -> str:
    if not room_id:
        return ""

    ROOM_NAMES[room_id] = name or room_id
    buffer = _buffer_for_room(room_id)
    if not buffer:
        buffer = _create_room_buffer(room_id, name or room_id)
    else:
        _update_room_buffer(buffer, room_id, name or room_id, encrypted=encrypted)
    return buffer


def _buffer_for_room(room_id: str) -> str:
    return ROOM_BUFFERS.get(room_id, "")


def _create_room_buffer(room_id: str, name: str) -> str:
    buffer_name = "meechat." + _safe_name(room_id)
    buffer = weechat.buffer_new(
        buffer_name,
        "room_input_cb",
        room_id,
        "room_close_cb",
        room_id,
    )
    if not buffer:
        weechat.prnt("", f"meechat: failed to create buffer for {room_id}")
        return ""

    ROOM_BUFFERS[room_id] = buffer
    BUFFER_ROOMS[buffer] = room_id
    _update_room_buffer(buffer, room_id, name)
    return buffer


def _update_room_buffer(
    buffer: str,
    room_id: str,
    name: str,
    *,
    encrypted: bool = False,
) -> None:
    short_name = name or room_id
    if encrypted:
        short_name += " [encrypted]"

    weechat.buffer_set(buffer, "short_name", short_name)
    weechat.buffer_set(buffer, "title", f"Matrix: {name or room_id} ({room_id})")
    weechat.buffer_set(buffer, "localvar_set_type", "matrix")
    weechat.buffer_set(buffer, "localvar_set_room_id", room_id)


def _print_matrix_message(message: dict[str, Any]) -> None:
    room_id = str(message.get("room_id") or "")
    room_name = str(message.get("room_name") or ROOM_NAMES.get(room_id, room_id))
    buffer = _record_room(room_id, room_name)
    if not buffer:
        return

    sender = str(message.get("sender") or "")
    body = str(message.get("body") or "")
    timestamp_ms = int(message.get("server_timestamp") or 0)
    timestamp = timestamp_ms // 1000 if timestamp_ms else 0
    tags = "notify_message"
    if sender:
        tags += ",nick_" + sender
    weechat.prnt_date_tags(buffer, timestamp, tags, f"{sender}\t{body}")


def _safe_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")
    return safe[:80] or "room"


def _debug(message: str) -> None:
    if _option_bool("debug"):
        weechat.prnt("", "meechat debug: " + message)


def worker_process_cb(data: str, command: str, return_code: int, out: Any, err: Any) -> int:
    global WORKER_HOOK

    return_code = int(return_code)

    if out:
        _consume_worker_output(out)
    if err and _option_bool("debug"):
        if isinstance(err, bytes):
            err = err.decode("utf-8", errors="replace")
        for line in str(err).splitlines():
            weechat.prnt("", "meechat worker stderr: " + line)

    if return_code == weechat.WEECHAT_HOOK_PROCESS_ERROR:
        WORKER_HOOK = ""
        weechat.prnt("", f"meechat: failed to launch worker command {command}")
    elif return_code >= 0:
        WORKER_HOOK = ""
        _debug(f"worker exited with code {return_code}")

    return weechat.WEECHAT_RC_OK


def room_input_cb(data: str, buffer: str, input_data: str) -> int:
    if input_data.startswith("/"):
        weechat.command(buffer, input_data)
        return weechat.WEECHAT_RC_OK

    room_id = data or BUFFER_ROOMS.get(buffer, "")
    if not room_id:
        weechat.prnt(buffer, "meechat: this buffer is not mapped to a Matrix room")
        return weechat.WEECHAT_RC_OK

    _send_to_worker({"type": "send_message", "room_id": room_id, "body": input_data})
    return weechat.WEECHAT_RC_OK


def room_close_cb(data: str, buffer: str) -> int:
    room_id = data or BUFFER_ROOMS.get(buffer, "")
    if room_id:
        ROOM_BUFFERS.pop(room_id, None)
    BUFFER_ROOMS.pop(buffer, None)
    return weechat.WEECHAT_RC_OK


def script_unload_cb() -> int:
    if WORKER_HOOK:
        _send_to_worker({"type": "shutdown"})
    return weechat.WEECHAT_RC_OK


def command_cb(data: str, buffer: str, args: str) -> int:
    argv = args.strip().split()
    subcommand = argv[0].lower() if argv else "help"

    if subcommand == "connect":
        _connect()
        return weechat.WEECHAT_RC_OK

    if subcommand == "disconnect":
        _disconnect()
        return weechat.WEECHAT_RC_OK

    if subcommand == "rooms":
        _send_to_worker({"type": "rooms"})
        return weechat.WEECHAT_RC_OK

    if subcommand == "join":
        if len(argv) < 2:
            weechat.prnt(buffer, "usage: /meechat join <room-id-or-alias>")
        else:
            _send_to_worker({"type": "join", "room": argv[1]})
        return weechat.WEECHAT_RC_OK

    if subcommand in {"part", "leave"}:
        room_id = argv[1] if len(argv) > 1 else BUFFER_ROOMS.get(buffer, "")
        if not room_id:
            weechat.prnt(buffer, "usage: /meechat leave <room-id>")
        else:
            _send_to_worker({"type": "leave", "room_id": room_id})
        return weechat.WEECHAT_RC_OK

    if subcommand == "set":
        if len(argv) < 3:
            weechat.prnt(buffer, "usage: /meechat set <option> <value>")
        else:
            name = argv[1]
            if name not in DEFAULT_OPTIONS:
                weechat.prnt(buffer, f"meechat: unknown option {name}")
            else:
                weechat.config_set_plugin(name, args.split(None, 2)[2])
                weechat.prnt(buffer, f"meechat: set {name}")
        return weechat.WEECHAT_RC_OK

    if subcommand == "status":
        status = "running" if WORKER_HOOK else "stopped"
        weechat.prnt(buffer, f"meechat: worker {status}")
        return weechat.WEECHAT_RC_OK

    _print_help(buffer)
    return weechat.WEECHAT_RC_OK


def _print_help(buffer: str) -> None:
    weechat.prnt(buffer, "MeeChat commands:")
    weechat.prnt(buffer, "  /meechat connect")
    weechat.prnt(buffer, "  /meechat disconnect")
    weechat.prnt(buffer, "  /meechat rooms")
    weechat.prnt(buffer, "  /meechat join <room-id-or-alias>")
    weechat.prnt(buffer, "  /meechat leave [room-id]")
    weechat.prnt(buffer, "  /meechat set <option> <value>")
    weechat.prnt(buffer, "  /meechat status")


def _register() -> None:
    if weechat is None:
        return

    if not weechat.register(
        SCRIPT_NAME,
        SCRIPT_AUTHOR,
        SCRIPT_VERSION,
        SCRIPT_LICENSE,
        SCRIPT_DESC,
        "script_unload_cb",
        "",
    ):
        return

    _init_options()
    weechat.hook_command(
        "meechat",
        "Matrix client adapter",
        "connect | disconnect | rooms | join <room> | leave [room-id] | set <option> <value> | status",
        "connect: connect to Matrix\n"
        "disconnect: stop the Matrix worker\n"
        "rooms: refresh joined room buffers\n"
        "join: join a Matrix room by alias or room ID\n"
        "leave: leave the current room buffer or provided room ID\n"
        "set: set a MeeChat plugin option\n"
        "status: show worker status",
        "connect || disconnect || rooms || join || leave || set || status",
        "command_cb",
        "",
    )

    if _option_bool("autoconnect"):
        _connect()


_register()
