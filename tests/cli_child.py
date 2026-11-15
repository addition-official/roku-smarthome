"""Runs the CLI in a child process with a fake client, for tests that need a
real process (pipes, signals, closed stdin). Usage: cli_child.py SCENARIO ARGS..."""

import copy
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))

import roku_smarthome.client  # noqa: E402
from roku_smarthome import RokuSmartHome, cli  # noqa: E402

roku_smarthome.client.SOCKET_URL = "ws://127.0.0.1:9/never-roku"  # belt and braces
from conftest import make_leaf  # noqa: E402

scenario, args = sys.argv[1], sys.argv[2:]
leaves = [make_leaf(id=f"d{i}", name=f"Lamp {i}") for i in range(200)]
if scenario == "emoji":
    leaves = [make_leaf(name="Lamp \U0001f4a1")]
if scenario == "escape":
    leaves = [make_leaf(name="Evil\x1b]0;pwned\x07\x1b[2J")]


def fake_client(cookies=None):
    rk = RokuSmartHome(cookies or os.path.join(os.environ["COOKIE_DIR"], "c.json"))
    rk._fetch_leaves = lambda: copy.deepcopy(leaves)
    if scenario == "slow":
        def slow():
            time.sleep(10)
            return []
        rk._fetch_leaves = slow
    # every scenario talks to a local fake server, never to Roku
    from test_websocket import FakeRoku
    server = FakeRoku()
    rk.socket_url = server.url
    if scenario == "watch":
        rk._cookies = {"ks.session": "x"}

        def pump():
            server.wait_for_clients()
            for i in range(300):
                server.push({"type": "leaf_state_report", "payload": [{
                    "id": "d1", "state": {"power": {"power": "on" if i % 2 else "off"}}}]})
                time.sleep(0.01)
        threading.Thread(target=pump, daemon=True).start()
    return rk


cli.RokuSmartHome = fake_client
cli.main(args)
