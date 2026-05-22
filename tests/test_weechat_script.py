import pytest

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
        self.cleared_buffers = []
        self.bar_items = []
        self.bars = []
        self.existing_bars = {}
        self.bar_item_updates = []
        self.removed_bars = []
        self.removed_bar_items = []
        self.line_data = {}
        self.line_updates = []
        self.last_line_by_lines = {}
        self.line_data_by_line = {}
        self.line_prev_by_line = {}
        self.line_next_by_line = {}
        self.scroll_updates = []
        self.scroll_values = {"scroll": {}}
        self.buffer_strings = {}
        self.buffer_inputs = {}
        self.hook_sets = []
        self.infolist_items = []
        self.infolist_index = -1
        self.freed_infolists = []

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
        self.buffer_strings[pointer] = {
            "name": name,
            "full_name": "python." + name,
        }
        return pointer

    def buffer_set(self, buffer, property_name, value):
        self.buffer_properties.append((buffer, property_name, value))
        if property_name == "input":
            self.buffer_inputs[buffer] = value
        if property_name in {"short_name", "title"}:
            self.buffer_strings.setdefault(buffer, {})[property_name] = value
        if property_name.startswith("localvar_set_"):
            name = "localvar_" + property_name.removeprefix("localvar_set_")
            self.buffer_strings.setdefault(buffer, {})[name] = value

    def buffer_get_string(self, buffer, property_name):
        if property_name == "input":
            return self.buffer_inputs.get(buffer, "")
        return self.buffer_strings.get(buffer, {}).get(property_name, "")

    def command(self, buffer, command):
        self.commands.append((buffer, command))

    def buffer_clear(self, buffer):
        self.cleared_buffers.append(buffer)
        self.last_line_by_lines.pop(f"lines:{buffer}", None)

    def prnt_date_tags(self, buffer, timestamp, tags, message):
        self.printed_lines.append((buffer, timestamp, tags, message))
        line_number = len(self.printed_lines)
        line = f"line-{line_number}"
        line_data = f"line-data-{line_number}"
        prefix, _separator, body = message.partition("\t")
        self.line_data[line_data] = {"prefix": prefix, "message": body}
        self.line_data_by_line[line] = line_data
        previous_line = self.last_line_by_lines.get(f"lines:{buffer}", "")
        self.line_prev_by_line[line] = previous_line
        self.line_next_by_line[line] = ""
        if previous_line:
            self.line_next_by_line[previous_line] = line
        self.last_line_by_lines[f"lines:{buffer}"] = line

    def bar_item_new(self, name, callback, data):
        self.bar_items.append((name, callback, data))
        return "bar-item"

    def bar_new(self, *args):
        self.bars.append(args)
        return "bar"

    def bar_search(self, name):
        return self.existing_bars.get(name, "")

    def bar_item_update(self, name):
        self.bar_item_updates.append(name)

    def bar_remove(self, bar):
        self.removed_bars.append(bar)
        for name, pointer in list(self.existing_bars.items()):
            if pointer == bar:
                del self.existing_bars[name]

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
        if hdata == "line" and name == "prev_line":
            return self.line_prev_by_line.get(pointer, "")
        if hdata == "line" and name == "next_line":
            return self.line_next_by_line.get(pointer, "")
        if hdata == "window" and name == "scroll":
            return "scroll"
        if hdata == "window_scroll" and name == "start_line":
            return self.scroll_values.get(pointer, {}).get("start_line", "")
        return ""

    def hdata_update(self, hdata, pointer, values):
        if hdata == "window_scroll" and pointer == "scroll":
            self.scroll_updates.append((pointer, values))
            self.scroll_values.setdefault(pointer, {}).update(values)
            return len(values)
        if hdata != "line_data" or pointer not in self.line_data:
            return 0
        self.line_updates.append((pointer, values))
        self.line_data[pointer].update(values)
        return len(values)

    def window_search_with_buffer(self, buffer):
        return f"window:{buffer}"

    def window_get_integer(self, window, name):
        if window.startswith("window:") and name == "win_chat_height":
            return 5
        return 0

    def window_get_pointer(self, window, name):
        if window.startswith("window:") and name == "scroll":
            return "scroll"
        return ""

    def hook_process_hashtable(self, command, options, timeout, callback, data):
        self.process_calls.append((command, options, timeout, callback, data))
        return "worker-hook"

    def hook_set(self, hook, property_name, value):
        self.hook_sets.append((hook, property_name, value))

    def infolist_get(self, name, _pointer, _args):
        if name != "buffer":
            return ""
        self.infolist_index = -1
        return "infolist:buffer"

    def infolist_next(self, infolist):
        if infolist != "infolist:buffer":
            return False
        self.infolist_index += 1
        return self.infolist_index < len(self.infolist_items)

    def infolist_pointer(self, infolist, name):
        if infolist != "infolist:buffer" or name != "pointer":
            return ""
        return self.infolist_items[self.infolist_index]

    def infolist_free(self, infolist):
        self.freed_infolists.append(infolist)


@pytest.fixture
def fresh_meechat(monkeypatch):
    fake = FakeWeechat()
    monkeypatch.setattr(meechat, "weechat", fake)
    for name, value in (
        ("WORKER_HOOK", ""),
        ("WORKER_STDOUT", ""),
        ("ROOM_BUFFERS", {}),
        ("BUFFER_ROOMS", {}),
        ("ROOM_NAMES", {}),
        ("ROOM_TOPICS", {}),
        ("ROOM_MESSAGES", {}),
        ("ROOM_INSPECT_INDEX", {}),
        ("ROOM_READ_RECEIPTS", {}),
        ("ROOM_REACTIONS", {}),
        ("ROOM_BACKFILL_PENDING", set()),
        ("ROOM_BACKFILL_EXHAUSTED", set()),
        ("ROOM_BACKFILL_SELECT_PREVIOUS", set()),
        ("ROOM_HIGHLIGHTED_INDEX", {}),
        ("INSPECT_ACTION_STATE", {}),
        ("INSPECT_ACTIVE_ROOMS", set()),
        ("INSPECT_BAR_ITEM", ""),
        ("INSPECT_BAR", ""),
        ("TOPIC_BAR_ITEM", ""),
        ("TOPIC_BAR", ""),
    ):
        monkeypatch.setattr(meechat, name, value)
    return fake


def print_message(
    *,
    room_id="!room:example",
    room_name="General",
    sender="@alice:example",
    sender_display_name="Alice",
    body="hello",
    event_id="$event",
    server_timestamp=123000,
    **extra,
):
    message = {
        "room_id": room_id,
        "room_name": room_name,
        "sender": sender,
        "sender_display_name": sender_display_name,
        "body": body,
        "server_timestamp": server_timestamp,
        **extra,
    }
    if event_id is not None:
        message["event_id"] = event_id
    meechat._print_matrix_message(message)


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
    printed = "\n".join(message for _buffer, message in fake.printed)
    assert "config summary" in printed
    assert "resolved_auth=password" in printed
    assert "password_present=True" in printed
    assert "access_token_present=True" in printed
    assert "secret" not in printed
    assert "STALE_TOKEN" not in printed


def test_command_summary_redacts_secret_set_commands():
    assert meechat._command_summary("set password secret value") == (
        "set password <redacted>"
    )
    assert meechat._command_summary("/meechat set access_token SECRET") == (
        "/meechat set access_token <redacted>"
    )


def test_print_matrix_message_uses_colored_display_name(fresh_meechat):
    fake = fresh_meechat
    print_message(event_id=None, room_topic="Only the topic")

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
        "",
    ) in fake.buffer_properties
    assert (
        "buffer",
        "title",
        "Matrix: General (!room:example)",
    ) not in fake.buffer_properties
    meechat._ensure_topic_bar()
    assert meechat.topic_bar_item_cb("", "", "", "buffer", {}) == ""


def test_topic_bar_prefers_buffer_room_localvar(fresh_meechat):
    fake = fresh_meechat
    meechat.BUFFER_ROOMS["buffer"] = "!wrong:example"
    meechat.ROOM_TOPICS["!wrong:example"] = "Wrong topic"
    meechat.ROOM_TOPICS["!right:example"] = "Right topic"
    fake.buffer_strings["buffer"] = {"localvar_room_id": "!right:example"}

    assert meechat.topic_bar_item_cb("", "", "", "buffer", {}) == "Right topic"


def test_inspect_monitor_steps_through_room_messages(fresh_meechat):
    fake = fresh_meechat
    for event_id, sender, body, timestamp in (
        ("$one", "Alice", "first", 123000),
        ("$two", "Bob", "second", 124000),
    ):
        print_message(
            event_id=event_id,
            sender=f"@{sender.lower()}:example",
            sender_display_name=sender,
            body=body,
            server_timestamp=timestamp,
        )

    meechat.command_cb("", "buffer", "inspect")

    assert meechat.INSPECT_BAR_ITEM == "bar-item"
    assert meechat.INSPECT_BAR == "bar"
    assert fake.bar_items == [
        ("(extra)meechat_inspect", "inspect_bar_item_cb", ""),
        ("(extra)meechat_topic", "topic_bar_item_cb", ""),
    ]
    assert fake.bars[0][0] == "meechat_inspect"
    assert fake.bars[0][2] == "100"
    assert fake.bars[0][5] == "top"
    assert fake.bars[0][9] == "9"
    assert fake.bars[1][0] == "meechat_topic"
    assert fake.bars[1][2] == "99"
    assert fake.bars[1][5] == "top"
    assert fake.bars[1][12] == "darkblue"
    assert fake.bars[1][15] == "meechat_topic"
    assert "meechat_inspect" in fake.bar_item_updates

    rendered = meechat.inspect_bar_item_cb("", "", "", "buffer", {})
    assert "room:" not in rendered
    assert rendered.splitlines()[0].startswith("time: ")
    assert rendered.splitlines()[0].endswith(" | sender: Bob | user: @bob:example")
    assert "message: 2/2" in rendered
    assert "sender: Bob" in rendered
    assert "user: @bob:example" in rendered
    assert "event:" not in rendered
    assert "body: second" in rendered
    assert "read receipts:" in rendered
    assert "enter: select message" in rendered
    assert "\nactions:" not in rendered

    meechat.command_cb("", "buffer", "inspect-prev")

    rendered = meechat.inspect_bar_item_cb("", "", "", "buffer", {})
    assert "message: 1/2" in rendered
    assert "sender: Alice" in rendered
    assert "user: @alice:example" in rendered
    assert "event:" not in rendered
    assert "body: first" in rendered
    assert "enter: select message" in rendered


def test_inspect_monitor_shows_colored_read_receipts(fresh_meechat):
    fake = fresh_meechat
    for event_id, body in (("$one", "first"), ("$two", "second")):
        print_message(event_id=event_id, body=body)

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


def test_inspect_monitor_shows_reactions_with_counts_and_senders(fresh_meechat):
    for event_id, body in (("$one", "first"), ("$two", "second")):
        print_message(event_id=event_id, body=body)

    for event_id, key, sender, display_name, timestamp in (
        ("$react1", "👍", "@beatrice:example", "Beatrice", 124000),
        ("$react2", "👍", "@charlie:example", "Charlie", 125000),
        ("$react3", "❤️", "@dana:example", "Dana", 126000),
    ):
        meechat._record_reaction(
            {
                "room_id": "!room:example",
                "event_id": event_id,
                "target_event_id": "$two",
                "key": key,
                "sender": sender,
                "sender_display_name": display_name,
                "server_timestamp": timestamp,
            }
        )

    meechat.command_cb("", "buffer", "inspect")

    rendered = meechat.inspect_bar_item_cb("", "", "", "buffer", {})
    assert (
        "reactions: 👍 x2 by <red>Beatrice<reset>, <red>Charlie<reset> | "
        "❤️ x1 by <red>Dana<reset>"
    ) in rendered

    meechat.command_cb("", "buffer", "inspect-prev")

    rendered = meechat.inspect_bar_item_cb("", "", "", "buffer", {})
    assert "reactions: \n" in rendered


def test_inspect_navigation_moves_cursor_before_enter(fresh_meechat):
    fake = fresh_meechat
    for event_id, sender, body in (
        ("$one", "Alice", "first"),
        ("$two", "Bob", "second"),
    ):
        print_message(
            event_id=event_id,
            sender=f"@{sender.lower()}:example",
            sender_display_name=sender,
            body=body,
        )

    assert fake.line_updates == []

    meechat.command_cb("", "buffer", "inspect")

    assert fake.line_data["line-data-2"]["prefix"].startswith("<reverse>>> ")
    assert fake.line_data["line-data-2"]["message"] == "<reverse>second<reset>"
    rendered = meechat.inspect_bar_item_cb("", "", "", "buffer", {})
    assert "enter: select message" in rendered
    assert "\nactions:" not in rendered

    meechat.command_cb("", "buffer", "inspect-prev")

    assert fake.line_data["line-data-2"] == {
        "prefix": "<red>Bob<reset>",
        "message": "second",
    }
    assert fake.line_data["line-data-1"]["prefix"].startswith("<reverse>>> ")
    assert fake.line_data["line-data-1"]["message"] == "<reverse>first<reset>"
    assert fake.scroll_updates == []

    meechat.command_cb("", "buffer", "inspect-enter")

    assert fake.line_data["line-data-1"]["prefix"].startswith("<reverse>>> ")
    assert fake.line_data["line-data-1"]["message"] == "<reverse>first<reset>"
    rendered = meechat.inspect_bar_item_cb("", "", "", "buffer", {})
    assert "selected: message options" in rendered
    assert "actions:" in rendered

    meechat.command_cb("", "buffer", "inspect-cancel")

    assert fake.line_data["line-data-1"] == {
        "prefix": "<red>Alice<reset>",
        "message": "first",
    }
    rendered = meechat.inspect_bar_item_cb("", "", "", "buffer", {})
    assert rendered == ""


def test_inspect_navigation_scrolls_only_when_selection_leaves_view(fresh_meechat):
    fake = fresh_meechat
    for index in range(1, 7):
        print_message(body=f"message {index}", event_id=f"${index}")

    meechat.command_cb("", "buffer", "inspect")

    assert fake.scroll_updates == []

    for _ in range(4):
        meechat.command_cb("", "buffer", "inspect-prev")

    assert fake.scroll_updates == []

    meechat.command_cb("", "buffer", "inspect-prev")

    assert fake.scroll_updates[-1] == (
        "scroll",
        {"start_line": "line-1", "start_line_pos": "0", "scrolling": "1"},
    )

    for _ in range(4):
        meechat.command_cb("", "buffer", "inspect-next")

    assert len(fake.scroll_updates) == 1

    meechat.command_cb("", "buffer", "inspect-next")

    assert fake.scroll_updates[-1] == (
        "scroll",
        {"start_line": "line-2", "start_line_pos": "0", "scrolling": "1"},
    )
    assert ("buffer", "/window page_up") not in fake.commands
    assert ("buffer", "/window page_down") not in fake.commands


def test_inspect_navigation_requests_backfill_at_oldest_message(
    monkeypatch,
    fresh_meechat,
):
    fake = fresh_meechat
    monkeypatch.setattr(meechat, "WORKER_HOOK", "worker-hook")
    for event_id, body in (("$one", "first"), ("$two", "second")):
        print_message(event_id=event_id, body=body)

    meechat.command_cb("", "buffer", "inspect")
    meechat.command_cb("", "buffer", "inspect-prev")
    meechat.command_cb("", "buffer", "inspect-prev")
    meechat.command_cb("", "buffer", "inspect-prev")

    backfill_payloads = [
        meechat.loads(payload)
        for _hook, property_name, payload in fake.hook_sets
        if property_name == "stdin"
    ]
    assert backfill_payloads == [
        {"type": "backfill", "room_id": "!room:example"}
    ]
    assert meechat.ROOM_BACKFILL_PENDING == {"!room:example"}
    assert meechat.ROOM_BACKFILL_SELECT_PREVIOUS == {"!room:example"}


def test_backfill_prepends_history_and_selects_next_older_message(
    monkeypatch,
    fresh_meechat,
):
    fake = fresh_meechat
    monkeypatch.setattr(meechat, "WORKER_HOOK", "worker-hook")
    for event_id, body in (("$one", "first"), ("$two", "second")):
        print_message(event_id=event_id, body=body)

    meechat.command_cb("", "buffer", "inspect")
    meechat.command_cb("", "buffer", "inspect-prev")
    meechat.command_cb("", "buffer", "inspect-prev")

    meechat._record_backfill(
        {
            "type": "backfill",
            "room_id": "!room:example",
            "messages": [
                {
                    "room_id": "!room:example",
                    "room_name": "General",
                    "sender": "@bob:example",
                    "sender_display_name": "Bob",
                    "body": "older one",
                    "event_id": "$older1",
                    "server_timestamp": 121000,
                },
                {
                    "room_id": "!room:example",
                    "room_name": "General",
                    "sender": "@carol:example",
                    "sender_display_name": "Carol",
                    "body": "older two",
                    "event_id": "$older2",
                    "server_timestamp": 122000,
                },
            ],
            "reactions": [],
            "exhausted": False,
        }
    )

    assert [m["event_id"] for m in meechat.ROOM_MESSAGES["!room:example"]] == [
        "$older1",
        "$older2",
        "$one",
        "$two",
    ]
    assert meechat.ROOM_INSPECT_INDEX["!room:example"] == 1
    assert fake.cleared_buffers == ["buffer"]
    rendered = meechat.inspect_bar_item_cb("", "", "", "buffer", {})
    assert "message: 2/4" in rendered
    assert "sender: Carol" in rendered
    assert "body: older two" in rendered
    assert fake.line_data["line-data-4"]["message"] == "<reverse>older two<reset>"


def test_ctrl_g_toggles_monitor_and_selector(fresh_meechat):
    fake = fresh_meechat
    print_message()

    assert meechat.inspect_bar_item_cb("", "", "", "buffer", {}) == ""
    assert meechat.INSPECT_ACTIVE_ROOMS == set()

    meechat.command_cb("", "buffer", "inspect-toggle")

    assert meechat.INSPECT_ACTIVE_ROOMS == {"!room:example"}
    assert (
        "buffer",
        "localvar_set_meechat_inspect",
        "1",
    ) in fake.buffer_properties
    assert fake.line_data["line-data-1"]["prefix"].startswith("<reverse>>> ")
    assert "message: 1/1" in meechat.inspect_bar_item_cb("", "", "", "buffer", {})

    meechat.command_cb("", "buffer", "inspect-toggle")

    assert meechat.INSPECT_ACTIVE_ROOMS == set()
    assert fake.line_data["line-data-1"] == {
        "prefix": "<red>Alice<reset>",
        "message": "hello",
    }
    assert (
        "buffer",
        "localvar_set_meechat_inspect",
        "0",
    ) in fake.buffer_properties
    assert not [
        name
        for _buffer, name, _value in fake.buffer_properties
        if name.startswith("key_unbind_")
    ]
    assert meechat.inspect_bar_item_cb("", "", "", "buffer", {}) == ""


def test_inspect_keys_delegate_in_normal_mode(fresh_meechat):
    fake = fresh_meechat
    print_message()

    meechat.command_cb("", "buffer", "inspect-up")
    meechat.command_cb("", "buffer", "inspect-prev")
    meechat.command_cb("", "buffer", "inspect-enter")

    assert meechat.INSPECT_ACTIVE_ROOMS == set()
    assert fake.commands == [
        ("buffer", "/input history_previous"),
        ("buffer", "/input history_previous"),
        ("buffer", "/input return"),
    ]
    assert meechat.inspect_bar_item_cb("", "", "", "buffer", {}) == ""


def test_inspect_enter_menu_completes_edit_action(monkeypatch, fresh_meechat):
    fake = fresh_meechat
    monkeypatch.setattr(meechat, "WORKER_HOOK", "worker-hook")
    for event_id, body in (("$one", "first"), ("$two", "second")):
        print_message(event_id=event_id, body=body)

    meechat.command_cb("", "buffer", "inspect")
    meechat.command_cb("", "buffer", "inspect-enter")

    rendered = meechat.inspect_bar_item_cb("", "", "", "buffer", {})
    assert "selected: message options" in rendered
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


def test_inspect_action_menu_uses_arrow_selection(fresh_meechat):
    fake = fresh_meechat
    print_message()

    meechat.command_cb("", "buffer", "inspect")
    meechat.command_cb("", "buffer", "inspect-enter")
    scroll_count = len(fake.scroll_updates)
    meechat.command_cb("", "buffer", "inspect-down")
    meechat.command_cb("", "buffer", "inspect-enter")

    assert meechat.INSPECT_ACTION_STATE["!room:example"]["action"] == "thread"
    assert "action: thread" in meechat.inspect_bar_item_cb("", "", "", "buffer", {})
    assert len(fake.scroll_updates) == scroll_count


def test_room_buffer_binds_inspect_navigation_and_creates_top_monitor(fresh_meechat):
    fake = fresh_meechat
    print_message()
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
        "/meechat inspect-toggle",
    ) in fake.buffer_properties
    for key in meechat.INSPECT_CANCEL_KEYS:
        assert (
            "buffer",
            "key_bind_" + key,
            "/meechat inspect-cancel",
        ) in fake.buffer_properties
    assert fake.bars[0][0] == "meechat_inspect"
    assert fake.bars[0][5] == "top"
    assert fake.bars[1][0] == "meechat_topic"
    assert fake.bars[1][5] == "top"
    assert fake.bars[1][12] == "darkblue"
    assert fake.bars[1][15] == "meechat_topic"


def test_startup_rehydrates_existing_room_buffers(fresh_meechat):
    fake = fresh_meechat
    fake.infolist_items = ["buffer"]
    fake.buffer_strings["buffer"] = {
        "name": "meechat._room_example.org",
        "full_name": "python.meechat._room_example.org",
        "localvar_room_id": "!room:example.org",
        "short_name": "Test Room",
        "title": "Room topic",
    }

    meechat._rehydrate_room_buffers()

    assert meechat.ROOM_BUFFERS["!room:example.org"] == "buffer"
    assert meechat.BUFFER_ROOMS["buffer"] == "!room:example.org"
    assert meechat.ROOM_NAMES["!room:example.org"] == "Test Room"
    assert "!room:example.org" not in meechat.ROOM_TOPICS
    assert ("buffer", "title", "") in fake.buffer_properties
    assert ("buffer", "localvar_set_meechat", "1") in fake.buffer_properties
    assert ("buffer", "localvar_set_meechat_inspect", "0") in fake.buffer_properties
    assert (
        "buffer",
        "key_bind_ctrl-G",
        "/meechat inspect-toggle",
    ) in fake.buffer_properties
    for key in meechat.INSPECT_CANCEL_KEYS:
        assert (
            "buffer",
            "key_bind_" + key,
            "/meechat inspect-cancel",
        ) in fake.buffer_properties
    assert fake.freed_infolists == ["infolist:buffer"]


def test_room_topic_updates_current_topic_bar(fresh_meechat):
    fake = fresh_meechat
    meechat._handle_worker_message(
        {
            "type": "room",
            "room_id": "!room:example",
            "name": "General",
            "topic": "Old topic",
        }
    )
    meechat._ensure_topic_bar()

    assert meechat.topic_bar_item_cb("", "", "", "buffer", {}) == "Old topic"

    meechat._record_room_topic("!room:example", "Current topic")

    assert meechat.topic_bar_item_cb("", "", "", "buffer", {}) == "Current topic"
    assert ("buffer", "title", "") in fake.buffer_properties
    assert "meechat_topic" in fake.bar_item_updates


def test_topic_bar_replaces_stale_reload_bar(fresh_meechat):
    fake = fresh_meechat
    fake.existing_bars = {
        "meechat_inspect": "old-inspect-bar",
        "meechat_topic": "old-topic-bar",
    }

    meechat._ensure_inspect_bar()

    assert "old-inspect-bar" in fake.removed_bars
    assert "old-topic-bar" in fake.removed_bars
    assert fake.bars[0][0] == "meechat_inspect"
    assert fake.bars[1][0] == "meechat_topic"
