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


def _redacted_text(value: Any) -> str:
    text = str(value or "")
    if not text:
        return "<empty>"
    if len(text) <= 8:
        return "<set>"
    return text[:4] + "..." + text[-4:]


def _message_summary(message: dict[str, Any]) -> str:
    message_type = str(message.get("type") or "")
    parts = [f"type={message_type}"]
    for key in (
        "auth",
        "where",
        "room_id",
        "room",
        "event_id",
        "user_id",
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
    if key in {"room_id", "room", "event_id", "user_id"}:
        return _redacted_text(value)
    return str(value)


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
        self.log(
            "initialized "
            f"version={__version__} debug={self.debug} pid={os.getpid()} "
            f"python={sys.executable}"
        )

    def log(self, message: str) -> None:
        self.stderr.write("meechat-worker: " + message + "\n")
        self.stderr.flush()

    def config_summary(self) -> str:
        if self.config is None:
            return "<not configured>"
        return (
            f"auth={self.config.auth} "
            f"use_password_login={self.config.use_password_login} "
            f"homeserver={_redacted_text(self.config.homeserver)} "
            f"user_id={_redacted_text(self.config.user_id)} "
            f"username={_redacted_text(self.config.username)} "
            f"device_id={_redacted_text(self.config.device_id)} "
            f"token_present={bool(self.config.access_token)} "
            f"password_present={bool(self.config.password)} "
            f"store_path={self.config.store_path or '<empty>'} "
            f"sync_timeout_ms={self.config.sync_timeout_ms} "
            f"backlog_limit={self.config.backlog_limit}"
        )

    async def run(self) -> None:
        self.log(f"run starting cwd={os.getcwd()}")
        await self.emit({"type": "ready", "version": __version__})
        self.log("ready emitted; entering stdin command loop")

        while not self.stopping.is_set():
            line = await asyncio.to_thread(self.stdin.readline)
            if line == "":
                self.log("stdin EOF received; leaving command loop")
                break

            try:
                self.log(f"stdin line received bytes={len(line)}")
                keep_running = await self.handle(loads(line))
            except ProtocolError as exc:
                self.log(f"protocol error while handling stdin: {exc}")
                await self.emit_error(str(exc), where="protocol")
                keep_running = True
            except Exception as exc:  # pragma: no cover - last-resort guard
                self.log(f"unhandled worker exception: {exc}")
                await self.emit_error(str(exc), where="worker")
                if self.debug:
                    traceback.print_exc(file=self.stderr)
                keep_running = True

            if not keep_running:
                self.log("command requested worker stop")
                break

        self.log("command loop ending; disconnecting")
        await self.disconnect()
        self.log("run complete")

    async def handle(self, message: dict[str, Any]) -> bool:
        message_type = message["type"]
        self.log("handling command: " + _message_summary(message))

        if message_type == "configure":
            self.config = WorkerConfig.from_message(message)
            self.log("configured: " + self.config_summary())
            await self.emit({"type": "configured"})
            return True

        if message_type == "connect":
            self.log("connect command received")
            await self.connect()
            return True

        if message_type == "disconnect":
            self.log("disconnect command received")
            await self.disconnect()
            return True

        if message_type == "shutdown":
            self.log("shutdown command received")
            self.stopping.set()
            return False

        if message_type == "rooms":
            self.log("rooms command received")
            await self.emit_rooms()
            return True

        if message_type == "send_message":
            self.log(
                "send_message command received: "
                f"room_id={_redacted_text(message.get('room_id', ''))} "
                f"body_len={len(str(message.get('body') or ''))}"
            )
            await self.send_message(
                room_id=str(message.get("room_id") or ""),
                body=str(message.get("body") or ""),
            )
            return True

        if message_type == "message_action":
            self.log(
                "message_action command received: "
                f"action={message.get('action', '')} "
                f"room_id={_redacted_text(message.get('room_id', ''))} "
                f"event_id={_redacted_text(message.get('event_id', ''))} "
                f"body_len={len(str(message.get('body') or ''))} "
                f"reason_len={len(str(message.get('reason') or ''))}"
            )
            await self.message_action(
                room_id=str(message.get("room_id") or ""),
                event_id=str(message.get("event_id") or ""),
                action=str(message.get("action") or ""),
                body=str(message.get("body") or ""),
                reason=str(message.get("reason") or ""),
            )
            return True

        if message_type == "join":
            self.log(f"join command received: room={_redacted_text(message.get('room'))}")
            await self.join(str(message.get("room") or ""))
            return True

        if message_type == "leave":
            self.log(
                "leave command received: "
                f"room_id={_redacted_text(message.get('room_id'))}"
            )
            await self.leave(str(message.get("room_id") or ""))
            return True

        self.log(f"unknown command type received: {message_type}")
        await self.emit_error(f"unknown command type: {message_type}", where="command")
        return True

    async def connect(self) -> None:
        self.log("connect starting: " + self.config_summary())
        if self.config is None:
            self.log("connect rejected; worker is not configured")
            await self.emit_error("worker is not configured", where="connect")
            return

        if self.client is not None:
            if self.connected:
                self.log("connect requested while already connected")
                await self.emit({"type": "connected", "user_id": self.config.user_id})
            else:
                self.log("connect rejected; connection attempt already in progress")
                await self.emit_error(
                    "connection attempt already in progress",
                    where="connect",
                )
            return

        try:
            self.log("importing matrix-nio client classes")
            from nio import AsyncClient, ReceiptEvent, RoomMessageText

            try:
                from nio import AsyncClientConfig as ClientConfig
            except ImportError:
                from nio import ClientConfig  # type: ignore
            try:
                from nio import RoomTopicEvent
            except ImportError:
                RoomTopicEvent = None  # type: ignore[assignment]
            try:
                from nio import ReactionEvent
            except ImportError:
                ReactionEvent = None  # type: ignore[assignment]
            self.log("matrix-nio import succeeded")
        except ModuleNotFoundError:
            self.log("matrix-nio import failed; module is not installed")
            await self.emit_error(
                "matrix-nio is not installed; run `pip install -e .` or "
                "`pip install matrix-nio` in the Python environment WeeChat uses",
                where="connect",
            )
            return

        nio_config = ClientConfig(store_sync_tokens=False)
        client_user = self.config.username or self.config.user_id
        self.log(
            "creating AsyncClient: "
            f"homeserver={_redacted_text(self.config.homeserver)} "
            f"client_user={_redacted_text(client_user)} "
            f"device_id={_redacted_text(self.config.device_id)} "
            f"store_path={self.config.store_path or '<empty>'}"
        )
        self.client = AsyncClient(
            self.config.homeserver,
            client_user,
            device_id=self.config.device_id,
            store_path=self.config.store_path,
            config=nio_config,
        )

        if not self.config.use_password_login and hasattr(self.client, "restore_login"):
            self.log(
                "restoring token login through AsyncClient.restore_login: "
                f"user_id={_redacted_text(self.config.user_id)} "
                f"device_id={_redacted_text(self.config.device_id)} "
                f"token_present={bool(self.config.access_token)}"
            )
            self.client.restore_login(
                self.config.user_id,
                self.config.device_id,
                self.config.access_token,
            )
        elif not self.config.use_password_login:
            self.log(
                "restoring token login by setting client fields: "
                f"user_id={_redacted_text(self.config.user_id)} "
                f"device_id={_redacted_text(self.config.device_id)} "
                f"token_present={bool(self.config.access_token)}"
            )
            self.client.user_id = self.config.user_id
            self.client.device_id = self.config.device_id
            self.client.access_token = self.config.access_token
        else:
            self.log(
                "performing password login: "
                f"username={_redacted_text(client_user)} "
                f"password_present={bool(self.config.password)}"
            )
            response = await self.client.login(
                password=self.config.password,
                device_name="MeeChat",
            )
            self.log(f"password login response class={response.__class__.__name__}")
            if response.__class__.__name__ != "LoginResponse":
                self.log(f"password login failed: response={response}")
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
            self.log("password login succeeded: " + self.config_summary())

        self.client.add_event_callback(self.on_room_message, RoomMessageText)
        self.log("room message event callback registered")
        if RoomTopicEvent is not None:
            self.client.add_event_callback(self.on_room_topic, RoomTopicEvent)
            self.log("room topic event callback registered")
        else:
            self.log("room topic event callback unavailable in this nio version")
        if ReactionEvent is not None:
            self.client.add_event_callback(self.on_reaction, ReactionEvent)
            self.log("reaction event callback registered")
        else:
            self.log("reaction event callback unavailable in this nio version")
        add_ephemeral_callback = getattr(self.client, "add_ephemeral_callback", None)
        if add_ephemeral_callback is not None:
            add_ephemeral_callback(self.on_receipt, ReceiptEvent)
            self.log("receipt ephemeral callback registered")
        else:
            self.log("receipt ephemeral callback unavailable in this nio version")
        self.sync_task = asyncio.create_task(self.sync_loop())
        self.log(f"sync task created: task={self.sync_task!r}")

    async def disconnect(self) -> None:
        self.log(
            "disconnect starting: "
            f"sync_task_present={self.sync_task is not None} "
            f"client_present={self.client is not None} connected={self.connected}"
        )
        current_task = asyncio.current_task()
        if self.sync_task is not None and self.sync_task is not current_task:
            self.log("cancelling sync task")
            self.sync_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.sync_task
            self.sync_task = None
            self.log("sync task cancelled")

        await self.close_client()

        await self.emit({"type": "disconnected"})
        self.log("disconnect emitted disconnected")

    async def close_client(self) -> None:
        if self.client is not None:
            self.log("closing Matrix client")
            close = getattr(self.client, "close", None)
            if close is not None:
                await close()
                self.log("Matrix client close() completed")
            else:
                self.log("Matrix client has no close() method")
            self.client = None
        else:
            self.log("close_client skipped; no Matrix client")
        self.connected = False
        self.log("client state cleared")

    async def sync_loop(self) -> None:
        assert self.client is not None
        assert self.config is not None

        first_sync = True
        backoff = 1
        attempt = 0
        self.log(
            "sync loop starting: "
            f"timeout_ms={self.config.sync_timeout_ms} "
            f"backlog_limit={self.config.backlog_limit}"
        )

        try:
            while True:
                try:
                    attempt += 1
                    self.log(
                        "sync attempt starting: "
                        f"attempt={attempt} first_sync={first_sync} "
                        f"backoff={backoff}"
                    )
                    response = await self.client.sync(
                        timeout=self.config.sync_timeout_ms,
                        sync_filter=_sync_filter(self.config.backlog_limit),
                        full_state=first_sync,
                    )
                    self.log(
                        "sync response received: "
                        f"attempt={attempt} class={response.__class__.__name__}"
                    )
                    if response.__class__.__name__ == "SyncResponse":
                        if first_sync:
                            self.connected = True
                            self.log(
                                "first sync succeeded; emitting connected "
                                f"user_id={_redacted_text(self.config.user_id)}"
                            )
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
                        self.log(
                            "sync success emitted: "
                            f"first={first_sync} "
                            f"next_batch_present={bool(getattr(self.client, 'next_batch', ''))}"
                        )
                        await self.emit_rooms()
                        first_sync = False
                        backoff = 1
                        continue

                    if _is_unknown_token_response(response):
                        self.log("sync failed with M_UNKNOWN_TOKEN")
                        await self.emit_error(
                            _unknown_token_message(response),
                            where="connect",
                        )
                        await self.close_client()
                        await self.emit({"type": "disconnected"})
                        return

                    self.log(f"sync returned non-success response: {response}")
                    await self.emit_error(str(response), where="sync")
                except asyncio.CancelledError:
                    self.log("sync loop cancelled")
                    raise
                except Exception as exc:
                    self.log(f"sync loop exception: {exc}")
                    await self.emit_error(str(exc), where="sync")
                    if self.debug:
                        traceback.print_exc(file=self.stderr)

                self.log(f"sync sleeping before retry: seconds={backoff}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
        finally:
            if self.sync_task is asyncio.current_task():
                self.sync_task = None
            self.log("sync loop exiting")

    async def on_room_message(self, room: Any, event: Any) -> None:
        room_id = getattr(room, "room_id", "")
        sender = getattr(event, "sender", "")
        body = getattr(event, "body", "")
        if not room_id or not body:
            self.log(
                "room message skipped: "
                f"room_id_present={bool(room_id)} body_present={bool(body)}"
            )
            return

        self.log(
            "room message event emitting: "
            f"room_id={_redacted_text(room_id)} "
            f"sender={_redacted_text(sender)} body_len={len(str(body))} "
            f"event_id={_redacted_text(getattr(event, 'event_id', ''))}"
        )
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

    async def on_room_topic(self, room: Any, event: Any) -> None:
        room_id = getattr(room, "room_id", "")
        if not room_id:
            self.log("room topic event skipped; room_id empty")
            return

        topic = str(getattr(event, "topic", "") or _room_topic(room))
        self.log(
            "room topic event emitting: "
            f"room_id={_redacted_text(room_id)} topic_len={len(topic)}"
        )
        await self.emit(
            {
                "type": "room_topic",
                "room_id": room_id,
                "topic": topic,
            }
        )

    async def on_receipt(self, room: Any, event: Any) -> None:
        room_id = getattr(room, "room_id", "")
        if not room_id:
            self.log("receipt event skipped; room_id empty")
            return

        receipts = []
        skipped = 0
        for receipt in getattr(event, "receipts", []) or []:
            if _receipt_type_value(getattr(receipt, "receipt_type", "")) != "m.read":
                skipped += 1
                continue

            event_id = str(getattr(receipt, "event_id", "") or "")
            user_id = str(getattr(receipt, "user_id", "") or "")
            if not event_id or not user_id:
                skipped += 1
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
            self.log(
                "receipt event emitting: "
                f"room_id={_redacted_text(room_id)} receipts={len(receipts)} "
                f"skipped={skipped}"
            )
            await self.emit(
                {
                    "type": "read_receipts",
                    "room_id": room_id,
                    "receipts": receipts,
                }
            )
        else:
            self.log(
                "receipt event produced no public read receipts: "
                f"room_id={_redacted_text(room_id)} skipped={skipped}"
            )

    async def on_reaction(self, room: Any, event: Any) -> None:
        room_id = getattr(room, "room_id", "")
        target_event_id = str(getattr(event, "reacts_to", "") or "")
        key = str(getattr(event, "key", "") or "")
        sender = str(getattr(event, "sender", "") or "")
        if not room_id or not target_event_id or not key or not sender:
            self.log(
                "reaction event skipped: "
                f"room_id_present={bool(room_id)} "
                f"target_present={bool(target_event_id)} key_present={bool(key)} "
                f"sender_present={bool(sender)}"
            )
            return

        self.log(
            "reaction event emitting: "
            f"room_id={_redacted_text(room_id)} "
            f"target_event_id={_redacted_text(target_event_id)} "
            f"event_id={_redacted_text(getattr(event, 'event_id', ''))} "
            f"sender={_redacted_text(sender)} key={key!r}"
        )
        await self.emit(
            {
                "type": "reaction",
                "room_id": room_id,
                "event_id": getattr(event, "event_id", ""),
                "target_event_id": target_event_id,
                "key": key,
                "sender": sender,
                "sender_display_name": _sender_display_name(room, sender),
                "server_timestamp": getattr(event, "server_timestamp", 0),
            }
        )

    async def send_message(self, room_id: str, body: str) -> None:
        self.log(
            "send_message starting: "
            f"room_id={_redacted_text(room_id)} body_len={len(body)} "
            f"client_present={self.client is not None}"
        )
        if self.client is None:
            self.log("send_message rejected; not connected")
            await self.emit_error("not connected", where="send_message")
            return

        if not room_id.strip():
            self.log("send_message rejected; room_id empty")
            await self.emit_error("room_id is required", where="send_message")
            return

        if not body:
            self.log("send_message ignored; body empty")
            return

        self.log("calling Matrix room_send")
        response = await self.client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": body},
        )
        event_id = getattr(response, "event_id", "")
        if event_id:
            self.log(
                "send_message succeeded: "
                f"room_id={_redacted_text(room_id)} event_id={_redacted_text(event_id)}"
            )
            await self.emit(
                {"type": "send_ok", "room_id": room_id, "event_id": event_id}
            )
            return

        self.log(f"send_message failed: response={response}")
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
        self.log(
            "message_action starting: "
            f"action={action} room_id={_redacted_text(room_id)} "
            f"event_id={_redacted_text(event_id)} body_len={len(body)} "
            f"reason_len={len(reason)} client_present={self.client is not None}"
        )
        if self.client is None:
            self.log("message_action rejected; not connected")
            await self.emit_error("not connected", where="message_action")
            return

        if not room_id.strip() or not event_id.strip():
            self.log("message_action rejected; room_id or event_id empty")
            await self.emit_error(
                "room_id and event_id are required",
                where="message_action",
                room_id=room_id,
            )
            return

        if action == "remove":
            self.log(
                "calling Matrix room_redact: "
                f"room_id={_redacted_text(room_id)} "
                f"event_id={_redacted_text(event_id)} "
                f"reason_present={bool(reason.strip())}"
            )
            response = await self.client.room_redact(
                room_id=room_id,
                event_id=event_id,
                reason=reason.strip() or None,
            )
            await self.emit_action_response(action, room_id, response)
            return

        message_type, content = _message_action_event(action, event_id, body)
        if content is None:
            self.log(f"message_action rejected; unsupported or incomplete action={action}")
            await self.emit_error(
                f"unsupported or incomplete message action: {action}",
                where="message_action",
                room_id=room_id,
            )
            return

        self.log(
            "calling Matrix room_send for message action: "
            f"action={action} message_type={message_type} "
            f"room_id={_redacted_text(room_id)} "
            f"event_id={_redacted_text(event_id)}"
        )
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
            self.log(
                "message_action succeeded: "
                f"action={action} room_id={_redacted_text(room_id)} "
                f"event_id={_redacted_text(event_id)}"
            )
            await self.emit(
                {
                    "type": "action_ok",
                    "action": action,
                    "room_id": room_id,
                    "event_id": event_id,
                }
            )
            return

        self.log(f"message_action failed: action={action} response={response}")
        await self.emit_error(str(response), where="message_action", room_id=room_id)

    async def join(self, room: str) -> None:
        self.log(
            "join starting: "
            f"room={_redacted_text(room)} client_present={self.client is not None}"
        )
        if self.client is None:
            self.log("join rejected; not connected")
            await self.emit_error("not connected", where="join")
            return

        if not room.strip():
            self.log("join rejected; room empty")
            await self.emit_error("room alias or id is required", where="join")
            return

        self.log(f"calling Matrix join: room={_redacted_text(room)}")
        response = await self.client.join(room.strip())
        room_id = getattr(response, "room_id", "")
        if room_id:
            self.log(
                "join succeeded: "
                f"room={_redacted_text(room)} room_id={_redacted_text(room_id)}"
            )
            await self.emit({"type": "joined", "room_id": room_id, "room": room})
            return

        self.log(f"join failed: response={response}")
        await self.emit_error(str(response), where="join")

    async def leave(self, room_id: str) -> None:
        self.log(
            "leave starting: "
            f"room_id={_redacted_text(room_id)} client_present={self.client is not None}"
        )
        if self.client is None:
            self.log("leave rejected; not connected")
            await self.emit_error("not connected", where="leave")
            return

        if not room_id.strip():
            self.log("leave rejected; room_id empty")
            await self.emit_error("room_id is required", where="leave")
            return

        self.log(f"calling Matrix room_leave: room_id={_redacted_text(room_id)}")
        response = await self.client.room_leave(room_id.strip())
        if response.__class__.__name__ == "RoomLeaveResponse":
            self.log(f"leave succeeded: room_id={_redacted_text(room_id)}")
            await self.emit({"type": "left", "room_id": room_id})
            return

        self.log(f"leave failed: response={response}")
        await self.emit_error(str(response), where="leave", room_id=room_id)

    async def emit_rooms(self) -> None:
        self.log(f"emit_rooms starting: client_present={self.client is not None}")
        if self.client is None:
            self.log("emit_rooms rejected; not connected")
            await self.emit_error("not connected", where="rooms")
            return

        rooms = getattr(self.client, "rooms", {}) or {}
        self.log(f"emit_rooms found rooms={len(rooms)}")
        for room_id, room in rooms.items():
            self.log(
                "emitting room: "
                f"room_id={_redacted_text(room_id)} name={_room_name(room)!r} "
                f"topic_len={len(_room_topic(room))} "
                f"members={len(getattr(room, 'users', {}) or {})} "
                f"encrypted={bool(getattr(room, 'encrypted', False))}"
            )
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
        self.log(
            "emitting error: "
            f"where={where} room_id={_redacted_text(room_id)} message={message}"
        )
        payload: dict[str, Any] = {"type": "error", "where": where, "message": message}
        if room_id:
            payload["room_id"] = room_id
        await self.emit(payload)

    async def emit(self, message: dict[str, Any]) -> None:
        self.log("emitting protocol message: " + _message_summary(message))
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

    print(
        "meechat-worker: async_main starting "
        f"debug={args.debug} argv={argv if argv is not None else sys.argv[1:]}",
        file=sys.stderr,
        flush=True,
    )
    worker = MatrixWorker(debug=args.debug)
    await worker.run()
    print("meechat-worker: async_main exiting rc=0", file=sys.stderr, flush=True)
    return 0


def main() -> int:
    try:
        print("meechat-worker: main entering asyncio.run", file=sys.stderr, flush=True)
        return asyncio.run(async_main())
    except KeyboardInterrupt:
        print("meechat-worker: KeyboardInterrupt", file=sys.stderr, flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
