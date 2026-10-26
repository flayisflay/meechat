# MeeChat

Small Matrix adapter for WeeChat.

## Install

Requirements: macOS or Linux, Bash 3.2 or newer, and WeeChat with its Python
plugin. Install either `uv` (which can find or download a suitable Python) or
Python 3.10+ with `venv` and `ensurepip` support. On Debian/Ubuntu, the
`python3-venv` package provides that support. Dependency downloads need network
access; platforms without compatible binary wheels may also need build tools.

Keep this checkout in a permanent, user-writable location, then run:

```sh
./install.sh
```

The installer creates `.venv`, installs locked dependencies, verifies Matrix
imports and worker startup, then links `meechat.py` into WeeChat autoload. Copy
the printed WeeChat commands to load it immediately. The `/python load` command
intentionally has no quotes around its filename, including paths with spaces.

The autoload link and Python environment depend on this checkout staying in
place. After moving it, recreate `.venv` and rerun the installer. Reinstall on
each machine; do not copy `.venv` between machines.

The destination is WeeChat's **data directory**, which contains
`python/autoload`. It is selected in this order:

1. `--weechat-dir DIR`.
2. `WEECHAT_DIR`, an installer-specific override.
3. `WEECHAT_HOME` (the data field for a colon-separated configuration).
4. `$XDG_DATA_HOME/weechat`, or `~/.local/share/weechat` by default.

For legacy installations, use `--weechat-dir ~/.weechat` or set `WEECHAT_HOME`.
The mere presence of `~/.weechat` does not change the default: modern WeeChat can
use XDG directories even when that old directory remains. If WeeChat uses a
custom `-d` argument, pass its data directory explicitly. Inside WeeChat, print
the active path with:

```text
/eval /print ${info:weechat_data_dir,}
```

Useful options:

```sh
./install.sh --weechat-dir ~/.weechat
./install.sh --weechat-dir /path/to/weechat-data
PYTHON=/path/to/python3.12 ./install.sh
./install.sh --no-deps
```

`PYTHON` selects the interpreter for both uv and pip installations. Dependencies
always go into this checkout's `.venv`, even if `UV_PROJECT_ENVIRONMENT` is set.
`--no-deps` skips installation but still requires a working Python 3.10+ worker
with Matrix dependencies. It uses `.venv` if available, otherwise `PYTHON` or
`python3`. Verification failures return a nonzero exit status before linking.
Existing regular files at the destination are preserved.

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
uv run --locked --extra dev pytest
```

Installer tests use disposable checkouts and home directories. They also test
autoload and the printed commands in real WeeChat when `weechat-headless` is
installed. To include fresh uv and pip dependency installations:

```sh
MEECHAT_TEST_INSTALL=1 uv run --locked --extra dev pytest
```

CI runs the suite, including fresh installs, on macOS and Linux with Python
3.10 and 3.14. After updating `uv.lock`, regenerate the pip runtime lock:

```sh
uv export --locked --no-dev --no-emit-project --no-header --no-annotate \
  --output-file requirements.lock
```

The tests check that the exported lock matches `uv.lock`.
