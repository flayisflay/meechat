import meechat


class FakeWeechat:
    WEECHAT_RC_OK = 0

    def __init__(self, options=None):
        self.options = options or {}
        self.process_calls = []
        self.printed = []
        self.printed_lines = []
        self.buffers = []
        self.buffer_properties = []
        self.commands = []
        self.bar_items = []
        self.bars = []
        self.bar_item_updates = []
        self.removed_bars = []
        self.removed_bar_items = []

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
        pointer = "buffer" if not self.buffers else f"buffer-{len(self.buffers) + 1}"
        self.buffers.append((pointer, name, input_cb, input_data, close_cb, close_data))
        return pointer

    def buffer_set(self, buffer, property_name, value):
        self.buffer_properties.append((buffer, property_name, value))

    def command(self, buffer, command):
        self.commands.append((buffer, command))

    def prnt_date_tags(self, buffer, timestamp, tags, message):
        self.printed_lines.append((buffer, timestamp, tags, message))

    def bar_item_new(self, name, callback, data):
        self.bar_items.append((name, callback, data))
        return "bar-item"

    def bar_new(self, *args):
        self.bars.append(args)
        return "bar"

    def bar_item_update(self, name):
        self.bar_item_updates.append(name)

    def bar_remove(self, bar):
        self.removed_bars.append(bar)

    def bar_item_remove(self, item):
        self.removed_bar_items.append(item)

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


def test_inspect_monitor_steps_through_room_messages(monkeypatch):
    fake = FakeWeechat()
    monkeypatch.setattr(meechat, "weechat", fake)
    monkeypatch.setattr(meechat, "ROOM_BUFFERS", {})
    monkeypatch.setattr(meechat, "BUFFER_ROOMS", {})
    monkeypatch.setattr(meechat, "ROOM_NAMES", {})
    monkeypatch.setattr(meechat, "ROOM_MESSAGES", {})
    monkeypatch.setattr(meechat, "ROOM_INSPECT_INDEX", {})
    monkeypatch.setattr(meechat, "INSPECT_BAR_ITEM", "")
    monkeypatch.setattr(meechat, "INSPECT_BAR", "")

    for event_id, sender, body, timestamp in (
        ("$one", "Alice", "first", 123000),
        ("$two", "Bob", "second", 124000),
    ):
        meechat._print_matrix_message(
            {
                "room_id": "!room:example",
                "room_name": "General",
                "sender": f"@{sender.lower()}:example",
                "sender_display_name": sender,
                "body": body,
                "event_id": event_id,
                "server_timestamp": timestamp,
            }
        )

    meechat.command_cb("", "buffer", "inspect")

    assert meechat.INSPECT_BAR_ITEM == "bar-item"
    assert meechat.INSPECT_BAR == "bar"
    assert fake.bar_items == [
        ("(extra)meechat_inspect", "inspect_bar_item_cb", ""),
    ]
    assert fake.bars[0][0] == "meechat_inspect"
    assert fake.bars[0][5] == "top"
    assert "meechat_inspect" in fake.bar_item_updates

    rendered = meechat.inspect_bar_item_cb("", "", "", "buffer", {})
    assert "room: General | message: 2/2" in rendered
    assert "sender: Bob" in rendered
    assert "event: $two" in rendered
    assert "body: second" in rendered

    meechat.command_cb("", "buffer", "inspect-prev")

    rendered = meechat.inspect_bar_item_cb("", "", "", "buffer", {})
    assert "message: 1/2" in rendered
    assert "sender: Alice" in rendered
    assert "event: $one" in rendered
    assert "body: first" in rendered


def test_room_buffer_binds_inspect_navigation_and_creates_top_monitor(monkeypatch):
    fake = FakeWeechat()
    monkeypatch.setattr(meechat, "weechat", fake)
    monkeypatch.setattr(meechat, "ROOM_BUFFERS", {})
    monkeypatch.setattr(meechat, "BUFFER_ROOMS", {})
    monkeypatch.setattr(meechat, "ROOM_NAMES", {})
    monkeypatch.setattr(meechat, "ROOM_MESSAGES", {})
    monkeypatch.setattr(meechat, "ROOM_INSPECT_INDEX", {})
    monkeypatch.setattr(meechat, "INSPECT_BAR_ITEM", "")
    monkeypatch.setattr(meechat, "INSPECT_BAR", "")

    meechat._print_matrix_message(
        {
            "room_id": "!room:example",
            "room_name": "General",
            "sender": "@alice:example",
            "sender_display_name": "Alice",
            "body": "hello",
            "event_id": "$event",
            "server_timestamp": 123000,
        }
    )
    meechat.command_cb("", "buffer", "inspect")

    assert (
        "buffer",
        "key_bind_meta-shift-up",
        "/meechat inspect-prev",
    ) in fake.buffer_properties
    assert (
        "buffer",
        "key_bind_meta-shift-down",
        "/meechat inspect-next",
    ) in fake.buffer_properties
    assert fake.bars[0][0] == "meechat_inspect"
    assert fake.bars[0][5] == "top"
