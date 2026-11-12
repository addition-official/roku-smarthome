"""Live updates (on_change) against the local fake Roku server."""

import copy
import json
import logging
import threading
import time

import pytest

from roku_smarthome import RokuSmartHome, SessionExpired, cli

from conftest import make_leaf
from test_websocket import FakeRoku


def report(leaf_id, **state):
    """A leaf_state_report shaped like the one captured from real Roku."""
    return {"type": "leaf_state_report", "payload": [{
        "id": leaf_id, "type": "device",
        "state": {k: {k if k != "brightness" else "level": v,
                      "updated": 1794447253339, "eventId": "1"} for k, v in state.items()},
    }]}


class Changes:
    """Collects on_change callbacks so tests can wait for them."""

    def __init__(self):
        self.devices = []
        self._cond = threading.Condition()

    def __call__(self, device):
        with self._cond:
            self.devices.append(device)
            self._cond.notify_all()

    def wait(self, n, timeout=5):
        with self._cond:
            self._cond.wait_for(lambda: len(self.devices) >= n, timeout)
        return self.devices


@pytest.fixture
def server():
    s = FakeRoku()
    yield s
    s.stop()


@pytest.fixture
def rk(cookie_file, server):
    rk = RokuSmartHome(cookie_file, timeout=3)
    rk.import_cookie_header("ks.session=abc")
    rk.socket_url = server.url
    rk.leaves = [
        make_leaf(),  # leaf-1, Bedroom Bulb, on, 60%, 3000K
        make_leaf(id="leaf-2", name="Living Room Plug", subType="plug",
                  device={"id": "SERIAL-2", "type": "plug"},
                  supportedCommands=[{"type": "power"}],
                  state={"power": {"power": "off"}, "online": {"online": True}}),
    ]
    rk._fetch_leaves = lambda: copy.deepcopy(rk.leaves)
    yield rk
    rk.close()


def test_push_reports_change_with_full_device(rk, server):
    changes = Changes()
    rk.on_change(changes)
    server.wait_for_clients()
    server.push(report("leaf-2", power="on"))
    [d] = changes.wait(1)
    assert d.name == "Living Room Plug" and d.power == "on" and d.online is True


def test_partial_report_keeps_other_state(rk, server):
    changes = Changes()
    rk.on_change(changes)
    server.wait_for_clients()
    server.push(report("leaf-1", brightness=25))
    [d] = changes.wait(1)
    assert d.brightness == 25
    assert d.power == "on" and d.color_temp == 3000  # untouched


def test_reports_build_on_each_other(rk, server):
    changes = Changes()
    rk.on_change(changes)
    server.wait_for_clients()
    server.push(report("leaf-1", power="off"))
    server.push(report("leaf-1", brightness=10))
    d = changes.wait(2)[-1]
    assert d.power == "off" and d.brightness == 10


def test_report_matched_by_device_serial(rk, server):
    # The captured report used an id like the device serial; accept either.
    changes = Changes()
    rk.on_change(changes)
    server.wait_for_clients()
    server.push(report("SERIAL-2", power="on"))
    [d] = changes.wait(1)
    assert d.id == "leaf-2" and d.power == "on"


def test_unknown_and_malformed_reports_are_ignored(rk, server):
    changes = Changes()
    rk.on_change(changes)
    server.wait_for_clients()
    server.push(report("not-a-device", power="on"))
    server.push({"type": "leaf_state_report", "payload": None})
    server.push({"type": "leaf_state_report", "payload": [None, {"id": "leaf-1"},
                                                          {"id": "leaf-1", "state": "x"}]})
    server.push({"type": "something_else", "payload": [1, 2]})
    server.push(report("leaf-2", power="on"))
    devices = changes.wait(1)
    time.sleep(0.2)
    assert [d.id for d in devices] == ["leaf-2"]


def test_callback_can_send_commands(rk, server):
    done = threading.Event()

    def turn_it_back_off(device):
        if device.power == "on":
            device.turn_off()
            done.set()
    rk.on_change(turn_it_back_off)
    server.wait_for_clients()
    server.push(report("leaf-2", power="on"))
    assert done.wait(5)
    assert server.received[-1]["payload"]["command"]["parameters"] == {"power": "off"}


def test_failing_callback_does_not_stop_updates(rk, server, caplog):
    changes = Changes()

    def broken(device):
        raise RuntimeError("boom")
    rk.on_change(broken)
    rk.on_change(changes)
    server.wait_for_clients()
    with caplog.at_level(logging.ERROR, logger="roku_smarthome.client"):
        server.push(report("leaf-2", power="on"))
        server.push(report("leaf-2", power="off"))
        assert len(changes.wait(2)) == 2
    assert "boom" in caplog.text


def test_listening_keeps_connection_open_past_idle_timeout(rk, server):
    rk.idle_timeout = 0.2
    rk.on_change(Changes())
    time.sleep(0.8)
    assert rk._ws is not None and server.connections == 1


def test_commands_share_the_listening_connection(rk, server):
    rk.on_change(Changes())
    rk.find("plug").turn_on()
    assert server.connections == 1


def test_reconnects_and_reports_changes_missed_while_offline(rk, server):
    changes = Changes()
    rk.on_change(changes)
    server.wait_for_clients()
    server.drop_all_connections()
    # someone turns the plug on in the Roku app while we're disconnected
    rk.leaves[1]["state"]["power"] = {"power": "on"}
    [d] = changes.wait(1, timeout=10)
    assert d.id == "leaf-2" and d.power == "on"
    # and pushes arrive on the new connection
    server.wait_for_clients()
    server.push(report("leaf-1", power="off"))
    assert changes.wait(2)[-1].power == "off"
    assert server.connections == 2


def test_unsubscribe_stops_updates_and_restores_idle_close(rk, server):
    changes = Changes()
    rk.idle_timeout = 0.3
    stop = rk.on_change(changes)
    server.wait_for_clients()
    stop()
    assert not rk.listening
    server.push(report("leaf-2", power="on"))
    time.sleep(1.2)  # idle timeout plus the wait after the last command
    assert changes.devices == []
    assert rk._ws is None


def test_close_stops_listening_and_reconnecting(rk, server):
    changes = Changes()
    rk.on_change(changes)
    server.wait_for_clients()
    rk.close()
    assert not rk.listening
    time.sleep(1.5)  # longer than the first reconnect delay
    assert server.connections == 1


def test_expired_session_fails_cleanly(rk):
    def expired():
        raise SessionExpired("x")
    rk._fetch_leaves = expired
    with pytest.raises(SessionExpired):
        rk.on_change(Changes())
    assert not rk.listening and rk._listeners == []


def test_reports_are_ignored_when_not_listening(rk, server):
    rk.find("plug").turn_on()        # opens the shared connection
    server.wait_for_clients()
    server.push(report("leaf-2", power="off"))
    time.sleep(0.2)
    assert rk._live == {}


def test_cli_watch_prints_changes(rk, server, monkeypatch, capsys):
    monkeypatch.setattr(cli, "RokuSmartHome", lambda cookies=None: rk)
    t = threading.Thread(target=cli.main, args=(["watch", "--seconds", "1.5"],))
    t.start()
    server.wait_for_clients()
    time.sleep(0.2)
    server.push(report("leaf-2", power="on"))
    t.join(10)
    out = capsys.readouterr().out
    assert "Watching for changes" in out
    assert "Living Room Plug" in out and "ON" in out
