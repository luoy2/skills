"""agent-eval runner: which sandbox a machine uses, what its bwrap policy mounts, and host overrides.

A wrong mount order or a missed deny root fails silently: the candidate simply reads the
owner's notes and scores well. A host entry merged wrongly runs one machine with another's
paths. Tests that start bwrap or sandbox-exec are in tests/integration/test_evalkit_sandbox_integration.py.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
KIT_PATH = REPO_ROOT / "skills" / "engineering" / "agent-eval" / "scripts" / "evalkit.py"
MINIMAL = {"candidates": [], "modes": [], "reviewer": {}, "judges": [], "prices": {}}


@pytest.fixture
def kit():
    """A fresh module per test: configure() rewrites module settings."""
    spec = importlib.util.spec_from_file_location("evalkit_sandbox_under_test", KIT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(tmp_path, **over):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({**MINIMAL, **over}), encoding="utf-8")
    return path


def _tree(tmp_path):
    """A denied users tree holding the run root, as /tmp or /home holds it on a real machine."""
    base = Path(os.path.realpath(tmp_path))
    users = base / "users"
    root = users / "me" / "run"
    root.mkdir(parents=True)
    return base, users, root


def _pos(argv, *seq):
    """Index of the first occurrence of `seq` as consecutive items of argv."""
    for i in range(len(argv) - len(seq) + 1):
        if argv[i:i + len(seq)] == list(seq):
            return i
    raise AssertionError(f"{seq} not in {argv}")


# ------------------------------------------------------------------ bwrap policy

def test_bwrap_hides_each_deny_root_before_binding_the_run_root_inside_it(kit, tmp_path, monkeypatch):
    base, users, root = _tree(tmp_path)
    sock = base / "service.sock"
    sock.write_text("")  # a file deny root stands in for a service socket
    monkeypatch.setattr(kit, "SANDBOX", "bwrap")
    monkeypatch.setattr(kit, "DENY_ROOTS", (str(users), str(sock), str(base / "absent")))
    monkeypatch.setattr(kit, "ALLOW_READ", ())
    argv = kit.sandboxed(root, ["/bin/cat", "x"])
    assert argv[:4] == [kit.BWRAP, "--ro-bind", "/", "/"]
    assert argv[-3:] == ["--", "/bin/cat", "x"]
    tmpfs = _pos(argv, "--tmpfs", str(users))
    assert _pos(argv, "--ro-bind", "/dev/null", str(sock)) > 0
    assert str(base / "absent") not in argv, "a deny root that does not exist has nothing to hide"
    bind = _pos(argv, "--bind", str(root), str(root))
    assert tmpfs < bind, "the run root must be bound after the tmpfs that would hide it"
    for flag in ("--dev", "--proc", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--die-with-parent"):
        assert flag in argv
    assert not {"--unshare-net", "--unshare-all"} & set(argv), "candidates reach the model gateway"


def test_bwrap_binds_back_only_allowed_reads_that_a_deny_root_hides(kit, tmp_path, monkeypatch):
    base, users, root = _tree(tmp_path)
    venv = users / "me" / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("")
    link = users / "me" / "claude"
    link.symlink_to(venv)
    visible = base / "opt-tool"
    visible.mkdir()
    monkeypatch.setattr(kit, "SANDBOX", "bwrap")
    monkeypatch.setattr(kit, "DENY_ROOTS", (str(users),))
    monkeypatch.setattr(kit, "ALLOW_READ", (str(venv / "bin" / "python"), str(venv), str(link), str(visible),
                                            str(users / "me" / "gone")))
    argv = kit.sandboxed(root, ["/bin/true"])
    assert _pos(argv, "--ro-bind", str(venv), str(venv))
    assert str(venv / "bin" / "python") not in argv, "already readable through its bound parent"
    assert _pos(argv, "--symlink", str(venv), str(link)), "a symlink is recreated, not bound as its target"
    assert str(visible) not in argv, "outside every deny root it is readable through the read-only root"
    assert str(users / "me" / "gone") not in argv
    assert _pos(argv, "--tmpfs", str(users)) < _pos(argv, "--ro-bind", str(venv), str(venv))


def test_bwrap_binds_the_run_root_last_so_an_allowed_parent_cannot_make_it_read_only(kit, tmp_path, monkeypatch):
    base, users, root = _tree(tmp_path)
    monkeypatch.setattr(kit, "SANDBOX", "bwrap")
    monkeypatch.setattr(kit, "DENY_ROOTS", (str(users),))
    monkeypatch.setattr(kit, "ALLOW_READ", (str(users / "me"),))
    argv = kit.sandboxed(root, ["/bin/true"])
    assert _pos(argv, "--ro-bind", str(users / "me"), str(users / "me")) < _pos(argv, "--bind", str(root), str(root))


def test_sandbox_exec_reads_the_profile_written_into_the_run_root(kit, tmp_path, monkeypatch):
    monkeypatch.setattr(kit, "SANDBOX", "sandbox-exec")
    assert kit.sandboxed(tmp_path, ["/bin/true"]) == ["/usr/bin/sandbox-exec", "-f", str(tmp_path / "sandbox.sb"),
                                                      "/bin/true"]
    kit.prepare_sandbox(tmp_path)
    assert (tmp_path / "sandbox.sb").read_text() == kit.sandbox_profile(tmp_path)


def test_no_sandbox_builds_no_argv(kit, tmp_path, monkeypatch):
    monkeypatch.setattr(kit, "SANDBOX", "")
    with pytest.raises(kit.EvalError):
        kit.sandboxed(tmp_path, ["/bin/true"])


# -------------------------------------------------------------- isolation check

def _root(tmp_path):
    for sub in ("wt", "home", "tmp"):
        (tmp_path / sub).mkdir()
    (tmp_path / "wt" / "AGENTS.md").write_text("# entry\n")
    return tmp_path


@pytest.fixture
def probes_pass(kit, monkeypatch):
    """The probes start processes (integration tests run them); here only the argv check is under test."""
    monkeypatch.setattr(kit, "probe_sandbox", lambda root, denied: [])
    monkeypatch.setattr(kit, "probe_test_python", lambda root: [])


@pytest.mark.parametrize("sandbox", ["bwrap", "sandbox-exec"])
def test_the_isolation_check_accepts_every_builder_wrapped_for_this_run(kit, tmp_path, monkeypatch, probes_pass,
                                                                       sandbox):
    monkeypatch.setattr(kit, "SANDBOX", sandbox)
    monkeypatch.setattr(kit, "DENY_ROOTS", ())
    root = _root(tmp_path)
    assert kit.isolation_check(root, [], "p", {}, "codex", "gateway", argv=kit.codex_argv("m", "high", root, root)) == []
    assert kit.isolation_check(root, [], "p", {}, "claude", "gateway", argv=kit.claude_impl_argv("m", "high", root),
                               implement=True) == []


def test_the_isolation_check_rejects_an_unwrapped_or_foreign_argv(kit, tmp_path, monkeypatch, probes_pass):
    monkeypatch.setattr(kit, "SANDBOX", "bwrap")
    monkeypatch.setattr(kit, "DENY_ROOTS", ())
    (tmp_path / "run").mkdir()
    root = _root(tmp_path / "run")
    other = tmp_path / "other"
    other.mkdir()
    for argv in ([kit.CODEX_BIN, "exec", "-"], kit.codex_argv("m", "high", other, other)):
        failures = kit.isolation_check(root, [], "p", {}, "codex", "gateway", argv=argv)
        assert any("not wrapped" in f for f in failures), argv


# ------------------------------------------------------------- platform default

@pytest.mark.parametrize("platform,sandbox", [("darwin", "sandbox-exec"), ("linux", "bwrap")])
def test_the_platform_picks_the_sandbox_and_its_deny_roots(kit, tmp_path, monkeypatch, platform, sandbox):
    monkeypatch.setattr(kit.sys, "platform", platform)
    kit.configure(_config(tmp_path), host=None)
    assert kit.SANDBOX == sandbox
    assert kit.DENY_ROOTS == kit.DEFAULT_DENY_ROOTS[sandbox]


def test_a_platform_without_a_sandbox_is_an_error_not_an_unsandboxed_run(kit, tmp_path, monkeypatch):
    monkeypatch.setattr(kit.sys, "platform", "win32")
    with pytest.raises(kit.EvalError, match="never run unsandboxed"):
        kit.configure(_config(tmp_path))
    kit.configure(_config(tmp_path, isolation={"sandbox": "bwrap"}))
    assert kit.SANDBOX == "bwrap"


def test_an_unknown_sandbox_name_is_an_error(kit, tmp_path):
    with pytest.raises(kit.EvalError):
        kit.configure(_config(tmp_path, isolation={"sandbox": "firejail"}))


def test_configured_deny_roots_replace_the_platform_default(kit, tmp_path, monkeypatch):
    monkeypatch.setattr(kit.sys, "platform", "linux")
    kit.configure(_config(tmp_path, isolation={"deny_roots": ["/home", "/data"]}))
    assert kit.DENY_ROOTS == ("/home", "/data")


# ---------------------------------------------------------------------- hosts

HOSTS = {"pisces": {"scratch_root": "/tmp/agent-eval", "claude_bin": "/usr/local/bin/claude",
                    "isolation": {"sandbox": "bwrap", "deny_roots": ["/home", "/tmp"]},
                    "implement": {"test_python": "/home/me/venv/bin/python"}}}
TOP = {"scratch_root": "/private/tmp/agent-eval", "claude_bin": "~/.local/bin/claude",
       "isolation": {"sandbox": "sandbox-exec", "forbidden_env_prefixes": ["GH_", "VAULT_"]},
       "implement": {"test_python": "/Users/me/venv/bin/python", "repeats": 3}}


def test_the_entry_named_like_this_machine_is_merged_one_level_deep(kit, tmp_path, monkeypatch):
    monkeypatch.setattr(kit.socket, "gethostname", lambda: "Pisces.lan")
    kit.configure(_config(tmp_path, **TOP, hosts=HOSTS))
    assert kit.HOST == "pisces"
    assert (str(kit.SCRATCH_ROOT), kit.CLAUDE_BIN, kit.SANDBOX) == ("/tmp/agent-eval", "/usr/local/bin/claude", "bwrap")
    assert kit.DENY_ROOTS == ("/home", "/tmp")
    assert kit.FORBIDDEN_ENV_PREFIXES == ("GH_", "VAULT_"), "a key the host leaves out keeps the top-level value"
    assert kit.TEST_PYTHON == "/home/me/venv/bin/python" and kit.CFG["implement"]["repeats"] == 3
    assert "hosts" not in kit.CFG


def test_another_machine_uses_the_top_level(kit, tmp_path, monkeypatch):
    monkeypatch.setattr(kit.socket, "gethostname", lambda: "Rosetta-6")
    kit.configure(_config(tmp_path, **TOP, hosts=HOSTS))
    assert kit.HOST is None and kit.SANDBOX == "sandbox-exec" and kit.CLAUDE_BIN.endswith("/.local/bin/claude")


def test_a_named_host_wins_over_the_hostname_and_must_be_declared(kit, tmp_path, monkeypatch):
    monkeypatch.setattr(kit.socket, "gethostname", lambda: "Rosetta-6")
    kit.configure(_config(tmp_path, **TOP, hosts=HOSTS), host="PISCES")
    assert kit.HOST == "pisces" and kit.SANDBOX == "bwrap"
    with pytest.raises(kit.EvalError, match="not declared"):
        kit.configure(_config(tmp_path, **TOP, hosts=HOSTS), host="aries")


def test_the_cli_passes_host_through(kit, tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(kit, "configure", lambda path, host=None: seen.update(host=host))
    monkeypatch.setattr(kit, "cmd_report", lambda args: 0)
    assert kit.main(["--config", str(_config(tmp_path)), "--host", "pisces", "report", "--batch", "b"]) == 0
    assert seen == {"host": "pisces"}


def test_a_host_leaves_the_text_digest_alone_and_may_not_replace_the_text(kit, tmp_path, monkeypatch):
    monkeypatch.setattr(kit.socket, "gethostname", lambda: "pisces")
    kit.configure(_config(tmp_path, **TOP))
    plain = kit.TEXT_DIGEST
    kit.configure(_config(tmp_path, **TOP, hosts=HOSTS))
    assert kit.TEXT_DIGEST == plain == kit.text_digest(kit.TEXT_EN)
    with pytest.raises(kit.EvalError, match="text"):
        kit.configure(_config(tmp_path, **TOP, hosts={"pisces": {"text": "zh.json"}}))
