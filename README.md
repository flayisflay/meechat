# MeeChat

Small Matrix adapter for WeeChat.

## Install

```sh
./install.sh
```

The installer creates `.venv`, installs dependencies, links `meechat.py` into
WeeChat autoload, and prints the WeeChat commands to load it now.

Useful options:

```sh
./install.sh --weechat-dir ~/.weechat
./install.sh --no-deps
```

## Configure

Token auth:

```text
/secure set meechat_access_token <access-token>
/set plugins.var.python.meechat.homeserver "https://matrix.example.org"
/set plugins.var.python.meechat.user_id "@alice:example.org"
/set plugins.var.python.meechat.access_token "${sec.data.meechat_access_token}"
```

Password auth:

```text
/secure set meechat_password <password>
/set plugins.var.python.meechat.homeserver "https://matrix.example.org"
/set plugins.var.python.meechat.username "alice"
/set plugins.var.python.meechat.password "${sec.data.meechat_password}"
/set plugins.var.python.meechat.auth password
```

Optional:

```text
/set plugins.var.python.meechat.autoconnect on
```

## Usage

```text
/meechat connect
/meechat rooms
/meechat join #room:example.org
/meechat leave
/meechat status
/meechat disconnect
```

Matrix rooms appear as WeeChat buffers; type in a room buffer to send messages.
MeeChat supports room topics, backlog, read receipts, reactions, replies,
thread replies, edits, redactions, and permalinks.

Hotkeys in Matrix room buffers:

- `Ctrl+G`: toggle message inspector.
- `Up`/`Down`: older/newer inspected message while inspector is active.
- `Alt+Shift+Up`/`Alt+Shift+Down`: older/newer inspected message.
- `Enter`: open/advance the inspector action menu.
- `r`, `t`, `e`, `a`, `v`, `p`: reply, thread reply, edit, react, remove,
  permalink.
- `Esc`: exit inspector.

For actions that prompt for input, type it in the room input line and press
`Enter`. For reactions, select `a`, press `Enter`, type the emoji or reaction
text, then press `Enter`. Shortcodes are not expanded: `:smiley:` is sent
literally.

## Tests

```sh
uv run --extra dev pytest
```
