import asyncio
import io
import sys
from types import SimpleNamespace

import pytest
from nio.responses import SyncError

from meechat_matrix.protocol import ProtocolError, loads
from meechat_matrix.worker import MatrixWorker, WorkerConfig, _room_name, _sync_filter


def test_worker_config_from_message():
    config = WorkerConfig.from_message(
        {
            "type": "configure",
            "homeserver": " https://matrix.example ",
            "user_id": " @alice:example ",
            "access_token": " token ",
            "device_id": " DEVICE ",
            "store_path": " /tmp/store ",
            "sync_timeout_ms": "45000",
            "backlog_limit": "250",
        }
    )

    assert config.homeserver == "https://matrix.example"
    assert config.user_id == "@alice:example"
    assert config.access_token == "token"
    assert config.device_id == "DEVICE"
    assert config.auth == "auto"
    assert not config.use_password_login
    assert config.store_path == "/tmp/store"
    assert config.sync_timeout_ms == 45000
    assert config.backlog_limit == 250


def test_worker_config_requires_credentials():
    with pytest.raises(ProtocolError, match="homeserver"):
        WorkerConfig.from_message(
            {
                "type": "configure",
                "user_id": "@alice:example",
                "access_token": "token",
                "device_id": "DEVICE",
            }
        )


def test_worker_config_accepts_password_login_without_token_or_device_id():
    config = WorkerConfig.from_message(
        {
            "type": "configure",
            "homeserver": "https://matrix.example",
            "username": "alice",
            "password": "secret",
        }
    )

    assert config.homeserver == "https://matrix.example"
    assert config.username == "alice"
    assert config.password == "secret"
    assert config.auth == "auto"
    assert config.use_password_login
    assert config.user_id == ""
    assert config.access_token == ""
    assert config.device_id == ""


def test_worker_config_rejects_tiny_sync_timeout():
    with pytest.raises(ProtocolError, match="at least"):
        WorkerConfig.from_message(
            {
                "type": "configure",
                "homeserver": "https://matrix.example",
                "user_id": "@alice:example",
                "access_token": "token",
                "device_id": "DEVICE",
                "sync_timeout_ms": "10",
            }
        )


def test_worker_config_rejects_tiny_backlog_limit():
    with pytest.raises(ProtocolError, match="backlog_limit"):
        WorkerConfig.from_message(
            {
                "type": "configure",
                "homeserver": "https://matrix.example",
                "user_id": "@alice:example",
                "access_token": "token",
                "device_id": "DEVICE",
                "backlog_limit": "0",
            }
        )


def test_room_name_prefers_display_name():
    room = type(
        "Room",
        (),
        {"display_name": "General", "name": "Old", "room_id": "!id"},
    )()

    assert _room_name(room) == "General"


def test_sync_loop_reports_connected_after_successful_sync():
    async def run_test():
        class SyncResponse:
            pass

        class FakeClient:
            rooms = {}
            next_batch = "batch"
            sync_calls = []

            async def sync(self, *, timeout, sync_filter, full_state):
                self.sync_calls.append(
                    {
                        "timeout": timeout,
                        "sync_filter": sync_filter,
                        "full_state": full_state,
                    }
                )
                if full_state:
                    return SyncResponse()
                raise asyncio.CancelledError

        stdout = io.StringIO()
        worker = MatrixWorker(stdout=stdout)
        worker.config = WorkerConfig(
            auth="token",
            homeserver="https://matrix.example",
            user_id="@alice:example",
            access_token="token",
            device_id="DEVICE",
            backlog_limit=25,
        )
        client = FakeClient()
        worker.client = client

        with pytest.raises(asyncio.CancelledError):
            await worker.sync_loop()

        messages = [loads(line) for line in stdout.getvalue().splitlines()]
        assert messages[0] == {"type": "connected", "user_id": "@alice:example"}
        assert messages[1] == {"type": "sync", "first": True, "next_batch": "batch"}
        assert client.sync_calls[0] == {
            "timeout": 30000,
            "sync_filter": {"room": {"timeline": {"limit": 25}}},
            "full_state": True,
        }

    asyncio.run(run_test())


def test_connect_logs_in_with_password(monkeypatch):
    async def run_test():
        class FakeClientConfig:
            def __init__(self, *, store_sync_tokens):
                self.store_sync_tokens = store_sync_tokens

        class LoginResponse:
            user_id = "@alice:example"
            device_id = "SERVERDEVICE"
            access_token = "SERVER_TOKEN"

        class FakeClient:
            instances = []

            def __init__(self, homeserver, user, *, device_id, store_path, config):
                self.homeserver = homeserver
                self.user = user
                self.device_id = device_id
                self.store_path = store_path
                self.config = config
                self.user_id = ""
                self.access_token = ""
                self.login_calls = []
                self.callback_calls = []
                self.ephemeral_callback_calls = []
                FakeClient.instances.append(self)

            async def login(self, *, password, device_name):
                self.login_calls.append((password, device_name))
                self.user_id = LoginResponse.user_id
                self.device_id = LoginResponse.device_id
                self.access_token = LoginResponse.access_token
                return LoginResponse()

            def add_event_callback(self, callback, event_type):
                self.callback_calls.append((callback, event_type))

            def add_ephemeral_callback(self, callback, event_type):
                self.ephemeral_callback_calls.append((callback, event_type))

        fake_nio = SimpleNamespace(
            AsyncClient=FakeClient,
            AsyncClientConfig=FakeClientConfig,
            ReceiptEvent=object,
            RoomMessageText=object,
        )
        monkeypatch.setitem(sys.modules, "nio", fake_nio)

        worker = MatrixWorker(stdout=io.StringIO())
        worker.config = WorkerConfig(
            auth="auto",
            homeserver="https://matrix.example",
            user_id="",
            username="alice",
            password="secret",
            access_token="STALE_TOKEN",
            device_id="OLDDEVICE",
        )

        async def fake_sync_loop():
            return None

        worker.sync_loop = fake_sync_loop  # type: ignore[method-assign]

        await worker.connect()
        assert worker.sync_task is not None
        await worker.sync_task

        client = FakeClient.instances[0]
        assert client.homeserver == "https://matrix.example"
        assert client.user == "alice"
        assert not client.config.store_sync_tokens
        assert client.login_calls == [("secret", "MeeChat")]
        assert client.callback_calls
        assert client.ephemeral_callback_calls
        assert worker.config.user_id == "@alice:example"
        assert worker.config.device_id == "SERVERDEVICE"
        assert worker.config.access_token == "SERVER_TOKEN"

    asyncio.run(run_test())


def test_on_room_message_emits_sender_display_name():
    async def run_test():
        user = SimpleNamespace(display_name="Alice")
        room = SimpleNamespace(
            room_id="!room:example",
            display_name="General",
            topic="A focused room topic",
            users={"@alice:example": user},
        )
        event = SimpleNamespace(
            sender="@alice:example",
            body="hello",
            event_id="$event",
            server_timestamp=123000,
        )
        stdout = io.StringIO()
        worker = MatrixWorker(stdout=stdout)

        await worker.on_room_message(room, event)

        message = loads(stdout.getvalue())
        assert message["sender"] == "@alice:example"
        assert message["sender_display_name"] == "Alice"
        assert message["room_topic"] == "A focused room topic"

    asyncio.run(run_test())


def test_on_receipt_emits_display_names_for_public_read_receipts():
    async def run_test():
        users = {
            "@alice:example": SimpleNamespace(display_name="Alice"),
            "@bob:example": SimpleNamespace(display_name="Bob"),
        }
        room = SimpleNamespace(
            room_id="!room:example",
            display_name="General",
            users=users,
        )
        event = SimpleNamespace(
            receipts=[
                SimpleNamespace(
                    event_id="$event",
                    receipt_type="m.read",
                    user_id="@alice:example",
                    timestamp=123000,
                ),
                SimpleNamespace(
                    event_id="$event",
                    receipt_type="m.read.private",
                    user_id="@bob:example",
                    timestamp=124000,
                ),
            ]
        )
        stdout = io.StringIO()
        worker = MatrixWorker(stdout=stdout)

        await worker.on_receipt(room, event)

        message = loads(stdout.getvalue())
        assert message == {
            "type": "read_receipts",
            "room_id": "!room:example",
            "receipts": [
                {
                    "event_id": "$event",
                    "user_id": "@alice:example",
                    "display_name": "Alice",
                    "timestamp": 123000,
                }
            ],
        }

    asyncio.run(run_test())


def test_sync_loop_stops_on_unknown_token():
    async def run_test():
        class FakeClient:
            rooms = {}
            next_batch = ""
            closed = False

            async def sync(self, *, timeout, sync_filter, full_state):
                return SyncError("Invalid access token passed.", "M_UNKNOWN_TOKEN")

            async def close(self):
                self.closed = True

        stdout = io.StringIO()
        client = FakeClient()
        worker = MatrixWorker(stdout=stdout)
        worker.config = WorkerConfig(
            auth="token",
            homeserver="https://matrix.example",
            user_id="@alice:example",
            access_token="token",
            device_id="DEVICE",
        )
        worker.client = client

        await worker.sync_loop()

        messages = [loads(line) for line in stdout.getvalue().splitlines()]
        assert messages[0]["type"] == "error"
        assert messages[0]["where"] == "connect"
        assert "access_token" in messages[0]["message"]
        assert messages[1] == {"type": "disconnected"}
        assert worker.client is None
        assert not worker.connected
        assert client.closed

    asyncio.run(run_test())


def test_sync_filter_sets_room_timeline_limit():
    assert _sync_filter(75) == {"room": {"timeline": {"limit": 75}}}
