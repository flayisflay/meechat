# MeeChat

Small Python Matrix adapter for WeeChat.

## Install

Install Python dependencies:

```sh
uv sync
```

Or with pip:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

Install the WeeChat script for autoload on startup:

```sh
mkdir -p ~/.config/weechat/python/autoload
ln -sf /absolute/path/to/meechat/meechat.py ~/.config/weechat/python/autoload/meechat.py
```

Use `~/.weechat/python/autoload` instead if your WeeChat config directory is
`~/.weechat`:

```sh
mkdir -p ~/.weechat/python/autoload
ln -sf /absolute/path/to/meechat/meechat.py ~/.weechat/python/autoload/meechat.py
```

If using a virtualenv, point the worker at that Python:

```text
/set plugins.var.python.meechat.python "/absolute/path/to/meechat/.venv/bin/python"
```

## Load in WeeChat

To load MeeChat immediately in a running WeeChat session, run:

```text
/python load /absolute/path/to/meechat/meechat.py
```

If the Python plugin is not already loaded, load it first:

```text
/plugin load python
```

Confirm the script is loaded:

```text
/python list
```

After changing `meechat.py`, reload it from WeeChat:

```text
/python reload meechat
```

Confirm MeeChat is loaded and connected:

```text
/python list
/meechat status
```

## Configure

You can connect with an existing Matrix access token:

```text
/secure set meechat_access_token <access-token>
/set plugins.var.python.meechat.access_token "${sec.data.meechat_access_token}"
```

Set the required Matrix options:

```text
/set plugins.var.python.meechat.homeserver "https://matrix.example.org"
/set plugins.var.python.meechat.user_id "@alice:example.org"
/set plugins.var.python.meechat.device_id "DEVICEID"
```

Or connect with Matrix username/password login:

```text
/secure set meechat_password <password>
/set plugins.var.python.meechat.password "${sec.data.meechat_password}"
/set plugins.var.python.meechat.username "alice"
/set plugins.var.python.meechat.auth "password"
/set plugins.var.python.meechat.access_token ""
/set plugins.var.python.meechat.homeserver "https://matrix.example.org"
```

`username` can be a localpart, email address, or full Matrix user ID. If
`username` is empty, MeeChat uses `user_id` for password login. `device_id` is
optional for password login; setting it gives MeeChat a stable Matrix device ID.
`auth` can be `auto`, `token`, or `password`; `auto` uses password login when a
password is configured, otherwise it uses token auth.

MeeChat requests recent room history on the first sync after connecting. Tune
how much history is requested per room with:

```text
/set plugins.var.python.meechat.backlog_limit "100"
```

To connect automatically when WeeChat starts and autoloads MeeChat:

```text
/set plugins.var.python.meechat.autoconnect on
```

With `autoconnect` enabled, WeeChat loads `meechat.py` from `python/autoload` at
startup, then MeeChat runs `/meechat connect` automatically.

If `/meechat connect` reports `M_UNKNOWN_TOKEN` while `auth` is `token`, the
homeserver rejected the configured access token. If it happens while `auth` is
`password`, the homeserver rejected the token returned by the password login
request, which usually means the session was invalidated immediately or the
homeserver/login details do not match.

## Run

Connect:

```text
/meechat connect
```

Common commands:

```text
/meechat rooms
/meechat inspect
/meechat inspect-prev
/meechat inspect-next
/meechat inspect-enter
/meechat join #room:example.org
/meechat leave
/meechat disconnect
```

`Ctrl+G` toggles the top `meechat_inspect` monitor bar and message selector for
the current Matrix room. While the selector is active, `Up`/`Down` and
`Alt+Shift+Up`/`Alt+Shift+Down` inspect older and newer messages in that monitor.
Press `Enter` on an empty input line to open actions for the selected message:
reply, reply in thread, edit, add reaction, remove, and permalink. Select with
the action letter or arrow keys, press `Enter`, then complete the prompted input.
`Ctrl+G` again exits inspect mode and clears the message marker; `Esc` also exits
inspect mode.

MeeChat also adds a `meechat_topic` title bar below the monitor, populated only
from the current Matrix room topic and using a dark blue title-bar background.
The monitor includes a bottom read-receipts row using the
colored first four characters of each reader's display name. The message counter
appears beside the bottom enter prompt. While inspecting, the selected message is
temporarily marked in the room buffer.

Run tests:

```sh
uv run --extra dev pytest
```
