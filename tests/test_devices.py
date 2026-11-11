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
