import pytest

from roku_smarthome import cli, DeviceNotFound, RokuUnavailable, SessionExpired

from conftest import make_leaf


@pytest.fixture
def use_client(client, monkeypatch):
    """Make the CLI use the offline test client."""
    monkeypatch.setattr(cli, "RokuSmartHome", lambda cookies=None: client)
    return client


def run_failing(argv, capsys):
    """Run the CLI expecting it to exit; return (exit code, stderr)."""
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    return exc.value.code, capsys.readouterr().err


def test_status_lists_devices(use_client, capsys):
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "Bedroom Bulb" in out and "60%" in out and "3000K" in out
    assert "Living Room Plug" in out and "OFF" in out


def test_on_command(use_client, capsys):
    cli.main(["on", "living room plug"])
    assert use_client.sent == [("leaf-2", "power", {"power": "on"})]
    assert "OK: on -> Living Room Plug" in capsys.readouterr().out


# ----- partial names -----

def test_partial_name(use_client, capsys):
    cli.main(["on", "plug"])
    assert use_client.sent == [("leaf-2", "power", {"power": "on"})]


def test_partial_name_any_case(use_client):
    cli.main(["brightness", "BEDROOM", "30"])
    assert use_client.sent == [("leaf-1", "brightness", {"level": 30})]


def test_ambiguous_partial_name_lists_matches(use_client, capsys):
    code, err = run_failing(["on", "room"], capsys)
    assert code == cli.EXIT_ERROR
    assert "several devices" in err and "Bedroom Bulb" in err and "Living Room Plug" in err
    assert use_client.sent == []


def test_exact_name_wins_over_partial(use_client):
    # "Lamp" is exact for one device even though "Lamp 2" also contains it
    use_client.fake_leaves = [make_leaf(id="a", name="Lamp"), make_leaf(id="b", name="Lamp 2")]
    cli.main(["on", "lamp"])
    assert use_client.sent[0][0] == "a"


def test_unknown_device_lists_names(use_client, capsys):
    code, err = run_failing(["on", "garage"], capsys)
    assert code == cli.EXIT_ERROR
    assert "no device matches 'garage'" in err and "Bedroom Bulb" in err


def test_find_by_id(client):
    assert client.find("leaf-2").name == "Living Room Plug"


def test_find_empty_query_matches_nothing(client):
    with pytest.raises(DeviceNotFound):
        client.find("  ")


# ----- colors -----

@pytest.mark.parametrize("args,rgb", [
    (["255", "80", "0"], [255, 80, 0]),
    (["#ff5000"], [255, 80, 0]),
    (["FF5000"], [255, 80, 0]),
    (["#f50"], [255, 85, 0]),
])
def test_rgb_formats(use_client, args, rgb):
    cli.main(["rgb", "Bedroom Bulb", *args])
    assert use_client.sent[-1][2] == {"colorType": "rgb", "rgb": rgb}


@pytest.mark.parametrize("args", [["#ff50"], ["#gg0000"], ["1", "2"], ["a", "b", "c"], ["300", "0", "0"]])
def test_bad_rgb_is_rejected(use_client, capsys, args):
    code, err = run_failing(["rgb", "Bedroom Bulb", *args], capsys)
    assert code == cli.EXIT_ERROR and "Error" in err
    assert use_client.sent == []


def test_temp_out_of_range_is_rejected(use_client, capsys):
    code, err = run_failing(["temp", "Bedroom Bulb", "30000"], capsys)
    assert code == cli.EXIT_ERROR and "1800..6500" in err
    assert use_client.sent == []


# ----- errors and exit codes -----

@pytest.mark.parametrize("argv", [
    ["brightness", "Bedroom Bulb", "150"],
    ["brightness", "Living Room Plug", "50"],  # plug has no brightness
])
def test_bad_input_exits_with_error(use_client, capsys, argv):
    code, err = run_failing(argv, capsys)
    assert code == cli.EXIT_ERROR and "Error" in err
    assert use_client.sent == []


@pytest.mark.parametrize("error,label,code", [
    (SessionExpired("x"), "SESSION EXPIRED", cli.EXIT_SESSION_EXPIRED),
    (RokuUnavailable("x"), "ROKU UNAVAILABLE", cli.EXIT_UNAVAILABLE),
])
def test_connection_errors_have_their_own_exit_codes(use_client, capsys, error, label, code):
    def boom():
        raise error
    use_client._fetch_leaves = boom
    exit_code, err = run_failing(["status"], capsys)
    assert exit_code == code and label in err


def test_exit_codes_are_distinct():
    codes = {cli.EXIT_ERROR, cli.EXIT_SESSION_EXPIRED, cli.EXIT_UNAVAILABLE, 2}
    assert len(codes) == 4
