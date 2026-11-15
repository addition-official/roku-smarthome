"""CLI behaviour that only shows up in a real process: pipes, Ctrl-C,
closed stdin, terminal encodings. Found in the November 2026 review."""

import os
import signal
import subprocess
import sys
import time

import pytest

CHILD = os.path.join(os.path.dirname(__file__), "cli_child.py")


def run(scenario, *args, tmp_path, stdin=subprocess.DEVNULL, env=None, **kw):
    e = dict(os.environ, COOKIE_DIR=str(tmp_path), **(env or {}))
    return subprocess.Popen([sys.executable, CHILD, scenario, *args], stdin=stdin,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=e, **kw)


def test_status_into_closed_pipe_is_quiet(tmp_path):
    p = run("many", "status", tmp_path=tmp_path)
    p.stdout.readline()
    p.stdout.close()                     # like `| head -1`
    p.wait(15)
    err = p.stderr.read().decode()
    assert "Traceback" not in err and "BrokenPipe" not in err
    assert p.returncode == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX pipes")
def test_watch_into_closed_pipe_exits(tmp_path):
    p = run("watch", "watch", tmp_path=tmp_path)
    assert b"Watching" in p.stdout.readline()
    p.stdout.readline()
    p.stdout.close()                     # like `watch | head -2`
    p.wait(15)                           # used to run forever
    err = p.stderr.read().decode()
    assert err.count("Traceback") == 0
    assert p.returncode == 0


def test_ascii_terminal_with_emoji_name(tmp_path):
    p = run("emoji", "status", tmp_path=tmp_path, env={"PYTHONIOENCODING": "ascii"})
    out, err = p.communicate(timeout=15)
    assert p.returncode == 0, err
    assert b"Lamp ?" in out


def test_ascii_terminal_command_reports_ok(tmp_path):
    # the command already ran; an encoding problem must not report failure
    p = run("emoji", "brightness", "lamp", "40", tmp_path=tmp_path,
            env={"PYTHONIOENCODING": "ascii"})
    out, err = p.communicate(timeout=15)
    assert p.returncode == 0, err
    assert b"OK: brightness -> Lamp ?" in out


def test_terminal_escapes_in_device_names_are_neutralized(tmp_path):
    p = run("escape", "status", tmp_path=tmp_path)
    out, _ = p.communicate(timeout=15)
    assert b"\x1b" not in out and b"\x07" not in out


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
def test_ctrl_c_exits_130_without_traceback(tmp_path):
    p = run("slow", "status", tmp_path=tmp_path)
    time.sleep(1.5)
    p.send_signal(signal.SIGINT)
    _, err = p.communicate(timeout=15)
    assert p.returncode == 130
    assert b"Traceback" not in err and b"Interrupted" in err


def test_login_with_closed_stdin(tmp_path):
    p = run("many", "login", tmp_path=tmp_path)
    _, err = p.communicate(timeout=15)
    assert p.returncode == 1
    assert b"Traceback" not in err and b"no cookie given" in err


def test_login_save_failure_is_a_clear_error(tmp_path):
    (tmp_path / "dir.json").mkdir()
    p = run("many", "--cookies", str(tmp_path / "dir.json"), "login", "ks.session=x",
            tmp_path=tmp_path)
    _, err = p.communicate(timeout=15)
    assert p.returncode == 1
    assert b"Traceback" not in err and b"could not save the login" in err


@pytest.mark.parametrize("bad", ["-1", "0", "nan", "inf", "x"])
def test_watch_rejects_bad_seconds(tmp_path, bad):
    p = run("many", "watch", "--seconds", bad, tmp_path=tmp_path)
    _, err = p.communicate(timeout=15)
    assert p.returncode == 2
    assert b"Watching" not in err
