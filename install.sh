#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<EOF
Usage: ./install.sh [--weechat-dir DIR] [--no-deps]

Installs MeeChat for WeeChat by:
  - installing Python dependencies into .venv
  - symlinking meechat.py into WeeChat's python/autoload directory

Options:
  --weechat-dir DIR  WeeChat data directory (contains python/autoload).
                     Defaults to WEECHAT_DIR, WEECHAT_HOME's data directory,
                     then XDG_DATA_HOME/weechat or ~/.local/share/weechat.
                     For legacy homes, use --weechat-dir ~/.weechat.
  --no-deps          Skip dependency installation; still verify the worker.
  -h, --help         Show this help.

PYTHON selects the Python 3.10+ interpreter for dependency installation.
Dependencies are installed into this checkout's .venv with uv or Python/pip.
EOF
}

die() {
    echo "install.sh: $*" >&2
    exit 1
}

expand_path() {
    case "$1" in
        "~")
            printf "%s\n" "$HOME"
            ;;
        "~/"*)
            printf "%s/%s\n" "$HOME" "${1#"~/"}"
            ;;
        *)
            printf "%s\n" "$1"
            ;;
    esac
}

resolve_python() {
    local python_path
    python_path="$(command -v "$1")" || die "Python executable not found: $1"
    case "$python_path" in
        /*) ;;
        *) python_path="$PWD/$python_path" ;;
    esac
    printf "%s\n" "$python_path"
}

check_python() {
    if ! "$1" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
        die "MeeChat requires a working Python 3.10 or newer: $1"
    fi
}

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
weechat_dir="${WEECHAT_DIR:-}"
install_deps=1

while (($#)); do
    case "$1" in
        --weechat-dir)
            if (($# < 2)) || [[ -z "$2" || "$2" == --* ]]; then
                die "--weechat-dir requires a directory"
            fi
            weechat_dir="$2"
            shift 2
            ;;
        --weechat-dir=*)
            weechat_dir="${1#*=}"
            [[ -n "$weechat_dir" ]] || die "--weechat-dir requires a directory"
            shift
            ;;
        --no-deps)
            install_deps=0
            shift
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            echo "install.sh: unknown option: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

if [[ -z "$weechat_dir" ]]; then
    if [[ -n "${WEECHAT_HOME:-}" ]]; then
        # With a split home (-d config:data:...), the second field is data.
        weechat_dir="$WEECHAT_HOME"
        if [[ "$weechat_dir" == *:* ]]; then
            weechat_dir="${weechat_dir#*:}"
            weechat_dir="${weechat_dir%%:*}"
            [[ -n "$weechat_dir" ]] || die "WEECHAT_HOME has no data directory; use --weechat-dir"
        fi
    elif [[ -n "${XDG_DATA_HOME:-}" ]]; then
        weechat_dir="$XDG_DATA_HOME/weechat"
    else
        weechat_dir="$HOME/.local/share/weechat"
    fi
fi

weechat_dir="$(expand_path "$weechat_dir")"
case "$weechat_dir" in
    /*) ;;
    *) weechat_dir="$PWD/$weechat_dir" ;;
esac

autoload_dir="$weechat_dir/python/autoload"
autoload_link="$autoload_dir/meechat.py"
venv_python="$script_dir/.venv/bin/python"

for source_file in meechat.py meechat_matrix/worker.py meechat_matrix/protocol.py; do
    [[ -f "$script_dir/$source_file" ]] || die "incomplete MeeChat checkout: missing $source_file"
done

if [[ -e "$autoload_link" && ! -L "$autoload_link" ]]; then
    die "$autoload_link already exists and is not a symlink; remove it or use --weechat-dir"
fi

python_bin=""
if [[ -n "${PYTHON:-}" ]]; then
    python_bin="$(resolve_python "$PYTHON")"
    check_python "$python_bin"
fi

if ((install_deps)); then
    if command -v uv >/dev/null 2>&1; then
        [[ -f "$script_dir/uv.lock" ]] || die "missing uv.lock in MeeChat checkout"
        echo "Installing Python dependencies with uv..."
        uv_options=(sync --locked --no-dev --project "$script_dir" --directory "$script_dir")
        if [[ -n "$python_bin" ]]; then
            uv_options+=(--python "$python_bin")
        fi
        UV_PROJECT_ENVIRONMENT="$script_dir/.venv" uv "${uv_options[@]}"
    else
        [[ -f "$script_dir/requirements.lock" ]] || die "missing requirements.lock in MeeChat checkout"
        if [[ -z "$python_bin" ]]; then
            python_bin="$(resolve_python python3)"
            check_python "$python_bin"
        fi
        echo "Installing Python dependencies with $python_bin..."
        if ! "$python_bin" -m venv "$script_dir/.venv"; then
            die "could not create .venv; install Python's venv/ensurepip support (python3-venv on Debian/Ubuntu), or install uv"
        fi
        "$venv_python" -m pip install --require-hashes -r "$script_dir/requirements.lock"
        "$venv_python" -m pip install --no-deps -e "$script_dir"
    fi
    [[ -x "$venv_python" ]] || die "dependency installation did not create $venv_python"
else
    echo "Skipping Python dependency installation."
fi

worker_python="${python_bin:-python3}"
if [[ -x "$venv_python" ]]; then
    worker_python="$venv_python"
fi
worker_python="$(resolve_python "$worker_python")"
check_python "$worker_python"

echo "Verifying Matrix dependencies and worker startup..."
if ! "$worker_python" - "$script_dir" <<'PY'
import json
import os
import subprocess
import sys

sys.path.insert(0, sys.argv[1])
from nio import AsyncClient, ReceiptEvent, RoomMessageText

try:
    result = subprocess.run(
        [sys.executable, os.path.join(sys.argv[1], "meechat_matrix", "worker.py")],
        input="", text=True, capture_output=True, timeout=10,
    )
except subprocess.TimeoutExpired:
    raise SystemExit("Matrix worker startup timed out")
if result.returncode or not result.stdout:
    sys.stderr.write(result.stderr)
    raise SystemExit("Matrix worker startup failed")
if json.loads(result.stdout.splitlines()[0]).get("type") != "ready":
    raise SystemExit("Matrix worker did not become ready")
PY
then
    die "worker verification failed with $worker_python; fix this environment or rerun without --no-deps"
fi

mkdir -p "$autoload_dir"
# Check again in case the destination changed during dependency installation.
if [[ -e "$autoload_link" && ! -L "$autoload_link" ]]; then
    die "$autoload_link already exists and is not a symlink"
fi
ln -sfn "$script_dir/meechat.py" "$autoload_link"

cat <<EOF

MeeChat installed.

WeeChat autoload link:
  $autoload_link -> $script_dir/meechat.py

Dependency check:
  matrix-nio imports and Matrix worker startup verified with $worker_python

In WeeChat, run:
  /plugin load python
  /set plugins.var.python.meechat.python "$worker_python"
  /python load $autoload_link

Then configure Matrix credentials as described in README.md.
EOF
