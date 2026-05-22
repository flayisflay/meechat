import meechat


class FakeWeechat:
    def __init__(self, options=None):
        self.options = options or {}
        self.process_calls = []
        self.printed = []
        self.printed_lines = []
        self.buffers = []
        self.buffer_properties = []

    def config_get_plugin(self, name):
        defaults = {"auth": "auto", "python": "python3", "debug": "off"}
        return self.options.get(name, defaults.get(name, ""))

    def string_eval_expression(self, value, _pointers, _extra_vars, _options):
        return value

    def prnt(self, buffer, message):
        self.printed.append((buffer, message))

    def info_get(self, name, _args):
        if name == "weechat_config_dir":
            return "/tmp/weechat"
        if name == "nick_color_name":
            return "red"
        return ""

    def color(self, name):
        return f"<{name}>"

    def buffer_new(self, name, input_cb, input_data, close_cb, close_data):
        self.buffers.append((name, input_cb, input_data, close_cb, close_data))
        return "buffer"

    def buffer_set(self, buffer, property_name, value):
        self.buffer_properties.append((buffer, property_name, value))

    def prnt_date_tags(self, buffer, timestamp, tags, message):
        self.printed_lines.append((buffer, timestamp, tags, message))

    def hook_process_hashtable(self, command, options, timeout, callback, data):
        self.process_calls.append((command, options, timeout, callback, data))
        return "worker-hook"


def test_start_worker_flushes_process_output_immediately(monkeypatch):
    fake = FakeWeechat()
    monkeypatch.setattr(meechat, "weechat", fake)
    monkeypatch.setattr(meechat, "WORKER_HOOK", "")

    assert meechat._start_worker()

    assert fake.process_calls
    _command, options, _timeout, _callback, _data = fake.process_calls[0]
    assert options["stdin"] == "1"
    assert options["buffer_flush"] == "1"


def test_config_message_password_auth_ignores_stale_token_requirement(monkeypatch):
    fake = FakeWeechat(
        {
            "auth": "password",
            "homeserver": "https://matrix.example",
            "username": "alice",
            "password": "secret",
            "access_token": "STALE_TOKEN",
        }
    )
    monkeypatch.setattr(meechat, "weechat", fake)

    message = meechat._config_message()

    assert message is not None
    assert message["auth"] == "password"
    assert message["username"] == "alice"
    assert message["password"] == "secret"
    assert message["access_token"] == "STALE_TOKEN"
    assert not fake.printed


def test_print_matrix_message_uses_colored_display_name(monkeypatch):
    fake = FakeWeechat()
    monkeypatch.setattr(meechat, "weechat", fake)
    monkeypatch.setattr(meechat, "ROOM_BUFFERS", {})
    monkeypatch.setattr(meechat, "BUFFER_ROOMS", {})
    monkeypatch.setattr(meechat, "ROOM_NAMES", {})

    meechat._print_matrix_message(
        {
            "room_id": "!room:example",
            "room_name": "General",
            "sender": "@alice:example",
            "sender_display_name": "Alice",
            "body": "hello",
            "server_timestamp": 123000,
        }
    )

    assert fake.printed_lines == [
        (
            "buffer",
            123,
            "notify_message,nick_@alice:example",
            "<red>Alice<reset>\thello",
        )
    ]
