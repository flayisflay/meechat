#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<EOF
Usage: ./install.sh [--weechat-dir DIR] [--no-deps]

Installs MeeChat for WeeChat by:
  - installing Python dependencies into .venv
  - symlinking meechat.py into WeeChat's python/autoload directory

Options:
  --weechat-dir DIR  WeeChat config directory. Defaults to WEECHAT_DIR,
                     ~/.config/weechat if present, ~/.weechat if present,
                     otherwise ~/.config/weechat.
  --no-deps          Skip dependency installation.
  -h, --help         Show this help.
EOF
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

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
weechat_dir="${WEECHAT_DIR:-}"
install_deps=1

while (($#)); do
    case "$1" in
        --weechat-dir)
            if (($# < 2)); then
                echo "install.sh: --weechat-dir requires a value" >&2
                exit 1
            fi
            weechat_dir="$2"
            shift 2
            ;;
        --weechat-dir=*)
            weechat_dir="${1#*=}"
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
    if [[ -d "$HOME/.config/weechat" ]]; then
        weechat_dir="$HOME/.config/weechat"
    elif [[ -d "$HOME/.weechat" ]]; then
        weechat_dir="$HOME/.weechat"
    else
        weechat_dir="$HOME/.config/weechat"
    fi
fi

weechat_dir="$(expand_path "$weechat_dir")"
mkdir -p "$weechat_dir"
weechat_dir="$(CDPATH= cd -- "$weechat_dir" && pwd -P)"

autoload_dir="$weechat_dir/python/autoload"
autoload_link="$autoload_dir/meechat.py"
venv_python="$script_dir/.venv/bin/python"

if ((install_deps)); then
    if command -v uv >/dev/null 2>&1; then
        echo "Installing Python dependencies with uv..."
        (cd "$script_dir" && uv sync)
    else
        python_bin="${PYTHON:-python3}"
        if ! command -v "$python_bin" >/dev/null 2>&1; then
            echo "install.sh: uv is not installed and $python_bin was not found" >&2
            exit 1
        fi
        echo "Installing Python dependencies with $python_bin..."
        "$python_bin" -m venv "$script_dir/.venv"
        "$venv_python" -m pip install -e "$script_dir"
    fi
else
    echo "Skipping Python dependency installation."
fi

mkdir -p "$autoload_dir"
if [[ -e "$autoload_link" && ! -L "$autoload_link" ]]; then
    echo "install.sh: $autoload_link already exists and is not a symlink" >&2
    echo "Remove it or choose another WeeChat directory with --weechat-dir." >&2
    exit 1
fi

ln -sfn "$script_dir/meechat.py" "$autoload_link"

worker_python="${PYTHON:-python3}"
if [[ -x "$venv_python" ]]; then
    worker_python="$venv_python"
fi

if "$worker_python" -c "import nio" >/dev/null 2>&1; then
    dependency_status="matrix-nio import verified with $worker_python"
else
    dependency_status="WARNING: $worker_python cannot import matrix-nio"
fi

cat <<EOF

MeeChat installed.

WeeChat autoload link:
  $autoload_link -> $script_dir/meechat.py

Dependency check:
  $dependency_status

In WeeChat, run:
  /plugin load python
  /set plugins.var.python.meechat.python "$worker_python"
  /python load "$autoload_link"

Then configure Matrix credentials as described in README.md.
EOF
