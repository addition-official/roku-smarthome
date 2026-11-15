import copy

import pytest

from roku_smarthome import RokuSmartHome


@pytest.fixture(autouse=True)
def never_contact_roku(monkeypatch):
    """Any client a test forgets to point at a fake server fails fast on a
    dead local port instead of connecting to Roku's real websocket."""
    import roku_smarthome.client as client_module
    monkeypatch.setattr(client_module, "SOCKET_URL", "ws://127.0.0.1:9/never-roku")


def make_leaf(**overrides):
    """A device entry shaped like one item from Roku's /leaves response."""
    leaf = {
        "id": "leaf-1",
        "name": "Bedroom Bulb",
        "type": "device",
        "subType": "light",
        "device": {"type": "light", "model": "BS1000X", "localIp": "192.168.1.20"},
        "supportedCommands": [{"type": "power"}, {"type": "brightness"}, {"type": "color"}],
        "state": {
            "power": {"power": "on"},
            "online": {"online": True},
            "brightness": {"level": 60},
            "color": {"colorType": "temperature", "temperature": 3000},
        },
    }
    leaf.update(overrides)
    return leaf


@pytest.fixture
def cookie_file(tmp_path):
    return tmp_path / "cookies.json"


@pytest.fixture
def client(cookie_file):
    """A client with a saved session that never touches the network.
    Device-list fetches return `client.fake_leaves`; commands are recorded
    in `client.sent` instead of going over the websocket."""
    rk = RokuSmartHome(cookie_file)
    rk.import_cookie_header("ks.session=abc123")
    rk.fake_leaves = [
        make_leaf(),
        make_leaf(
            id="leaf-2",
            name="Living Room Plug",
            subType="plug",
            supportedCommands=[{"type": "power"}],
            state={"power": {"power": "off"}, "online": {"online": True}},
        ),
    ]
    rk.fetch_count = 0
    rk.sent = []

    def fake_fetch():
        rk.fetch_count += 1
        return copy.deepcopy(rk.fake_leaves)

    def fake_send(device, command, parameters, wait=None, confirm=None):
        rk.sent.append((device.id, command, parameters))
        rk.invalidate()

    rk._fetch_leaves = fake_fetch
    rk._send_command = fake_send
    return rk
