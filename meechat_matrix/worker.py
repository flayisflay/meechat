"""Async Matrix worker process used by the WeeChat script.

The worker owns all Matrix network I/O and speaks newline-delimited JSON on
stdin/stdout. Keeping this process separate avoids blocking WeeChat's Python
script runtime.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
import traceback
from dataclasses import dataclass
from typing import Any, TextIO

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meechat_matrix import __version__
from meechat_matrix.protocol import ProtocolError, dumps, loads


@dataclass(slots=True)
class WorkerConfig:
    auth: str
    homeserver: str
    user_id: str
    access_token: str
    device_id: str
    username: str = ""
    password: str = ""
    store_path: str = ""
    sync_timeout_ms: int = 30000
    backlog_limit: int = 100

    @classmethod
    def from_message(cls, message: dict[str, Any]) -> "WorkerConfig":
        auth = str(message.get("auth") or "auto").strip().lower()
        homeserver = str(message.get("homeserver") or "").strip()
        user_id = str(message.get("user_id") or "").strip()
        access_token = str(message.get("access_token") or "").strip()
        device_id = str(message.get("device_id") or "").strip()
        username = str(message.get("username") or user_id).strip()
        password = str(message.get("password") or "").strip()

        missing = []
        if auth not in {"auto", "token", "password"}:
            missing.append("auth must be auto, token, or password")
        if not homeserver:
            missing.append("homeserver")
        use_password = auth == "password" or (auth == "auto" and password)
        use_token = auth == "token" or (auth == "auto" and not use_password)
        if use_password:
            if not password:
                missing.append("password")
            if not username:
                missing.append("username")
        elif use_token:
            if not access_token:
                missing.append("access_token")
            if not user_id:
                missing.append("user_id")
            if not device_id:
                missing.append("device_id")

        if missing:
            raise ProtocolError("missing config field(s): " + ", ".join(missing))

        try:
            sync_timeout_ms = int(message.get("sync_timeout_ms") or 30000)
        except (TypeError, ValueError) as exc:
            raise ProtocolError("sync_timeout_ms must be an integer") from exc

        if sync_timeout_ms < 1000:
            raise ProtocolError("sync_timeout_ms must be at least 1000")

        try:
            backlog_limit = int(message.get("backlog_limit") or 100)
        except (TypeError, ValueError) as exc:
            raise ProtocolError("backlog_limit must be an integer") from exc

        if backlog_limit < 1:
            raise ProtocolError("backlog_limit must be at least 1")

        return cls(
            auth=auth,
            homeserver=homeserver,
            user_id=user_id,
            access_token=access_token,
            device_id=device_id,
            username=username,
            password=password,
            store_path=str(message.get("store_path") or "").strip(),
            sync_timeout_ms=sync_timeout_ms,
            backlog_limit=backlog_limit,
        )

    @property
    def use_password_login(self) -> bool:
        return self.auth == "password" or (self.auth == "auto" and bool(self.password))


class MatrixWorker:
    def __init__(
        self,
        stdin: TextIO = sys.stdin,
        stdout: TextIO = sys.stdout,
        stderr: TextIO = sys.stderr,
        debug: bool = False,
    ) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.debug = debug
        self.config: WorkerConfig | None = None
        self.client: Any | None = None
        self.sync_task: asyncio.Task[None] | None = None
        self.connected = False
        self.stopping = asyncio.Event()
        self._write_lock = asyncio.Lock()

    async def run(self) -> None:
        await self.emit({"type": "ready", "version": __version__})

        while not self.stopping.is_set():
            line = await asyncio.to_thread(self.stdin.readline)
            if line == "":
                break

            try:
                keep_running = await self.handle(loads(line))
            except ProtocolError as exc:
                await self.emit_error(str(exc), where="protocol")
                keep_running = True
            except Exception as exc:  # pragma: no cover - last-resort guard
                await self.emit_error(str(exc), where="worker")
                if self.debug:
                    traceback.print_exc(file=self.stderr)
                keep_running = True

            if not keep_running:
                break

        await self.disconnect()

    async def handle(self, message: dict[str, Any]) -> bool:
        message_type = message["type"]

        if message_type == "configure":
            self.config = WorkerConfig.from_message(message)
            await self.emit({"type": "configured"})
            return True

        if message_type == "connect":
            await self.connect()
            return True

        if message_type == "disconnect":
            await self.disconnect()
            return True

        if message_type == "shutdown":
            self.stopping.set()
            return False

        if message_type == "rooms":
            await self.emit_rooms()
            return True

        if message_type == "send_message":
            await self.send_message(
                room_id=str(message.get("room_id") or ""),
                body=str(message.get("body") or ""),
            )
            return True

        if message_type == "message_action":
            await self.message_action(
                room_id=str(message.get("room_id") or ""),
                event_id=str(message.get("event_id") or ""),
                action=str(message.get("action") or ""),
                body=str(message.get("body") or ""),
                reason=str(message.get("reason") or ""),
            )
            return True

        if message_type == "join":
            await self.join(str(message.get("room") or ""))
            return True

        if message_type == "leave":
            await self.leave(str(message.get("room_id") or ""))
            return True

        await self.emit_error(f"unknown command type: {message_type}", where="command")
        return True

    async def connect(self) -> None:
        if self.config is None:
            await self.emit_error("worker is not configured", where="connect")
            return

        if self.client is not None:
            if self.connected:
                await self.emit({"type": "connected", "user_id": self.config.user_id})
            else:
                await self.emit_error(
                    "connection attempt already in progress",
                    where="connect",
                )
            return

        try:
            from nio import AsyncClient, ReceiptEvent, RoomMessageText

            try:
                from nio import AsyncClientConfig as ClientConfig
            except ImportError:
                from nio import ClientConfig  # type: ignore
        except ModuleNotFoundError:
            await self.emit_error(
                "matrix-nio is not installed; run `pip install -e .` or "
                "`pip install matrix-nio` in the Python environment WeeChat uses",
                where="connect",
            )
            return

        nio_config = ClientConfig(store_sync_tokens=False)
        client_user = self.config.username or self.config.user_id
        self.client = AsyncClient(
            self.config.homeserver,
            client_user,
            device_id=self.config.device_id,
            store_path=self.config.store_path,
            config=nio_config,
        )

        if not self.config.use_password_login and hasattr(self.client, "restore_login"):
            self.client.restore_login(
                self.config.user_id,
                self.config.device_id,
                self.config.access_token,
            )
        elif not self.config.use_password_login:
            self.client.user_id = self.config.user_id
            self.client.device_id = self.config.device_id
            self.client.access_token = self.config.access_token
        else:
            response = await self.client.login(
                password=self.config.password,
                device_name="MeeChat",
            )
            if response.__class__.__name__ != "LoginResponse":
                await self.emit_error(str(response), where="login")
                await self.close_client()
                return

            self.config.user_id = str(
                getattr(response, "user_id", "")
                or getattr(self.client, "user_id", "")
                or client_user
            )
            self.config.device_id = str(
                getattr(response, "device_id", "")
                or getattr(self.client, "device_id", "")
                or ""
            )
            self.config.access_token = str(
                getattr(response, "access_token", "")
                or getattr(self.client, "access_token", "")
                or ""
            )

        self.client.add_event_callback(self.on_room_message, RoomMessageText)
        add_ephemeral_callback = getattr(self.client, "add_ephemeral_callback", None)
        if add_ephemeral_callback is not None:
            add_ephemeral_callback(self.on_receipt, ReceiptEvent)
        self.sync_task = asyncio.create_task(self.sync_loop())

    async def disconnect(self) -> None:
        current_task = asyncio.current_task()
        if self.sync_task is not None and self.sync_task is not current_task:
            self.sync_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.sync_task
            self.sync_task = None

        await self.close_client()

        await self.emit({"type": "disconnected"})

    async def close_client(self) -> None:
        if self.client is not None:
            close = getattr(self.client, "close", None)
            if close is not None:
                await close()
            self.client = None
        self.connected = False

    async def sync_loop(self) -> None:
        assert self.client is not None
        assert self.config is not None

        first_sync = True
        backoff = 1

        try:
            while True:
                try:
                    response = await self.client.sync(
                        timeout=self.config.sync_timeout_ms,
                        sync_filter=_sync_filter(self.config.backlog_limit),
                        full_state=first_sync,
                    )
                    if response.__class__.__name__ == "SyncResponse":
                        if first_sync:
                            self.connected = True
                            await self.emit(
                                {"type": "connected", "user_id": self.config.user_id}
                            )
                        await self.emit(
                            {
                                "type": "sync",
                                "first": first_sync,
                                "next_batch": getattr(self.client, "next_batch", ""),
                            }
                        )
                        await self.emit_rooms()
                        first_sync = False
                        backoff = 1
                        continue

                    if _is_unknown_token_response(response):
                        await self.emit_error(
                            _unknown_token_message(response),
                            where="connect",
                        )
                        await self.close_client()
                        await self.emit({"type": "disconnected"})
                        return

                    await self.emit_error(str(response), where="sync")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await self.emit_error(str(exc), where="sync")
                    if self.debug:
                        traceback.print_exc(file=self.stderr)

                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
        finally:
            if self.sync_task is asyncio.current_task():
                self.sync_task = None

    async def on_room_message(self, room: Any, event: Any) -> None:
        room_id = getattr(room, "room_id", "")
        sender = getattr(event, "sender", "")
        body = getattr(event, "body", "")
        if not room_id or not body:
            return

        await self.emit(
            {
                "type": "message",
                "room_id": room_id,
                "room_name": _room_name(room),
                "room_topic": _room_topic(room),
                "sender": sender,
                "sender_display_name": _sender_display_name(room, sender),
                "body": body,
                "event_id": getattr(event, "event_id", ""),
                "server_timestamp": getattr(event, "server_timestamp", 0),
            }
        )

    async def on_receipt(self, room: Any, event: Any) -> None:
        room_id = getattr(room, "room_id", "")
        if not room_id:
            return

        receipts = []
        for receipt in getattr(event, "receipts", []) or []:
            if _receipt_type_value(getattr(receipt, "receipt_type", "")) != "m.read":
                continue

            event_id = str(getattr(receipt, "event_id", "") or "")
            user_id = str(getattr(receipt, "user_id", "") or "")
            if not event_id or not user_id:
                continue

            receipts.append(
                {
                    "event_id": event_id,
                    "user_id": user_id,
                    "display_name": _sender_display_name(room, user_id),
                    "timestamp": int(getattr(receipt, "timestamp", 0) or 0),
                }
            )

        if receipts:
            await self.emit(
                {
                    "type": "read_receipts",
                    "room_id": room_id,
                    "receipts": receipts,
                }
            )

    async def send_message(self, room_id: str, body: str) -> None:
        if self.client is None:
            await self.emit_error("not connected", where="send_message")
            return

        if not room_id.strip():
            await self.emit_error("room_id is required", where="send_message")
            return

        if not body:
            return

        response = await self.client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": body},
        )
        event_id = getattr(response, "event_id", "")
        if event_id:
            await self.emit(
                {"type": "send_ok", "room_id": room_id, "event_id": event_id}
            )
            return

        await self.emit_error(str(response), where="send_message", room_id=room_id)

    async def message_action(
        self,
        *,
        room_id: str,
        event_id: str,
        action: str,
        body: str = "",
        reason: str = "",
    ) -> None:
        if self.client is None:
            await self.emit_error("not connected", where="message_action")
            return

        if not room_id.strip() or not event_id.strip():
            await self.emit_error(
                "room_id and event_id are required",
                where="message_action",
                room_id=room_id,
            )
            return

        if action == "remove":
            response = await self.client.room_redact(
                room_id=room_id,
                event_id=event_id,
                reason=reason.strip() or None,
            )
            await self.emit_action_response(action, room_id, response)
            return

        message_type, content = _message_action_event(action, event_id, body)
        if content is None:
            await self.emit_error(
                f"unsupported or incomplete message action: {action}",
                where="message_action",
                room_id=room_id,
            )
            return

        response = await self.client.room_send(
            room_id=room_id,
            message_type=message_type,
            content=content,
        )
        await self.emit_action_response(action, room_id, response)

    async def emit_action_response(
        self,
        action: str,
        room_id: str,
        response: Any,
    ) -> None:
        event_id = getattr(response, "event_id", "")
        if event_id:
            await self.emit(
                {
                    "type": "action_ok",
                    "action": action,
                    "room_id": room_id,
                    "event_id": event_id,
                }
            )
            return

        await self.emit_error(str(response), where="message_action", room_id=room_id)

    async def join(self, room: str) -> None:
        if self.client is None:
            await self.emit_error("not connected", where="join")
            return

        if not room.strip():
            await self.emit_error("room alias or id is required", where="join")
            return

        response = await self.client.join(room.strip())
        room_id = getattr(response, "room_id", "")
        if room_id:
            await self.emit({"type": "joined", "room_id": room_id, "room": room})
            return

        await self.emit_error(str(response), where="join")

    async def leave(self, room_id: str) -> None:
        if self.client is None:
            await self.emit_error("not connected", where="leave")
            return

        if not room_id.strip():
            await self.emit_error("room_id is required", where="leave")
            return

        response = await self.client.room_leave(room_id.strip())
        if response.__class__.__name__ == "RoomLeaveResponse":
            await self.emit({"type": "left", "room_id": room_id})
            return

        await self.emit_error(str(response), where="leave", room_id=room_id)

    async def emit_rooms(self) -> None:
        if self.client is None:
            await self.emit_error("not connected", where="rooms")
            return

        rooms = getattr(self.client, "rooms", {}) or {}
        for room_id, room in rooms.items():
            await self.emit(
                {
                    "type": "room",
                    "room_id": room_id,
                    "name": _room_name(room),
                    "topic": _room_topic(room),
                    "member_count": len(getattr(room, "users", {}) or {}),
                    "encrypted": bool(getattr(room, "encrypted", False)),
                }
            )

    async def emit_error(
        self,
        message: str,
        *,
        where: str,
        room_id: str = "",
    ) -> None:
        payload: dict[str, Any] = {"type": "error", "where": where, "message": message}
        if room_id:
            payload["room_id"] = room_id
        await self.emit(payload)

    async def emit(self, message: dict[str, Any]) -> None:
        async with self._write_lock:
            self.stdout.write(dumps(message))
            self.stdout.flush()


def _room_name(room: Any) -> str:
    for attribute in ("display_name", "name"):
        value = getattr(room, attribute, "")
        if value:
            return str(value)
    return str(getattr(room, "room_id", ""))


def _room_topic(room: Any) -> str:
    return str(getattr(room, "topic", "") or "")


def _message_action_event(
    action: str,
    event_id: str,
    body: str,
) -> tuple[str, dict[str, Any] | None]:
    text = body.strip()
    if action in {"reply", "thread", "edit", "reaction"} and not text:
        return "", None

    if action == "reply":
        return (
            "m.room.message",
            {
                "msgtype": "m.text",
                "body": text,
                "m.relates_to": {"m.in_reply_to": {"event_id": event_id}},
            },
        )

    if action == "thread":
        return (
            "m.room.message",
            {
                "msgtype": "m.text",
                "body": text,
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": event_id,
                    "is_falling_back": True,
                    "m.in_reply_to": {"event_id": event_id},
                },
            },
        )

    if action == "edit":
        return (
            "m.room.message",
            {
                "msgtype": "m.text",
                "body": "* " + text,
                "m.new_content": {"msgtype": "m.text", "body": text},
                "m.relates_to": {"rel_type": "m.replace", "event_id": event_id},
            },
        )

    if action == "reaction":
        return (
            "m.reaction",
            {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": event_id,
                    "key": text,
                }
            },
        )

    return "", None


def _sender_display_name(room: Any, sender: str) -> str:
    users = getattr(room, "users", {}) or {}
    user = users.get(sender)
    if user is not None:
        display_name = getattr(user, "display_name", "")
        if display_name:
            return str(display_name)

    user_name = getattr(room, "user_name", None)
    if callable(user_name):
        name = user_name(sender)
        if name:
            return str(name)

    return sender


def _receipt_type_value(receipt_type: Any) -> str:
    return str(getattr(receipt_type, "value", receipt_type) or "")


def _sync_filter(backlog_limit: int) -> dict[str, Any]:
    return {"room": {"timeline": {"limit": backlog_limit}}}


def _is_unknown_token_response(response: Any) -> bool:
    return getattr(response, "status_code", "") == "M_UNKNOWN_TOKEN"


def _unknown_token_message(response: Any) -> str:
    message = str(getattr(response, "message", "") or response)
    return (
        f"{message}; the homeserver rejected the active access token. If auth is "
        "token, check homeserver, user_id, device_id, and access_token; if auth "
        "is password, this was the token returned by the login request"
    )


async def async_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    worker = MatrixWorker(debug=args.debug)
    await worker.run()
    return 0


def main() -> int:
    try:
        return asyncio.run(async_main())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
