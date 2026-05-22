import pytest

from meechat_matrix.protocol import ProtocolError
from meechat_matrix.worker import WorkerConfig, _room_name


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
        }
    )

    assert config.homeserver == "https://matrix.example"
    assert config.user_id == "@alice:example"
    assert config.access_token == "token"
    assert config.device_id == "DEVICE"
    assert config.store_path == "/tmp/store"
    assert config.sync_timeout_ms == 45000


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


def test_room_name_prefers_display_name():
    room = type("Room", (), {"display_name": "General", "name": "Old", "room_id": "!id"})()

    assert _room_name(room) == "General"
