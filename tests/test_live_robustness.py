"""Regression tests for connection and live-update findings from the
November 2026 review. Each reproduced a bug before its fix."""

import asyncio
import copy
import json
import logging
import os
import sys
import threading
import time

import pytest

from roku_smarthome import RokuSmartHome, RokuUnavailable, SessionExpired

from conftest import make_leaf
from test_live import Changes, report, rk, server  # noqa: F401  (fixtures)


def test_reconnect_survives_socket_dropping_while_list_loads(rk, server):
    changes = Changes()
    rk.on_change(changes)
    server.wait_for_clients()
    real_fetch = rk._fetch_leaves
    calls = {"n": 0}

    def fetch_then_drop():
        calls["n"] += 1
        if calls["n"] == 1:  # the first reconnect's fetch: drop the new socket
            time.sleep(0.3)
            server.drop_all_connections()
            time.sleep(0.3)
        return real_fetch()
    rk._fetch_leaves = fetch_then_drop
    server.drop_all_connections()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not (
            rk._ws is not None and calls["n"] >= 2 and server.wait_for_clients(1, 0.1)):
        time.sleep(0.1)
    server.push(report("leaf-2", power="on"))
    assert changes.wait(1, timeout=5)[-1].power == "on"


def test_subscribe_racing_unsubscribe_keeps_new_callback_live(rk, server, monkeypatch):
    first = rk.on_change(Changes())
    server.wait_for_clients()
    real_stop = rk._stop_listening

    async def slow_stop():
        await asyncio.sleep(0.5)
        await real_stop()
    monkeypatch.setattr(rk, "_stop_listening", slow_stop)
    t = threading.Thread(target=first)
    t.start()
    time.sleep(0.1)  # unsubscribe has decided to stop and is stopping
    second = Changes()
    rk.on_change(second)
    t.join()
    assert rk.listening
    server.wait_for_clients()
    server.push(report("leaf-2", power="on"))
    assert second.wait(1, timeout=5)


def test_reconnect_backs_off_when_server_keeps_dropping(rk, server):
    rk.on_change(Changes())
    server.wait_for_clients()

    async def accept_then_close(ws):
        server.connections += 1
        await ws.close()
    server.custom_handler = accept_then_close
    before = server.connections
    server.drop_all_connections()
    time.sleep(6.5)
    # delays 1, 2, 4 s: at most 3 reconnects in 6.5 s (it used to be ~6)
    assert server.connections - before <= 3


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
@pytest.mark.skipif(sys.platform == "darwin",
                    reason="forking a multi-threaded process is unreliable on macOS "
                           "(the system frameworks can deadlock the child)")
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded")
def test_forked_child_does_not_hang(rk, server):
    rk.find("plug").turn_on()          # parent has a running loop and socket
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:  # child
        try:
            start = time.monotonic()
            try:
                rk.find("plug").turn_off()
                result = "ok"
            except Exception as e:
                result = type(e).__name__
            rk.close()
            os.write(w, json.dumps([result, time.monotonic() - start]).encode())
        finally:
            os._exit(0)
    os.close(w)
    _, status = os.waitpid(pid, 0)
    result, took = json.loads(os.read(r, 1000).decode())
    assert result == "ok" and took < 5


@pytest.mark.parametrize("how", ["idle", "close"])
def test_slow_reply_is_not_cut_off_by_closing(rk, server, how):
    server.reply_delay = 0.8
    if how == "idle":
        rk.idle_timeout = 0.1
        reply = rk.find("plug").send("power", {"power": "on"})
    else:
        threading.Timer(0.2, rk.close).start()
        reply = rk.find("plug").send("power", {"power": "on"})
    assert reply["type"] == "send_command_resp"


def test_report_during_reconnect_fetch_is_not_undone(rk, server):
    changes = Changes()
    rk.on_change(changes)
    server.wait_for_clients()
    stale = copy.deepcopy(rk.leaves)       # plug is off in this snapshot
    pushed = threading.Event()

    def slow_stale_fetch():
        # meanwhile the plug turns on and Roku reports it
        threading.Thread(target=lambda: (server.wait_for_clients(), time.sleep(0.2),
                                         server.push(report("leaf-2", power="on")),
                                         pushed.set())).start()
        time.sleep(0.8)
        return copy.deepcopy(stale)
    rk._fetch_leaves = slow_stale_fetch
    server.drop_all_connections()
    assert pushed.wait(10)
    time.sleep(1.5)
    assert [d.power for d in changes.devices if d.id == "leaf-2"] == ["on"]
    assert rk._live["leaf-2"]["state"]["power"]["power"] == "on"


def test_report_while_subscribing_is_not_lost(rk, server, monkeypatch):
    changes = Changes()
    real_fetch = rk._fetch_leaves

    def fetch_with_change_meanwhile():
        server.wait_for_clients()
        server.push(report("leaf-2", power="on"))
        time.sleep(0.3)
        return real_fetch()            # still says off
    rk._fetch_leaves = fetch_with_change_meanwhile
    rk.on_change(changes)
    assert changes.wait(1, timeout=5)[0].power == "on"
    assert rk._live["leaf-2"]["state"]["power"]["power"] == "on"


def test_bookkeeping_only_report_does_not_fire(rk, server):
    changes = Changes()
    rk.on_change(changes)
    server.wait_for_clients()
    server.push(report("leaf-2", power="off"))   # already off; new eventId only
    server.push(report("leaf-2", power="on"))
    devices = changes.wait(1)
    time.sleep(0.3)
    assert [d.power for d in devices] == ["on"]


@pytest.mark.parametrize("bad", [
    {"id": [1, 2], "type": "send_command_resp"},
    {"id": {"x": 1}, "type": "send_command"},
    {"type": "leaf_state_report", "payload": [{"id": ["x"], "state": {}}]},
])
def test_odd_message_does_not_kill_reader(rk, server, bad):
    plug = rk.find("plug")
    plug.turn_on()
    server.push(bad)
    time.sleep(0.2)
    plug.turn_off()
    assert server.connections == 1
    assert len(server._clients) == 1


def test_cookie_not_in_websocket_debug_logs(rk, server, caplog):
    rk.import_cookie_header("ks.session=TOPSECRET42")
    with caplog.at_level(logging.DEBUG):
        rk.find("plug").turn_on()
    # only the client's logs; the fake server logs what it receives
    client_logs = "\n".join(r.getMessage() for r in caplog.records
                            if not r.name.startswith("websockets.server"))
    assert "<redacted>" in client_logs  # the Cookie header was logged, redacted
    assert "TOPSECRET42" not in client_logs


def test_live_error_reports_expired_session(rk, server):
    rk.on_change(Changes())
    server.wait_for_clients()
    assert rk.live_error is None
    server.reject_with = 403
    server.drop_all_connections()
    deadline = time.monotonic() + 8
    while rk.live_error is None and time.monotonic() < deadline:
        time.sleep(0.1)
    assert isinstance(rk.live_error, SessionExpired)
    server.reject_with = None
    deadline = time.monotonic() + 10
    while rk.live_error is not None and time.monotonic() < deadline:
        time.sleep(0.1)
    assert rk.live_error is None and rk._ws is not None
