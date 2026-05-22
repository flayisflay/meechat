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
        self.line_data = {}
        self.line_updates = []
        self.last_line_by_lines = {}
        self.line_data_by_line = {}
        self.buffer_inputs = {}
        self.hook_sets = []

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
        if property_name == "input":
            self.buffer_inputs[buffer] = value

    def buffer_get_string(self, buffer, property_name):
        if property_name == "input":
            return self.buffer_inputs.get(buffer, "")
        return ""

    def command(self, buffer, command):
        self.commands.append((buffer, command))

    def prnt_date_tags(self, buffer, timestamp, tags, message):
        self.printed_lines.append((buffer, timestamp, tags, message))
        line_number = len(self.printed_lines)
        line = f"line-{line_number}"
        line_data = f"line-data-{line_number}"
        prefix, _separator, body = message.partition("\t")
        self.line_data[line_data] = {"prefix": prefix, "message": body}
        self.line_data_by_line[line] = line_data
        self.last_line_by_lines[f"lines:{buffer}"] = line

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

    def hdata_get(self, name):
        return name

    def hdata_pointer(self, hdata, pointer, name):
        if hdata == "buffer" and name in {"own_lines", "lines"}:
            return f"lines:{pointer}"
        if hdata == "lines" and name == "last_line":
            return self.last_line_by_lines.get(pointer, "")
        if hdata == "line" and name == "data":
            return self.line_data_by_line.get(pointer, "")
        return ""

    def hdata_update(self, hdata, pointer, values):
        if hdata != "line_data" or pointer not in self.line_data:
            return 0
        self.line_updates.append((pointer, values))
        self.line_data[pointer].update(values)
        return len(values)

    def hook_process_hashtable(self, command, options, timeout, callback, data):
        self.process_calls.append((command, options, timeout, callback, data))
        return "worker-hook"

    def hook_set(self, hook, property_name, value):
        self.hook_sets.append((hook, property_name, value))


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
    monkeypatch.setattr(meechat, "ROOM_TOPICS", {})

    meechat._print_matrix_message(
        {
            "room_id": "!room:example",
            "room_name": "General",
            "room_topic": "Only the topic",
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
    assert (
        "buffer",
        "title",
        "Only the topic",
    ) in fake.buffer_properties
    assert (
        "buffer",
        "title",
        "Matrix: General (!room:example)",
    ) not in fake.buffer_properties


def test_inspect_monitor_steps_through_room_messages(monkeypatch):
    fake = FakeWeechat()
    monkeypatch.setattr(meechat, "weechat", fake)
    monkeypatch.setattr(meechat, "ROOM_BUFFERS", {})
    monkeypatch.setattr(meechat, "BUFFER_ROOMS", {})
    monkeypatch.setattr(meechat, "ROOM_NAMES", {})
    monkeypatch.setattr(meechat, "ROOM_TOPICS", {})
    monkeypatch.setattr(meechat, "ROOM_MESSAGES", {})
    monkeypatch.setattr(meechat, "ROOM_INSPECT_INDEX", {})
    monkeypatch.setattr(meechat, "ROOM_READ_RECEIPTS", {})
    monkeypatch.setattr(meechat, "ROOM_HIGHLIGHTED_INDEX", {})
    monkeypatch.setattr(meechat, "INSPECT_ACTION_STATE", {})
    monkeypatch.setattr(meechat, "INSPECT_BAR_ITEM", "")
    monkeypatch.setattr(meechat, "INSPECT_BAR", "")
    monkeypatch.setattr(meechat, "TOPIC_BAR", "")

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
    assert fake.bars[0][2] == "100"
    assert fake.bars[0][5] == "top"
    assert fake.bars[1][0] == "meechat_topic"
    assert fake.bars[1][2] == "99"
    assert fake.bars[1][5] == "top"
    assert fake.bars[1][12] == "darkblue"
    assert fake.bars[1][15] == "buffer_title"
    assert "meechat_inspect" in fake.bar_item_updates

    rendered = meechat.inspect_bar_item_cb("", "", "", "buffer", {})
    assert "room:" not in rendered
    assert "message: 2/2" in rendered
    assert "sender: Bob" in rendered
    assert "event: $two" in rendered
    assert "body: second" in rendered
    assert "read receipts:" in rendered

    meechat.command_cb("", "buffer", "inspect-prev")

    rendered = meechat.inspect_bar_item_cb("", "", "", "buffer", {})
    assert "message: 1/2" in rendered
    assert "sender: Alice" in rendered
    assert "event: $one" in rendered
    assert "body: first" in rendered


def test_inspect_monitor_shows_colored_read_receipts(monkeypatch):
    fake = FakeWeechat()
    monkeypatch.setattr(meechat, "weechat", fake)
    monkeypatch.setattr(meechat, "ROOM_BUFFERS", {})
    monkeypatch.setattr(meechat, "BUFFER_ROOMS", {})
    monkeypatch.setattr(meechat, "ROOM_NAMES", {})
    monkeypatch.setattr(meechat, "ROOM_TOPICS", {})
    monkeypatch.setattr(meechat, "ROOM_MESSAGES", {})
    monkeypatch.setattr(meechat, "ROOM_INSPECT_INDEX", {})
    monkeypatch.setattr(meechat, "ROOM_READ_RECEIPTS", {})
    monkeypatch.setattr(meechat, "ROOM_HIGHLIGHTED_INDEX", {})
    monkeypatch.setattr(meechat, "INSPECT_ACTION_STATE", {})
    monkeypatch.setattr(meechat, "INSPECT_BAR_ITEM", "")
    monkeypatch.setattr(meechat, "INSPECT_BAR", "")
    monkeypatch.setattr(meechat, "TOPIC_BAR", "")

    for event_id, body in (("$one", "first"), ("$two", "second")):
        meechat._print_matrix_message(
            {
                "room_id": "!room:example",
                "room_name": "General",
                "sender": "@alice:example",
                "sender_display_name": "Alice",
                "body": body,
                "event_id": event_id,
                "server_timestamp": 123000,
            }
        )

    meechat._record_read_receipts(
        {
            "room_id": "!room:example",
            "receipts": [
                {
                    "event_id": "$two",
                    "user_id": "@beatrice:example",
                    "display_name": "Beatrice",
                    "timestamp": 124000,
                },
                {
                    "event_id": "$two",
                    "user_id": "@charlie:example",
                    "display_name": "Charlie",
                    "timestamp": 125000,
                },
            ],
        }
    )

    meechat.command_cb("", "buffer", "inspect")

    rendered = meechat.inspect_bar_item_cb("", "", "", "buffer", {})
    assert "read receipts: <red>Bea<reset> <red>Cha<reset>" in rendered

    meechat.command_cb("", "buffer", "inspect-prev")

    rendered = meechat.inspect_bar_item_cb("", "", "", "buffer", {})
    assert "read receipts: <red>Bea<reset> <red>Cha<reset>" in rendered


def test_inspect_navigation_marks_selected_room_line(monkeypatch):
    fake = FakeWeechat()
    monkeypatch.setattr(meechat, "weechat", fake)
    monkeypatch.setattr(meechat, "ROOM_BUFFERS", {})
    monkeypatch.setattr(meechat, "BUFFER_ROOMS", {})
    monkeypatch.setattr(meechat, "ROOM_NAMES", {})
    monkeypatch.setattr(meechat, "ROOM_TOPICS", {})
    monkeypatch.setattr(meechat, "ROOM_MESSAGES", {})
    monkeypatch.setattr(meechat, "ROOM_INSPECT_INDEX", {})
    monkeypatch.setattr(meechat, "ROOM_READ_RECEIPTS", {})
    monkeypatch.setattr(meechat, "ROOM_HIGHLIGHTED_INDEX", {})
    monkeypatch.setattr(meechat, "INSPECT_ACTION_STATE", {})
    monkeypatch.setattr(meechat, "INSPECT_BAR_ITEM", "")
    monkeypatch.setattr(meechat, "INSPECT_BAR", "")
    monkeypatch.setattr(meechat, "TOPIC_BAR", "")

    for event_id, sender, body in (
        ("$one", "Alice", "first"),
        ("$two", "Bob", "second"),
    ):
        meechat._print_matrix_message(
            {
                "room_id": "!room:example",
                "room_name": "General",
                "sender": f"@{sender.lower()}:example",
                "sender_display_name": sender,
                "body": body,
                "event_id": event_id,
                "server_timestamp": 123000,
            }
        )

    assert fake.line_updates == []

    meechat.command_cb("", "buffer", "inspect")

    assert fake.line_data["line-data-2"]["prefix"].startswith("<reverse>>> ")
    assert fake.line_data["line-data-2"]["message"] == "<reverse>second<reset>"

    meechat.command_cb("", "buffer", "inspect-prev")

    assert fake.line_data["line-data-2"] == {
        "prefix": "<red>Bob<reset>",
        "message": "second",
    }
    assert fake.line_data["line-data-1"]["prefix"].startswith("<reverse>>> ")
    assert fake.line_data["line-data-1"]["message"] == "<reverse>first<reset>"


def test_inspect_enter_menu_completes_edit_action(monkeypatch):
    fake = FakeWeechat()
    monkeypatch.setattr(meechat, "weechat", fake)
    monkeypatch.setattr(meechat, "WORKER_HOOK", "worker-hook")
    monkeypatch.setattr(meechat, "ROOM_BUFFERS", {})
    monkeypatch.setattr(meechat, "BUFFER_ROOMS", {})
    monkeypatch.setattr(meechat, "ROOM_NAMES", {})
    monkeypatch.setattr(meechat, "ROOM_TOPICS", {})
    monkeypatch.setattr(meechat, "ROOM_MESSAGES", {})
    monkeypatch.setattr(meechat, "ROOM_INSPECT_INDEX", {})
    monkeypatch.setattr(meechat, "ROOM_READ_RECEIPTS", {})
    monkeypatch.setattr(meechat, "ROOM_HIGHLIGHTED_INDEX", {})
    monkeypatch.setattr(meechat, "INSPECT_ACTION_STATE", {})
    monkeypatch.setattr(meechat, "INSPECT_BAR_ITEM", "")
    monkeypatch.setattr(meechat, "INSPECT_BAR", "")
    monkeypatch.setattr(meechat, "TOPIC_BAR", "")

    for event_id, body in (("$one", "first"), ("$two", "second")):
        meechat._print_matrix_message(
            {
                "room_id": "!room:example",
                "room_name": "General",
                "sender": "@alice:example",
                "sender_display_name": "Alice",
                "body": body,
                "event_id": event_id,
                "server_timestamp": 123000,
            }
        )

    meechat.command_cb("", "buffer", "inspect")
    meechat.command_cb("", "buffer", "inspect-enter")

    rendered = meechat.inspect_bar_item_cb("", "", "", "buffer", {})
    assert "actions:" in rendered
    assert "[r]eply" in rendered
    assert (
        "buffer",
        "key_bind_e",
        "/meechat inspect-action e",
    ) in fake.buffer_properties

    meechat.command_cb("", "buffer", "inspect-action e")
    meechat.command_cb("", "buffer", "inspect-enter")

    rendered = meechat.inspect_bar_item_cb("", "", "", "buffer", {})
    assert "action: edit | input: replacement text" in rendered
    assert fake.buffer_inputs["buffer"] == "second"

    fake.buffer_inputs["buffer"] = "updated"
    meechat.command_cb("", "buffer", "inspect-enter")

    assert meechat.INSPECT_ACTION_STATE == {}
    assert (
        "buffer",
        "key_bind_e",
        "",
    ) in fake.buffer_properties
    _hook, property_name, payload = fake.hook_sets[-1]
    assert property_name == "stdin"
    assert meechat.loads(payload) == {
        "type": "message_action",
        "action": "edit",
        "room_id": "!room:example",
        "event_id": "$two",
        "body": "updated",
    }
    assert fake.buffer_inputs["buffer"] == ""


def test_inspect_action_menu_uses_arrow_selection(monkeypatch):
    fake = FakeWeechat()
    monkeypatch.setattr(meechat, "weechat", fake)
    monkeypatch.setattr(meechat, "ROOM_BUFFERS", {})
    monkeypatch.setattr(meechat, "BUFFER_ROOMS", {})
    monkeypatch.setattr(meechat, "ROOM_NAMES", {})
    monkeypatch.setattr(meechat, "ROOM_TOPICS", {})
    monkeypatch.setattr(meechat, "ROOM_MESSAGES", {})
    monkeypatch.setattr(meechat, "ROOM_INSPECT_INDEX", {})
    monkeypatch.setattr(meechat, "ROOM_READ_RECEIPTS", {})
    monkeypatch.setattr(meechat, "ROOM_HIGHLIGHTED_INDEX", {})
    monkeypatch.setattr(meechat, "INSPECT_ACTION_STATE", {})
    monkeypatch.setattr(meechat, "INSPECT_BAR_ITEM", "")
    monkeypatch.setattr(meechat, "INSPECT_BAR", "")
    monkeypatch.setattr(meechat, "TOPIC_BAR", "")

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
    meechat.command_cb("", "buffer", "inspect-enter")
    meechat.command_cb("", "buffer", "inspect-down")
    meechat.command_cb("", "buffer", "inspect-enter")

    assert meechat.INSPECT_ACTION_STATE["!room:example"]["action"] == "thread"
    assert "action: thread" in meechat.inspect_bar_item_cb("", "", "", "buffer", {})


def test_room_buffer_binds_inspect_navigation_and_creates_top_monitor(monkeypatch):
    fake = FakeWeechat()
    monkeypatch.setattr(meechat, "weechat", fake)
    monkeypatch.setattr(meechat, "ROOM_BUFFERS", {})
    monkeypatch.setattr(meechat, "BUFFER_ROOMS", {})
    monkeypatch.setattr(meechat, "ROOM_NAMES", {})
    monkeypatch.setattr(meechat, "ROOM_TOPICS", {})
    monkeypatch.setattr(meechat, "ROOM_MESSAGES", {})
    monkeypatch.setattr(meechat, "ROOM_INSPECT_INDEX", {})
    monkeypatch.setattr(meechat, "ROOM_READ_RECEIPTS", {})
    monkeypatch.setattr(meechat, "ROOM_HIGHLIGHTED_INDEX", {})
    monkeypatch.setattr(meechat, "INSPECT_ACTION_STATE", {})
    monkeypatch.setattr(meechat, "INSPECT_BAR_ITEM", "")
    monkeypatch.setattr(meechat, "INSPECT_BAR", "")
    monkeypatch.setattr(meechat, "TOPIC_BAR", "")

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
    assert (
        "buffer",
        "key_bind_up",
        "/meechat inspect-up",
    ) in fake.buffer_properties
    assert (
        "buffer",
        "key_bind_down",
        "/meechat inspect-down",
    ) in fake.buffer_properties
    assert (
        "buffer",
        "key_bind_ctrl-M",
        "/meechat inspect-enter",
    ) in fake.buffer_properties
    assert (
        "buffer",
        "key_bind_ctrl-G",
        "/meechat inspect-cancel",
    ) in fake.buffer_properties
    assert fake.bars[0][0] == "meechat_inspect"
    assert fake.bars[0][5] == "top"
    assert fake.bars[1][0] == "meechat_topic"
    assert fake.bars[1][5] == "top"
    assert fake.bars[1][12] == "darkblue"
    assert fake.bars[1][15] == "buffer_title"
