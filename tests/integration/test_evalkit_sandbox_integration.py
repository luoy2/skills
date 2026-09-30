"""agent-eval sandbox, started for real: what a candidate inside it can and cannot touch.

Each test runs the platform's sandbox (bwrap on Linux, sandbox-exec on macOS) through the
kit's own `sandboxed` builder and is skipped where that sandbox cannot start. The deny
roots are temporary trees standing in for a home directory, so a failure here names the
guarantee that broke without touching the real home.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
KIT_PATH = REPO_ROOT / "skills" / "engineering" / "agent-eval" / "scripts" / "evalkit.py"
MARKER = "rotate-the-ledger-4417"


def _load():
    spec = importlib.util.spec_from_file_location("evalkit_sandbox_integration", KIT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _unavailable(sandbox):
    """Why this sandbox cannot start here, or None when it can."""
    if sandbox == "sandbox-exec":
        return None if os.path.exists("/usr/bin/sandbox-exec") else "no /usr/bin/sandbox-exec"
    kit = _load()
    if not sys.platform.startswith("linux") or not os.path.exists(kit.BWRAP):
        return f"no {kit.BWRAP}"
    proc = subprocess.run([kit.BWRAP, "--ro-bind", "/", "/", "/bin/true"], capture_output=True, text=True)
    return None if proc.returncode == 0 else f"bwrap cannot create a namespace here: {proc.stderr.strip()}"


@pytest.fixture(params=["bwrap", "sandbox-exec"])
def kit(request, monkeypatch):
    reason = _unavailable(request.param)
    if reason:
        pytest.skip(reason)
    module = _load()
    monkeypatch.setattr(module, "SANDBOX", request.param)
    monkeypatch.setattr(module, "ALLOW_READ", ())
    return module


@pytest.fixture
def bwrap_kit(monkeypatch):
    reason = _unavailable("bwrap")
    if reason:
        pytest.skip(reason)
    module = _load()
    monkeypatch.setattr(module, "SANDBOX", "bwrap")
    monkeypatch.setattr(module, "ALLOW_READ", ())
    return module


@pytest.fixture
def sbpl_kit(monkeypatch):
    reason = _unavailable("sandbox-exec")
    if reason:
        pytest.skip(reason)
    module = _load()
    monkeypatch.setattr(module, "SANDBOX", "sandbox-exec")
    monkeypatch.setattr(module, "ALLOW_READ", ())
    return module


@pytest.fixture
def tree(tmp_path):
    """users/me is a denied home with the owner's notes; the run root sits inside it, as in real runs."""
    base = Path(os.path.realpath(tmp_path))
    me = base / "users" / "me"
    root = me / "run"
    for sub in ("wt", "home", "tmp"):
        (root / sub).mkdir(parents=True)
    (root / "wt" / "AGENTS.md").write_text("# snapshot entry\n")
    (me / "notes.md").write_text(f"answer: {MARKER}\n")
    (me / "repo").mkdir()
    (me / "repo" / "AGENTS.md").write_text("# live checkout\n")
    return base, me, root


def _run(kit, root, *argv):
    return subprocess.run(kit.sandboxed(root, list(argv)), capture_output=True, text=True, timeout=60)


def _deny(kit, monkeypatch, root, *paths):
    monkeypatch.setattr(kit, "DENY_ROOTS", tuple(str(p) for p in paths))
    kit.prepare_sandbox(root)


def test_a_file_under_a_deny_root_is_unreadable(kit, tree, monkeypatch):
    base, me, root = tree
    _deny(kit, monkeypatch, root, base / "users")
    proc = _run(kit, root, "/bin/cat", str(me / "notes.md"))
    assert proc.returncode != 0 and MARKER not in proc.stdout
    grep = _run(kit, root, "/usr/bin/grep", "-rl", MARKER, str(base / "users"))
    assert MARKER not in grep.stdout and "notes.md" not in grep.stdout


def test_the_run_root_is_readable_and_writable(kit, tree, monkeypatch):
    base, me, root = tree
    _deny(kit, monkeypatch, root, base / "users")
    target = root / "wt" / "out.txt"
    proc = _run(kit, root, "/bin/sh", "-c", f"echo written > {target} && /bin/cat {root / 'wt' / 'AGENTS.md'}")
    assert proc.returncode == 0, proc.stderr
    assert "snapshot entry" in proc.stdout
    assert target.read_text() == "written\n", "a write inside the root reaches the host"


def test_a_write_under_a_deny_root_never_reaches_the_host(kit, tree, monkeypatch):
    base, me, root = tree
    _deny(kit, monkeypatch, root, base / "users")
    _run(kit, root, "/bin/sh", "-c", f"echo x > {me / 'escape.txt'}; echo x > {me / 'notes.md'}")
    assert not (me / "escape.txt").exists()
    assert (me / "notes.md").read_text() == f"answer: {MARKER}\n"


def test_the_isolation_check_passes_a_clean_root_and_fails_a_planted_answer(kit, tree, monkeypatch):
    """The live checkout's instructions sit in the denied home; the probe must find them unreadable."""
    base, me, root = tree
    _deny(kit, monkeypatch, root, base / "users")
    monkeypatch.setattr(kit, "REPO", me / "repo")
    argv = kit.codex_argv("m", "high", root, root / "wt")
    assert kit.isolation_check(root, [MARKER], "prompt", {}, "codex", "gateway", argv=argv) == []
    (root / "home" / "notes.md").write_text(f"answer: {MARKER}\n")
    failures = kit.isolation_check(root, [MARKER], "prompt", {}, "codex", "gateway", argv=argv)
    assert any("leak markers" in f for f in failures), failures


def test_the_probe_fails_when_the_live_checkout_is_readable(kit, tree, monkeypatch):
    """The control: with nothing denied the same probe must fail, or it proves nothing."""
    base, me, root = tree
    _deny(kit, monkeypatch, root, base / "elsewhere")
    monkeypatch.setattr(kit, "REPO", me / "repo")
    failures = kit.isolation_check(root, [], "prompt", {}, "codex", "gateway",
                                   argv=kit.codex_argv("m", "high", root, root / "wt"))
    assert any("allowed reading" in f for f in failures), failures


def test_a_missing_test_interpreter_fails_the_probe(kit, tree, monkeypatch):
    base, me, root = tree
    _deny(kit, monkeypatch, root, base / "users")
    python = me / "venv" / "bin" / "python"
    monkeypatch.setattr(kit, "TEST_PYTHON", str(python))
    monkeypatch.setattr(kit, "ALLOW_READ", (str(me / "venv"),))
    kit.prepare_sandbox(root)
    assert kit.probe_test_python(root)


# --------------------------------------------------------------- bwrap only

def test_bwrap_makes_the_host_tree_read_only_outside_the_root(bwrap_kit, tree, monkeypatch):
    kit = bwrap_kit
    base, me, root = tree
    _deny(kit, monkeypatch, root, base / "users")
    proc = _run(kit, root, "/bin/sh", "-c", f"echo x > {base / 'outside.txt'}")
    assert proc.returncode != 0
    assert not (base / "outside.txt").exists()


def test_bwrap_binds_an_allowed_read_back_inside_a_deny_root(bwrap_kit, tree, monkeypatch):
    kit = bwrap_kit
    base, me, root = tree
    (me / "tool").mkdir()
    (me / "tool" / "version").write_text("1.0\n")
    monkeypatch.setattr(kit, "ALLOW_READ", (str(me / "tool"),))
    _deny(kit, monkeypatch, root, base / "users")
    assert _run(kit, root, "/bin/cat", str(me / "tool" / "version")).stdout == "1.0\n"
    assert _run(kit, root, "/bin/sh", "-c", f"echo x > {me / 'tool' / 'version'}").returncode != 0
    assert _run(kit, root, "/bin/cat", str(me / "notes.md")).returncode != 0


def test_bwrap_makes_a_denied_service_socket_unreachable(bwrap_kit, tree, monkeypatch):
    """A socket such as Docker's lives outside any home; a file deny root must close it."""
    kit = bwrap_kit
    base, me, root = tree
    if not os.path.exists("/usr/bin/python3"):
        pytest.skip("no /usr/bin/python3 to connect from inside the sandbox")
    path = base / "svc.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    connect = f"import socket; socket.socket(socket.AF_UNIX).connect({str(path)!r})"
    try:
        _deny(kit, monkeypatch, root, base / "elsewhere")
        assert _run(kit, root, "/usr/bin/python3", "-c", connect).returncode == 0, "control: reachable when not denied"
        _deny(kit, monkeypatch, root, path)
        assert _run(kit, root, "/usr/bin/python3", "-c", connect).returncode != 0
    finally:
        server.close()


def test_bwrap_default_deny_roots_hide_home_tmp_and_run_but_keep_the_resolver(bwrap_kit, monkeypatch):
    kit = bwrap_kit
    monkeypatch.setattr(kit, "DENY_ROOTS", kit.DEFAULT_DENY_ROOTS["bwrap"])
    root = Path(tempfile.mkdtemp(prefix="agent-eval-probe-", dir="/tmp"))
    other = Path(tempfile.mkdtemp(prefix="agent-eval-other-", dir="/tmp"))
    try:
        (other / "notes.md").write_text(f"answer: {MARKER}\n")
        assert _run(kit, root, "/bin/cat", str(other / "notes.md")).returncode != 0
        assert _run(kit, root, "/bin/sh", "-c", f"echo x > {root / 'x'}").returncode == 0
        listing = _run(kit, root, "/bin/ls", "-A", str(Path.home()))
        assert listing.stdout.strip() == "", "the real home must look empty"
        assert _run(kit, root, "/bin/cat", "/etc/resolv.conf").returncode == 0, "name resolution stays available"
        for sock in ("/run/docker.sock", "/var/run/docker.sock"):
            assert _run(kit, root, "/usr/bin/test", "-S", sock).returncode != 0
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(other, ignore_errors=True)


# -------------------------------------------------------- sandbox-exec only

LOOKUP = """import ctypes
lib = ctypes.CDLL("/usr/lib/libSystem.dylib")
port = ctypes.c_uint()
print(lib.bootstrap_look_up(ctypes.c_uint.in_dll(lib, "bootstrap_port"), b"com.apple.pasteboard.1", ctypes.byref(port)))
"""


def test_sbpl_closes_every_unix_socket_outside_the_deny_roots(sbpl_kit, tree, monkeypatch):
    """`allow default` let a candidate reach any local service socket, Docker's among them."""
    kit = sbpl_kit
    base, me, root = tree
    sockdir = Path(tempfile.mkdtemp(prefix="ev"))  # short: a unix socket path holds 104 bytes
    path = sockdir / "svc.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(8)  # room for the control and the probe, neither accepted
    connect = f"import socket; socket.socket(socket.AF_UNIX).connect({str(path)!r})"
    try:
        assert subprocess.run(["/usr/bin/python3", "-c", connect]).returncode == 0, "control: reachable unsandboxed"
        _deny(kit, monkeypatch, root, base / "users")
        assert "Operation not permitted" in _run(kit, root, "/usr/bin/python3", "-c", connect).stderr
    finally:
        server.close()
        shutil.rmtree(sockdir, ignore_errors=True)


def test_sbpl_refuses_mach_services_such_as_the_pasteboard(sbpl_kit, tree, monkeypatch):
    """A Mach service reached from inside answers outside it: the pasteboard, LaunchServices' `open`."""
    kit = sbpl_kit
    base, me, root = tree
    if subprocess.run(["/usr/bin/python3", "-c", LOOKUP], capture_output=True, text=True).stdout.strip() != "0":
        pytest.skip("no pasteboard service in this session")
    _deny(kit, monkeypatch, root, base / "users")
    proc = _run(kit, root, "/usr/bin/python3", "-c", LOOKUP)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() not in ("", "0")


def test_sbpl_keeps_name_resolution(sbpl_kit, tree, monkeypatch):
    kit = sbpl_kit
    base, me, root = tree
    resolve = "import socket; socket.getaddrinfo('example.com', 443)"
    if subprocess.run(["/usr/bin/python3", "-c", resolve], capture_output=True).returncode != 0:
        pytest.skip("no name resolution on this machine")
    _deny(kit, monkeypatch, root, base / "users")
    proc = _run(kit, root, "/usr/bin/python3", "-c", resolve)
    assert proc.returncode == 0, proc.stderr


def test_sbpl_hides_other_apps_preferences(sbpl_kit, tree, monkeypatch):
    """Codex needs the preferences service, which would otherwise answer for every app."""
    kit = sbpl_kit
    base, me, root = tree
    read = ["/usr/bin/defaults", "read", "com.apple.dock"]
    if subprocess.run(read, capture_output=True).returncode != 0:
        pytest.skip("no dock preferences in this session")
    # The plist files are denied as in real runs, so the preferences service is the only way left.
    _deny(kit, monkeypatch, root, base / "users", Path.home() / "Library" / "Preferences")
    assert _run(kit, root, *read).returncode != 0
