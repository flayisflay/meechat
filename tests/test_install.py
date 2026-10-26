"""Exercise the installer in disposable checkouts and WeeChat homes."""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]
UV = shutil.which("uv")
WEECHAT = shutil.which("weechat-headless")


class Installer:
    def __init__(self, root):
        self.root = root
        self.checkout = root / "checkout with spaces"
        self.checkout.mkdir()
        for name in ["install.sh", "meechat.py", "pyproject.toml", "README.md", "uv.lock", "requirements.lock"]:
            shutil.copy2(ROOT / name, self.checkout / name)
        shutil.copytree(ROOT / "meechat_matrix", self.checkout / "meechat_matrix",
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.bin = root / "bin"
        self.bin.mkdir()
        self.log = root / "commands.jsonl"
        self.helper = root / "command.py"
        self.helper.write_text(textwrap.dedent("""\
            import json
            import os
            from pathlib import Path
            import shlex
            import sys

            tool, args = sys.argv[1], sys.argv[2:]
            with open(os.environ["INSTALL_TEST_LOG"], "a") as log:
                log.write(json.dumps({"tool": tool, "args": args,
                                      "environment": os.environ.get("UV_PROJECT_ENVIRONMENT")}) + "\\n")

            def create_venv(directory):
                executable = Path(directory) / "bin/python"
                executable.parent.mkdir(parents=True, exist_ok=True)
                executable.write_text("#!/bin/sh\\nexec " + shlex.quote(sys.executable) + " "
                                      + shlex.quote(__file__) + ' python "$@"\\n')
                executable.chmod(0o755)

            if tool == "uv":
                if os.environ.get("INSTALL_TEST_FAIL_UV"):
                    sys.exit(19)
                create_venv(os.environ["UV_PROJECT_ENVIRONMENT"])
            elif args[:2] == ["-m", "venv"]:
                if os.environ.get("INSTALL_TEST_FAIL_VENV"):
                    sys.exit(1)
                create_venv(args[2])
            elif args[:2] == ["-m", "pip"]:
                if os.environ.get("INSTALL_TEST_FAIL_PIP"):
                    sys.exit(17)
            elif args[:1] == ["-c"] and os.environ.get("INSTALL_TEST_OLD_PYTHON"):
                sys.exit(1)
            else:
                os.execv(sys.executable, [sys.executable, *args])
            """))
        self.add_tool("python3", "python")
        for name in ["bash", "cat", "dirname", "mkdir", "ln"]:
            (self.bin / name).symlink_to(shutil.which(name))
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("UV_", "XDG_", "PYTHON", "WEECHAT", "INSTALL_TEST_"))
                    and key != "VIRTUAL_ENV"}
        self.env.update(HOME=str(root / "home"), PATH=str(self.bin),
                        PYTHONDONTWRITEBYTECODE="1", INSTALL_TEST_LOG=str(self.log),
                        UV_CACHE_DIR=str(root / "uv-cache"))

    def add_tool(self, name, tool):
        executable = self.bin / name
        executable.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " "
                              + shlex.quote(str(self.helper)) + " " + tool + ' "$@"\n')
        executable.chmod(0o755)

    @property
    def link(self):
        return self.root / "home/.local/share/weechat/python/autoload/meechat.py"

    @property
    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def run(self, *args, timeout=20):
        return subprocess.run([str(self.checkout / "install.sh"), *args], cwd=self.root,
                              env=self.env, text=True, capture_output=True, timeout=timeout)

    def assert_link(self, directory=None):
        link = self.link if directory is None else directory / "python/autoload/meechat.py"
        assert link.is_symlink()
        assert link.resolve() == (self.checkout / "meechat.py").resolve()


@pytest.fixture
def installer(tmp_path):
    return Installer(tmp_path)


@pytest.mark.parametrize("environment,existing,expected", [
    ({}, [], "home/.local/share/weechat"),
    ({}, ["home/.weechat"], "home/.local/share/weechat"),
    ({}, ["home/.weechat", "home/.config/weechat"], "home/.local/share/weechat"),
    ({}, ["home/.weechat", "home/.local/share/weechat"], "home/.local/share/weechat"),
    ({"XDG_DATA_HOME": "custom data"}, ["home/.weechat"], "custom data/weechat"),
    ({"XDG_CONFIG_HOME": "custom config"}, ["home/.weechat"], "home/.local/share/weechat"),
    ({"WEECHAT_HOME": "single home", "XDG_DATA_HOME": "data"}, [], "single home"),
    ({"WEECHAT_DIR": "override", "WEECHAT_HOME": "single home"}, [], "override"),
])
def test_data_directory_selection(installer, environment, existing, expected):
    installer.env.update({key: str(installer.root / value) for key, value in environment.items()})
    for directory in existing:
        (installer.root / directory).mkdir(parents=True)
    result = installer.run("--no-deps")
    assert result.returncode == 0, result.stderr
    installer.assert_link(installer.root / expected)


@pytest.mark.parametrize("count", [4, 5])
def test_split_weechat_home_uses_data_field(installer, count):
    directories = [str(installer.root / str(index)) for index in range(count)]
    installer.env["WEECHAT_HOME"] = ":".join(directories)
    result = installer.run("--no-deps")
    assert result.returncode == 0, result.stderr
    installer.assert_link(Path(directories[1]))


@pytest.mark.parametrize("directory", ["~/data with spaces", "relative data", "~/.weechat"])
def test_explicit_directory_overrides_environment(installer, directory):
    installer.env["WEECHAT_DIR"] = str(installer.root / "wrong")
    result = installer.run("--no-deps", "--weechat-dir", directory)
    assert result.returncode == 0, result.stderr
    expected = installer.root / ("home/" + directory[2:] if directory.startswith("~/") else directory)
    installer.assert_link(expected)


def test_directory_equals_option(installer):
    result = installer.run("--no-deps", "--weechat-dir=~/legacy home")
    assert result.returncode == 0, result.stderr
    installer.assert_link(installer.root / "home/legacy home")


@pytest.mark.parametrize("args", [
    ["--weechat-dir"], ["--weechat-dir", ""], ["--weechat-dir="],
    ["--weechat-dir", "--no-deps"], ["--unknown"],
])
def test_invalid_options_fail_before_installing(installer, args):
    result = installer.run(*args)
    assert result.returncode != 0
    assert not installer.calls
    assert not installer.link.is_symlink()


def test_uv_uses_checkout_environment_and_explicit_python(installer):
    installer.add_tool("uv", "uv")
    installer.env.update(PYTHON="bin/python3", UV_PROJECT_ENVIRONMENT=str(installer.root / "other"))
    result = installer.run()
    assert result.returncode == 0, result.stderr
    sync = next(call for call in installer.calls if call["tool"] == "uv")
    assert sync["environment"] == str(installer.checkout / ".venv")
    assert "--locked" in sync["args"]
    assert "--no-dev" in sync["args"]
    assert sync["args"][sync["args"].index("--python") + 1] == str(installer.bin / "python3")
    assert not (installer.root / "other").exists()
    installer.assert_link()


def test_pip_installs_hashed_lock_then_project_without_resolving(installer):
    result = installer.run()
    assert result.returncode == 0, result.stderr
    pip = [call["args"] for call in installer.calls if call["args"][:2] == ["-m", "pip"]]
    assert pip == [
        ["-m", "pip", "install", "--require-hashes", "-r", str(installer.checkout / "requirements.lock")],
        ["-m", "pip", "install", "--no-deps", "-e", str(installer.checkout)],
    ]
    installer.assert_link()


@pytest.mark.parametrize("failure", ["UV", "PIP", "VENV"])
def test_dependency_install_failure_does_not_link(installer, failure):
    if failure == "UV":
        installer.add_tool("uv", "uv")
    installer.env["INSTALL_TEST_FAIL_" + failure] = "1"
    result = installer.run()
    assert result.returncode != 0
    assert "MeeChat installed." not in result.stdout
    assert not installer.link.is_symlink()
    if failure == "VENV":
        assert "venv/ensurepip" in result.stderr


@pytest.mark.parametrize("args", [[], ["--no-deps"]])
def test_missing_dependency_fails_verification_before_linking(installer, args):
    (installer.checkout / "nio.py").write_text('raise ImportError("matrix-nio unavailable")\n')
    result = installer.run(*args)
    assert result.returncode != 0
    assert "worker verification failed" in result.stderr
    assert "MeeChat installed." not in result.stdout
    assert not installer.link.is_symlink()


@pytest.mark.parametrize("worker", ['raise RuntimeError("startup failed")', 'print("{}")'])
def test_worker_must_start_and_emit_ready(installer, worker):
    (installer.checkout / "meechat_matrix/worker.py").write_text(worker + "\n")
    result = installer.run("--no-deps")
    assert result.returncode != 0
    assert "worker verification failed" in result.stderr
    assert not installer.link.is_symlink()


def test_existing_file_is_preserved_without_installing_dependencies(installer):
    installer.link.parent.mkdir(parents=True)
    installer.link.write_text("existing script\n")
    result = installer.run()
    assert result.returncode != 0
    assert installer.link.read_text() == "existing script\n"
    assert not installer.calls


def test_repeat_install_and_broken_symlink_replacement(installer):
    installer.link.parent.mkdir(parents=True)
    installer.link.symlink_to(installer.root / "missing.py")
    for _ in range(2):
        result = installer.run("--no-deps")
        assert result.returncode == 0, result.stderr
        installer.assert_link()


def test_old_python_is_rejected_before_installing(installer):
    installer.add_tool("uv", "uv")
    installer.env.update(PYTHON="python3", INSTALL_TEST_OLD_PYTHON="1")
    result = installer.run()
    assert result.returncode != 0
    assert "Python 3.10 or newer" in result.stderr
    assert not any(call["tool"] == "uv" for call in installer.calls)
    assert not installer.link.is_symlink()


def test_missing_python_has_actionable_error(installer):
    installer.env["PYTHON"] = str(installer.root / "missing-python")
    result = installer.run()
    assert result.returncode != 0
    assert "Python executable not found" in result.stderr
    assert not installer.link.is_symlink()


@pytest.mark.skipif(UV is None, reason="uv is needed to check the exported lock")
def test_pip_lock_matches_uv_lock(tmp_path):
    result = subprocess.run([
        UV, "export", "--locked", "--offline", "--no-dev", "--no-emit-project",
        "--no-header", "--no-annotate", "--cache-dir", str(tmp_path / "cache"),
    ], cwd=ROOT, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout == (ROOT / "requirements.lock").read_text()


@pytest.mark.skipif(os.environ.get("MEECHAT_TEST_INSTALL") != "1",
                    reason="set MEECHAT_TEST_INSTALL=1 to download dependencies")
@pytest.mark.parametrize("backend", ["uv", "pip"])
def test_real_dependency_install(installer, backend):
    if backend == "uv":
        if UV is None:
            pytest.skip("uv is not installed")
        (installer.bin / "uv").symlink_to(UV)
    installer.env["PYTHON"] = sys.executable
    result = installer.run(timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    installer.assert_link()
    assert "worker startup verified" in result.stdout


@pytest.mark.skipif(WEECHAT is None, reason="weechat-headless is not installed")
@pytest.mark.parametrize("manual,legacy_present", [(False, False), (True, False), (False, True)],
                         ids=["autoload", "printed-commands", "legacy-present"])
def test_installed_script_loads_in_weechat(installer, manual, legacy_present):
    # Real Python dependencies come from the environment running pytest.
    if legacy_present:
        (installer.root / "home/.weechat").mkdir(parents=True)
    else:
        for kind in ["CONFIG", "DATA", "STATE", "CACHE", "RUNTIME"]:
            key = "XDG_RUNTIME_DIR" if kind == "RUNTIME" else "XDG_" + kind + "_HOME"
            installer.env[key] = str(installer.root / kind.lower())
    result = installer.run("--no-deps")
    assert result.returncode == 0, result.stderr
    report = installer.root / "weechat.json"
    inspect = textwrap.dedent(f"""\
        import json, pathlib
        messages = []
        items = weechat.infolist_get("buffer_lines", weechat.buffer_search("core", "weechat"), "")
        while weechat.infolist_next(items):
            messages.append(weechat.infolist_string(items, "message"))
        weechat.infolist_free(items)
        pathlib.Path({str(report)!r}).write_text(json.dumps({{
            "loaded": bool(weechat.config_get("plugins.var.python.meechat.python")),
            "messages": messages,
        }}))
        """)
    command = [WEECHAT, "-a", "-P", "python", "--stdout"]
    if manual:
        command.append("-s")
        for line in result.stdout.splitlines():
            if line.startswith("  /"):
                command.extend(["-r", line.strip()])
    for entry in ["/meechat rooms", "/wait 2 /python eval exec(" + repr(inspect) + ")", "/wait 3 /quit"]:
        command.extend(["-r", entry])
    run = subprocess.run(command, env=installer.env, text=True, capture_output=True, timeout=20)
    assert run.returncode == 0, run.stdout + run.stderr
    data = json.loads(report.read_text())
    assert data["loaded"], data["messages"]
    assert any("worker ready;" in message for message in data["messages"]), data["messages"]
