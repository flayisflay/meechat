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
    homeserver: str
    user_id: str
    access_token: str
    device_id: str
    store_path: str = ""
    sync_timeout_ms: int = 30000

    @classmethod
    def from_message(cls, message: dict[str, Any]) -> "WorkerConfig":
        missing = [
            key
            for key in ("homeserver", "user_id", "access_token", "device_id")
            if not str(message.get(key, "")).strip()
        ]
        if missing:
            raise ProtocolError("missing config field(s): " + ", ".join(missing))

        try:
            sync_timeout_ms = int(message.get("sync_timeout_ms") or 30000)
        except (TypeError, ValueError) as exc:
            raise ProtocolError("sync_timeout_ms must be an integer") from exc

        if sync_timeout_ms < 1000:
            raise ProtocolError("sync_timeout_ms must be at least 1000")

        return cls(
            homeserver=str(message["homeserver"]).strip(),
            user_id=str(message["user_id"]).strip(),
            access_token=str(message["access_token"]).strip(),
            device_id=str(message["device_id"]).strip(),
            store_path=str(message.get("store_path") or "").strip(),
            sync_timeout_ms=sync_timeout_ms,
        )


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
            await self.emit({"type": "connected", "user_id": self.config.user_id})
            return

        try:
            from nio import AsyncClient, RoomMessageText

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

        nio_config = ClientConfig(store_sync_tokens=True)
        self.client = AsyncClient(
            self.config.homeserver,
            self.config.user_id,
            device_id=self.config.device_id,
            store_path=self.config.store_path,
            config=nio_config,
        )

        if hasattr(self.client, "restore_login"):
            self.client.restore_login(
                self.config.user_id,
                self.config.device_id,
                self.config.access_token,
            )
        else:
            self.client.user_id = self.config.user_id
            self.client.device_id = self.config.device_id
            self.client.access_token = self.config.access_token

        self.client.add_event_callback(self.on_room_message, RoomMessageText)
        self.sync_task = asyncio.create_task(self.sync_loop())
        await self.emit({"type": "connected", "user_id": self.config.user_id})

    async def disconnect(self) -> None:
        if self.sync_task is not None:
            self.sync_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.sync_task
            self.sync_task = None

        if self.client is not None:
            close = getattr(self.client, "close", None)
            if close is not None:
                await close()
            self.client = None

        await self.emit({"type": "disconnected"})

    async def sync_loop(self) -> None:
        assert self.client is not None
        assert self.config is not None

        first_sync = True
        backoff = 1

        while True:
            try:
                response = await self.client.sync(
                    timeout=self.config.sync_timeout_ms,
                    full_state=first_sync,
                )
                if response.__class__.__name__ == "SyncResponse":
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

                await self.emit_error(str(response), where="sync")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.emit_error(str(exc), where="sync")
                if self.debug:
                    traceback.print_exc(file=self.stderr)

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

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
                "sender": sender,
                "body": body,
                "event_id": getattr(event, "event_id", ""),
                "server_timestamp": getattr(event, "server_timestamp", 0),
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
