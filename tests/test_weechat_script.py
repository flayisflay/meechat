import meechat


class FakeWeechat:
    def __init__(self, options=None):
        self.options = options or {}
        self.process_calls = []
        self.printed = []

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
        return ""

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
