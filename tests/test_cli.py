import pytest

from roku_smarthome import cli, RokuUnavailable, SessionExpired


@pytest.fixture
def use_client(client, monkeypatch):
    """Make the CLI use the offline test client."""
    monkeypatch.setattr(cli, "RokuSmartHome", lambda cookies=None: client)
    return client


def test_status_lists_devices(use_client, capsys):
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "Bedroom Bulb" in out and "60%" in out and "3000K" in out
    assert "Living Room Plug" in out and "OFF" in out


def test_on_command(use_client, capsys):
    cli.main(["on", "living room plug"])
    assert use_client.sent == [("leaf-2", "power", {"power": "on"})]
    assert "OK: on -> Living Room Plug" in capsys.readouterr().out


def test_rgb_command(use_client):
    cli.main(["rgb", "Bedroom Bulb", "255", "80", "0"])
    assert use_client.sent[-1][2] == {"colorType": "rgb", "rgb": [255, 80, 0]}


@pytest.mark.parametrize("argv,message", [
    (["on", "garage"], "Error"),
    (["brightness", "Bedroom Bulb", "150"], "Error"),
    (["brightness", "Living Room Plug", "50"], "Error"),
])
def test_bad_input_exits_with_error(use_client, argv, message):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert message in str(exc.value.code)
    assert use_client.sent == []


@pytest.mark.parametrize("error,label", [
    (SessionExpired("x"), "SESSION EXPIRED"),
    (RokuUnavailable("x"), "ROKU UNAVAILABLE"),
])
def test_connection_errors_are_reported(use_client, error, label):
    def boom():
        raise error
    use_client._fetch_leaves = boom
    with pytest.raises(SystemExit) as exc:
        cli.main(["status"])
    assert label in str(exc.value.code)
