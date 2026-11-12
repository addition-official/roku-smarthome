import pytest

from roku_smarthome import Device, DeviceNotFound, UnsupportedCommand

from conftest import make_leaf


def test_device_fields_are_parsed(client):
    d = Device(client, make_leaf())
    assert d.id == "leaf-1"
    assert d.name == "Bedroom Bulb"
    assert d.type == "light"
    assert d.model == "BS1000X"
    assert d.local_ip == "192.168.1.20"
    assert d.power == "on" and d.is_on
    assert d.online is True
    assert d.brightness == 60
    assert d.color_temp == 3000
    assert d.color_type == "temperature"
    assert d.supported_commands == {"power", "brightness", "color"}


def test_device_with_no_state_does_not_crash(client):
    d = Device(client, {"id": "cam-1", "name": "Camera", "state": None})
    assert d.power is None and d.online is None and d.brightness is None
    assert not d.is_on
    assert d.supported_commands == set()


def test_get_matches_name_case_insensitively(client):
    assert client.get("bedroom bulb").id == "leaf-1"
    assert client.get("LIVING ROOM PLUG").id == "leaf-2"


def test_get_by_id(client):
    assert client.get("leaf-2", by_id=True).name == "Living Room Plug"


def test_get_unknown_device_raises(client):
    with pytest.raises(DeviceNotFound):
        client.get("garage")


def test_power_commands(client):
    plug = client.get("Living Room Plug")
    plug.turn_on()
    assert plug.power == "on"
    plug.toggle()
    assert plug.power == "off"
    assert [s[1:] for s in client.sent] == [
        ("power", {"power": "on"}),
        ("power", {"power": "off"}),
    ]


def test_brightness_command_and_validation(client):
    bulb = client.get("Bedroom Bulb")
    bulb.set_brightness(40)
    assert client.sent[-1] == ("leaf-1", "brightness", {"level": 40})
    assert bulb.brightness == 40
    for bad in (-1, 101):
        with pytest.raises(ValueError):
            bulb.set_brightness(bad)
    assert len(client.sent) == 1


def test_color_temp_command(client):
    bulb = client.get("Bedroom Bulb")
    bulb.set_color_temp(4000)
    assert client.sent[-1] == (
        "leaf-1", "color", {"colorType": "temperature", "temperature": 4000})
    assert bulb.color_temp == 4000


def test_rgb_command_and_validation(client):
    bulb = client.get("Bedroom Bulb")
    bulb.set_color_rgb(255, 80, 0)
    assert client.sent[-1] == ("leaf-1", "color", {"colorType": "rgb", "rgb": [255, 80, 0]})
    assert bulb.color_rgb == [255, 80, 0] and bulb.color_type == "rgb"
    with pytest.raises(ValueError):
        bulb.set_color_rgb(256, 0, 0)
    assert len(client.sent) == 1


def test_unsupported_command_is_not_sent(client):
    plug = client.get("Living Room Plug")
    with pytest.raises(UnsupportedCommand):
        plug.set_brightness(50)
    assert client.sent == []


def test_refresh_reloads_state(client):
    plug = client.get("Living Room Plug")
    client.fake_leaves[1]["state"] = {"power": {"power": "on"}, "online": {"online": False}}
    plug.refresh()
    assert plug.power == "on" and plug.online is False


def test_cache_is_shared_until_ttl_and_cleared_by_commands(client):
    client.cache_ttl = 60
    client.devices()
    client.devices()
    assert client.fetch_count == 1
    client.get("Living Room Plug").turn_on()  # get() hits the cache too
    assert client.fetch_count == 1
    client.devices()  # command invalidated the cache
    assert client.fetch_count == 2
    client.devices(max_age=0)
    assert client.fetch_count == 3


def test_no_cache_by_default(client):
    client.devices()
    client.devices()
    assert client.fetch_count == 2


def test_color_temp_validation(client):
    bulb = client.get("Bedroom Bulb")
    bulb.set_color_temp(1800)
    bulb.set_color_temp(6500)
    for bad in (1799, 6501, 30000, 0):
        with pytest.raises(ValueError):
            bulb.set_color_temp(bad)
    assert len(client.sent) == 2
    assert bulb.color_temp == 6500



@pytest.mark.parametrize("leaf", [
    {"id": "x", "name": None, "device": None, "state": None, "supportedCommands": None},
    {"id": "x", "state": {"power": None, "online": None, "brightness": None, "color": None}},
    {"id": "x", "supportedCommands": [None, "power", {"type": None}, {"type": "power"}]},
    {"id": 123},
])
def test_nulls_and_junk_do_not_crash(client, leaf):
    d = Device(client, leaf)
    assert d.name == "" and d.power is None and d.brightness is None
    assert d.supported_commands <= {"power"}
    repr(d)


def test_status_and_get_survive_null_names(client, capsys):
    from roku_smarthome import cli
    client.fake_leaves.append({"id": "nameless", "name": None, "state": None})
    client.fake_leaves.append(None)          # junk entries are skipped
    client.fake_leaves.append({"name": "no id"})
    assert client.get("bedroom bulb").id == "leaf-1"
    assert client.get("nameless", by_id=True).name == ""
    import roku_smarthome.cli as cli_mod
    orig = cli_mod.RokuSmartHome
    cli_mod.RokuSmartHome = lambda cookies=None: client
    try:
        cli.main(["status"])
    finally:
        cli_mod.RokuSmartHome = orig
    assert "Bedroom Bulb" in capsys.readouterr().out


def test_duplicate_names_are_refused_by_get(client):
    client.fake_leaves = [make_leaf(id="a", name="Lamp"), make_leaf(id="b", name="lamp")]
    with pytest.raises(DeviceNotFound) as exc:
        client.get("Lamp")
    assert "a" in str(exc.value) and "b" in str(exc.value)
    assert client.get("b", by_id=True).id == "b"


@pytest.mark.parametrize("bad", [50.9, True, False, "50", None, 0.5])
def test_brightness_rejects_non_whole_numbers(client, bad):
    with pytest.raises(ValueError):
        client.get("Bedroom Bulb").set_brightness(bad)
    assert client.sent == []


def test_whole_floats_are_accepted(client):
    bulb = client.get("Bedroom Bulb")
    bulb.set_brightness(50.0)
    bulb.set_color_temp(3000.0)
    assert client.sent[0][2] == {"level": 50}
    assert client.sent[1][2]["temperature"] == 3000


@pytest.mark.parametrize("rgb", [(255.9, 0, 0), (-0.5, 0, 0), (True, 0, 0), (-1, 0, 0), ("1", 2, 3)])
def test_rgb_rejects_bad_values(client, rgb):
    with pytest.raises(ValueError):
        client.get("Bedroom Bulb").set_color_rgb(*rgb)
    assert client.sent == []


@pytest.mark.parametrize("bad", [0, -100, 100000, 3000.5])
def test_color_temp_rejects_qa_values(client, bad):
    with pytest.raises(ValueError):
        client.get("Bedroom Bulb").set_color_temp(bad)
    assert client.sent == []
