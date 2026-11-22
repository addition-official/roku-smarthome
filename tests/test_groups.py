"""Room groups: Roku returns a group as one leaf with its devices nested in
"memberLeaves". Shapes below are from a real account (ids changed)."""

import pytest

from roku_smarthome import DeviceNotFound, cli

from conftest import make_leaf


def make_group(*members, **overrides):
    """A group leaf as Roku sends it: no subType or device, members nested."""
    group = {
        "id": "group-1",
        "name": "Bedroom",
        "type": "group",
        "supportedCommands": [{"type": "power"}, {"type": "brightness"}, {"type": "color"}],
        "state": {
            "power": {"power": "on"},
            "online": {"online": True},
            "brightness": {"level": 60},
            "color": {"colorType": "temperature", "temperature": 3000},
        },
        "memberLeaves": list(members),
    }
    group.update(overrides)
    return group


def lamp(n, **overrides):
    fields = {"id": f"lamp-{n}", "name": f"Lamp {n}", "device": {"id": f"lamp-{n}", "type": "light"}}
    return make_leaf(**{**fields, **overrides})


@pytest.fixture
def grouped(client):
    client.fake_leaves = [make_group(lamp(1), lamp(2)), client.fake_leaves[1]]
    return client


def ids(devices):
    return [(d.id, d.group_id) for d in devices]


# ----- listing -----

def test_devices_lists_what_roku_lists(grouped):
    assert ids(grouped.devices()) == [("group-1", None), ("leaf-2", None)]


def test_include_members_lists_each_group_then_its_devices(grouped):
    assert ids(grouped.devices(include_members=True)) == [
        ("group-1", None), ("lamp-1", "group-1"), ("lamp-2", "group-1"), ("leaf-2", None)]


def test_a_group_is_a_device_with_members(grouped):
    group = grouped.devices()[0]
    assert group.is_group and group.type == "group" and group.brightness == 60
    assert ids(group.members) == [("lamp-1", "group-1"), ("lamp-2", "group-1")]
    lamp1 = group.members[0]
    assert not lamp1.is_group and lamp1.members == [] and lamp1.type == "light"
    assert group.members[0] is lamp1  # built once, so changes made through it stay


def test_raw_devices_is_still_roku_response(grouped):
    assert [r["id"] for r in grouped.raw_devices()] == ["group-1", "leaf-2"]


@pytest.mark.parametrize("members", [None, "nope", [None, 5, {"name": "no id"}]])
def test_malformed_members_are_skipped(client, members):
    client.fake_leaves = [make_group(memberLeaves=members)]
    [group] = client.devices()
    assert group.is_group and group.members == []


def test_only_groups_have_members(client):
    client.fake_leaves = [lamp(1, memberLeaves=[lamp(2)])]
    assert ids(client.devices(include_members=True)) == [("lamp-1", None)]
    assert client.devices()[0].members == []


def test_nested_groups_are_walked(client):
    client.fake_leaves = [make_group(make_group(lamp(1), id="group-2", name="Corner"))]
    assert ids(client.devices(include_members=True)) == [
        ("group-1", None), ("group-2", "group-1"), ("lamp-1", "group-2")]


def test_a_top_level_copy_wins_whatever_the_order(client):
    client.fake_leaves = [make_group(lamp(1)), lamp(1)]
    assert ids(client.devices(include_members=True)) == [("group-1", None), ("lamp-1", None)]


def test_each_device_is_listed_once(client):
    # at the top level and in a group, in two groups, and a group inside itself
    client.fake_leaves = [lamp(1), make_group(lamp(1), lamp(2)),
                          make_group(lamp(2), id="group-2", name="Upstairs"),
                          make_group(make_group(id="group-3"), id="group-3", name="Loop")]
    assert ids(client.devices(include_members=True)) == [
        ("lamp-1", None), ("group-1", None), ("lamp-2", "group-1"),
        ("group-2", None), ("group-3", None)]


# ----- lookup -----

def test_devices_in_groups_can_be_looked_up(grouped):
    assert grouped.get("lamp 1").id == "lamp-1"
    assert grouped.get("lamp-2", by_id=True).group_id == "group-1"
    assert grouped.find("2").id == "lamp-2"  # "Lamp 2" is the only name with a 2


def test_a_top_level_entry_still_wins(client):
    # what a name found before devices in groups were searched, it still finds
    client.fake_leaves = [lamp(9, name="Porch Light"),
                          make_group(lamp(1, name="Porch Lamp"), lamp(2, name="Porch Light"))]
    assert client.find("porch").id == "lamp-9"
    assert client.get("porch light").id == "lamp-9"
    assert client.find("lamp").id == "lamp-1"


def test_an_exact_name_in_a_group_beats_a_partial_one_at_the_top(client):
    # typing one lamp's exact name must not switch off the whole room
    client.fake_leaves = [make_group(lamp(1, name="Bedroom Lamp"), name="Bedroom Lamps")]
    assert client.find("bedroom lamp").id == "lamp-1"


def test_devices_in_groups_can_still_be_ambiguous(grouped):
    with pytest.raises(DeviceNotFound, match="several"):
        grouped.find("lamp")


# ----- commands and refresh -----

def test_commands_go_to_the_device_or_the_group(grouped):
    grouped.get("Lamp 1").set_brightness(20)
    grouped.get("Bedroom").turn_off()
    assert grouped.sent == [("lamp-1", "brightness", {"level": 20}),
                            ("group-1", "power", {"power": "off"})]


def test_refresh_keeps_group_id(grouped):
    member = grouped.get("Lamp 2")
    grouped.fake_leaves[0]["memberLeaves"][1]["state"]["brightness"]["level"] = 15
    assert member.refresh().brightness == 15 and member.group_id == "group-1"


# ----- CLI -----

def test_status_shows_a_groups_devices_under_it(client, monkeypatch, capsys):
    client.fake_leaves = [make_group(make_group(lamp(1), id="group-2", name="Corner"), lamp(2)),
                          make_group(lamp(2), id="group-3", name="Upstairs")]
    monkeypatch.setattr(cli, "RokuSmartHome", lambda cookies=None: client)
    cli.main(["status"])
    lines = capsys.readouterr().out.splitlines()
    assert [line[:12].rstrip() for line in lines] == [
        "Bedroom", "  Corner", "    Lamp 1", "  Lamp 2", "Upstairs"]
    assert {line[33:38].strip() for line in lines} == {"group", "light"}  # columns line up
