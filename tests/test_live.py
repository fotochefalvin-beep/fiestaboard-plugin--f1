"""F1 plugin, story 2: sidecar pushes, live states, top-N publishing.

One test (or more) per row of the story's I/O & edge-case matrix, plus the
manifest. The sidecar does the formatting (gap, status, track codes); its own
tests cover those rows at the source (sidecar/tests/test_state.py). Here the
plugin side of each row is checked: what a push turns into on the board.
"""

import json
import logging

import pytest

from .test_plugin import assert_board_safe


def row(position, code, gap="", interval="", status="", **extra):
    item = {
        "position": str(position),
        "code": code,
        "name": extra.get("name", code + "NAME"),
        "number": extra.get("number", str(position)),
        "team": extra.get("team", "TEAM"),
        "gap": gap,
        "interval": interval,
        "status": status,
    }
    return item


ORDER = ["VER", "NOR", "LEC", "HAM", "RUS", "PIA", "ALO", "SAI", "GAS", "OCO"]


def grid(codes=ORDER):
    return [
        row(i + 1, code, gap="" if i == 0 else f"+{i}.0", interval="" if i == 0 else "+1.0")
        for i, code in enumerate(codes)
    ]


def snapshot(session="LIVE", lap="34", total="57", track="GREEN", codes=ORDER, v=1):
    return {
        "v": v,
        "sent_at": "2026-11-08T17:34:00.000Z",
        "session": session,
        "lap": lap,
        "total_laps": total,
        "track_status": track,
        "grid": grid(codes),
    }


def push(plugin, payload):
    body = json.dumps(payload).encode()
    plugin.receive_payload(payload, {"authorization": "Bearer x"}, raw_body=body)


def codes(data):
    return [item["code"] for item in data["grid"]]


def all_live_values(data):
    for key in ("race_status", "lap", "total_laps", "track_status"):
        yield data[key]
    for item in data["grid"]:
        yield from item.values()


def assert_live_safe(data):
    assert_board_safe(data)
    for value in all_live_values(data):
        assert isinstance(value, str)
        assert len(value) <= 15, value
        assert value == value.upper() and value.isascii()


@pytest.fixture
def plugin(make_plugin):
    return make_plugin()


# ── manifest ─────────────────────────────────────────────────────────────────


def test_manifest_live_variables(manifest):
    from src.plugins.manifest import validate_manifest

    ok, errors = validate_manifest(manifest)
    assert ok, errors
    assert manifest["live_data"] is True
    top_n = manifest["settings_schema"]["properties"]["top_n"]
    assert (top_n["type"], top_n["default"], top_n["minimum"], top_n["maximum"]) == ("integer", 3, 1, 20)
    array = manifest["variables"]["arrays"]["grid"]
    assert array["label_field"] == "code"
    assert array["item_fields"] == [
        "position",
        "code",
        "name",
        "number",
        "team",
        "gap",
        "interval",
        "status",
    ]
    for name in ("lap", "total_laps", "track_status"):
        assert manifest["variables"]["simple"][name]["max_length"] == 15
        assert manifest["max_lengths"][name] == 15
    for field in array["item_fields"]:
        assert manifest["max_lengths"][f"grid.*.{field}"] == 15


def test_live_data_bypasses_core_cache(plugin):
    """live_data: every display tick calls fetch_data (no core cache)."""
    assert plugin.live_data is True
    assert plugin.refresh_seconds is None
    plugin.get_data()
    push(plugin, snapshot())
    assert plugin.get_data().data["race_status"] == "LIVE"


# ── Live ─────────────────────────────────────────────────────────────────────


def test_live_snapshot(plugin, clock):
    push(plugin, snapshot(lap="34", total="57", track="SC"))
    clock.advance(59)
    data = plugin.fetch_data().data
    assert data["race_status"] == "LIVE"
    assert (data["lap"], data["total_laps"], data["track_status"]) == ("34", "57", "SC")
    assert codes(data) == ORDER
    assert data["grid"][0] == row(1, "VER")
    assert data["grid"][1]["gap"] == "+1.0"
    # Jolpica keeps going alongside the live grid.
    assert data["next_race"] == "BRAZILIAN"
    assert [item["code"] for item in data["standings"]] == ["ANT", "VER", "HAM"]
    assert_live_safe(data)


def test_template_renders_grid_leader(plugin):
    from src.templates.engine import TemplateEngine

    push(plugin, snapshot())
    data = plugin.fetch_data().data
    engine = TemplateEngine()
    assert engine.render("{{f1.grid.0.code}} {{f1.lap}}/{{f1.total_laps}}", {"f1": data}) == "VER 34/57"
    assert engine.render("{{f1.grid.1.code}} {{f1.grid.1.gap}}", {"f1": data}) == "NOR +1.0"


def test_next_race_before_any_push(plugin):
    data = plugin.fetch_data().data
    assert data["race_status"] == "NEXT RACE"
    assert data["grid"] == []
    assert (data["lap"], data["total_laps"], data["track_status"]) == ("", "", "")


def test_pushed_values_are_fitted_defensively(plugin):
    payload = snapshot()
    payload["grid"][0].update(name="Hülkenberg the very long name", team="Red Bull Racing Honda RBPT")
    payload["lap"] = 34  # an int is accepted
    payload["grid"][0]["number"] = None  # null -> empty
    push(plugin, payload)
    data = plugin.fetch_data().data
    assert data["grid"][0]["name"] == "HULKENBERG THE"
    assert data["grid"][0]["team"] == "RED BULL RACING"
    assert data["grid"][0]["number"] == ""
    assert data["lap"] == "34"
    assert_live_safe(data)


def test_missing_grid_fields_are_empty(plugin):
    payload = snapshot()
    payload["grid"] = [{"position": "1", "code": "VER"}]
    push(plugin, payload)
    item = plugin.fetch_data().data["grid"][0]
    assert item == {
        "position": "1",
        "code": "VER",
        "name": "",
        "number": "",
        "team": "",
        "gap": "",
        "interval": "",
        "status": "",
    }


# ── Track codes ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("track", ["GREEN", "YELLOW", "SC", "VSC", "RED"])
def test_track_statuses_pass_through(plugin, track):
    push(plugin, snapshot(track=track))
    assert plugin.fetch_data().data["track_status"] == track


@pytest.mark.parametrize("track", ["", "3", "CHEQUERED", None])
def test_unknown_track_status_keeps_last(plugin, track):
    push(plugin, snapshot(track="SC"))
    push(plugin, snapshot(track=track))
    assert plugin.fetch_data().data["track_status"] == "SC"


def test_unknown_track_status_on_first_push_is_empty(plugin):
    push(plugin, snapshot(track="7"))
    assert plugin.fetch_data().data["track_status"] == ""


# ── Gap formats / Status (formatted by the sidecar, shown as pushed) ────────


def test_gap_and_status_values_shown_as_pushed(plugin):
    payload = snapshot()
    payload["grid"][1].update(gap="+18.4", interval="+1.2", status="PIT")
    payload["grid"][2].update(gap="+1 LAP", interval="+1 LAP", status="OUT")
    payload["grid"][3].update(gap="+3 LAPS", interval="+2 LAPS", status="DNF")
    push(plugin, payload)
    grid_data = plugin.fetch_data().data["grid"]
    assert (grid_data[0]["gap"], grid_data[0]["interval"], grid_data[0]["status"]) == ("", "", "")
    assert (grid_data[1]["gap"], grid_data[1]["interval"], grid_data[1]["status"]) == ("+18.4", "+1.2", "PIT")
    assert (grid_data[2]["gap"], grid_data[2]["status"]) == ("+1 LAP", "OUT")
    assert (grid_data[3]["gap"], grid_data[3]["status"]) == ("+3 LAPS", "DNF")


# ── Feed silent / Resume ─────────────────────────────────────────────────────


def test_feed_silent_shows_no_data_and_keeps_last_values(plugin, clock, caplog):
    push(plugin, snapshot(lap="34", track="SC"))
    clock.advance(60)
    with caplog.at_level(logging.WARNING, logger="plugins.f1"):
        data = plugin.fetch_data().data
        again = plugin.fetch_data().data
    assert data["race_status"] == "NO DATA"
    assert (data["lap"], data["total_laps"], data["track_status"]) == ("34", "57", "SC")
    assert codes(data) == ORDER
    assert again == data
    no_data_logs = [r for r in caplog.records if "NO DATA" in r.getMessage()]
    assert len(no_data_logs) == 1  # logged once


def test_resume_after_no_data(plugin, clock, caplog):
    push(plugin, snapshot(lap="34"))
    clock.advance(120)
    assert plugin.fetch_data().data["race_status"] == "NO DATA"
    with caplog.at_level(logging.INFO, logger="plugins.f1"):
        push(plugin, snapshot(lap="36"))
    data = plugin.fetch_data().data
    assert data["race_status"] == "LIVE"
    assert data["lap"] == "36"
    assert any("resumed" in r.getMessage() for r in caplog.records)
    # A second outage is logged again.
    clock.advance(60)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="plugins.f1"):
        assert plugin.fetch_data().data["race_status"] == "NO DATA"
    assert any("NO DATA" in r.getMessage() for r in caplog.records)


def test_resume_keeps_published_grid_when_top_n_unchanged(plugin, clock):
    push(plugin, snapshot())
    clock.advance(90)
    swapped_low = ORDER[:6] + ["SAI", "ALO"] + ORDER[8:]
    push(plugin, snapshot(codes=swapped_low))
    assert codes(plugin.fetch_data().data) == ORDER


def test_silent_past_the_cap_returns_to_next_race(plugin, clock):
    push(plugin, snapshot())
    clock.advance(4 * 3600 - 1)
    assert plugin.fetch_data().data["race_status"] == "NO DATA"
    clock.advance(1)
    data = plugin.fetch_data().data
    assert data["race_status"] == "NEXT RACE"
    assert data["grid"] == [] and data["lap"] == "" and data["track_status"] == ""


# ── Finished ─────────────────────────────────────────────────────────────────


def test_finished_holds_classification_two_hours(plugin, clock):
    push(plugin, snapshot(lap="57"))
    final = ["NOR", "VER", "LEC"] + ORDER[3:]
    push(plugin, snapshot(session="FINISHED", lap="57", codes=final))
    clock.advance(30)
    # Heartbeats of the FINISHED snapshot don't restart the hold.
    push(plugin, snapshot(session="FINISHED", lap="57", codes=final))
    clock.advance(2 * 3600 - 31)
    data = plugin.fetch_data().data
    assert data["race_status"] == "FINISHED"  # not NO DATA, even without pushes
    assert codes(data) == final
    assert data["lap"] == "57"

    clock.advance(1)  # 2 h after the first FINISHED receipt
    data = plugin.fetch_data().data
    assert data["race_status"] == "NEXT RACE"
    assert data["grid"] == []
    assert (data["lap"], data["total_laps"], data["track_status"]) == ("", "", "")


def test_finished_always_publishes_below_n_changes(plugin):
    push(plugin, snapshot())
    final = ORDER[:6] + ["SAI", "ALO"] + ORDER[8:]
    push(plugin, snapshot(session="FINISHED", codes=final))
    assert codes(plugin.fetch_data().data) == final
    # Every FINISHED push publishes (the classification may still settle).
    settled = ORDER[:8] + ["OCO", "GAS"]
    push(plugin, snapshot(session="FINISHED", codes=settled))
    assert codes(plugin.fetch_data().data) == settled


def test_finished_as_first_push(plugin):
    push(plugin, snapshot(session="FINISHED"))
    data = plugin.fetch_data().data
    assert data["race_status"] == "FINISHED"
    assert codes(data) == ORDER


def test_live_push_after_finished_starts_a_new_race(plugin, clock):
    push(plugin, snapshot(session="FINISHED", track="RED"))
    clock.advance(600)
    next_race = ["HAM", "RUS"] + ORDER[2:3]
    push(plugin, snapshot(lap="1", track="", codes=next_race))
    data = plugin.fetch_data().data
    assert data["race_status"] == "LIVE"
    assert codes(data) == next_race
    assert data["track_status"] == ""  # not carried over from the last race
    clock.advance(2 * 3600)  # no stale hold from the old FINISHED
    assert plugin.fetch_data().data["race_status"] == "NO DATA"


def test_push_after_hold_expired_starts_fresh(plugin, clock):
    push(plugin, snapshot(session="FINISHED"))
    clock.advance(2 * 3600)
    push(plugin, snapshot(codes=["LEC", "VER", "NOR"]))
    data = plugin.fetch_data().data
    assert data["race_status"] == "LIVE"
    assert codes(data) == ["LEC", "VER", "NOR"]


# ── Top-N ────────────────────────────────────────────────────────────────────


def test_top_n_change_replaces_whole_grid(plugin):
    push(plugin, snapshot())
    swapped = ["NOR", "VER"] + ORDER[2:]
    payload = snapshot(codes=swapped)
    payload["grid"][5]["gap"] = "+42.0"  # below N: comes along with the swap
    push(plugin, payload)
    data = plugin.fetch_data().data
    assert codes(data) == swapped
    assert data["grid"][5]["gap"] == "+42.0"
    assert data["grid"] == payload["grid"]


def test_two_quick_swaps_end_with_latest_order(plugin):
    push(plugin, snapshot())
    push(plugin, snapshot(codes=["NOR", "VER"] + ORDER[2:]))
    latest = ["NOR", "LEC", "VER"] + ORDER[3:]
    push(plugin, snapshot(codes=latest))
    assert codes(plugin.fetch_data().data) == latest


def test_driver_entering_top_n_from_below(plugin):
    push(plugin, snapshot())
    entered = ["VER", "NOR", "HAM", "LEC"] + ORDER[4:]
    push(plugin, snapshot(codes=entered))
    assert codes(plugin.fetch_data().data) == entered


# ── Lap / flag only ──────────────────────────────────────────────────────────


def test_lap_and_flag_update_without_grid(plugin):
    push(plugin, snapshot(lap="34", track="GREEN"))
    payload = snapshot(lap="35", track="VSC")
    payload["grid"][1]["gap"] = "+9.9"  # gaps change every lap
    push(plugin, payload)
    data = plugin.fetch_data().data
    assert (data["lap"], data["track_status"]) == ("35", "VSC")
    assert data["grid"][1]["gap"] == "+1.0"  # grid stays as published


# ── Below-N only ─────────────────────────────────────────────────────────────


def test_below_n_swap_does_not_change_grid(plugin):
    push(plugin, snapshot())
    swapped = ORDER[:6] + ["SAI", "ALO"] + ORDER[8:]  # P7/P8 swap
    push(plugin, snapshot(codes=swapped))
    assert codes(plugin.fetch_data().data) == ORDER


def test_top_n_setting(plugin):
    plugin.config = {"enabled": True, "top_n": 8}
    push(plugin, snapshot())
    swapped = ORDER[:6] + ["SAI", "ALO"] + ORDER[8:]
    push(plugin, snapshot(codes=swapped))
    assert codes(plugin.fetch_data().data) == swapped


@pytest.mark.parametrize("value", [0, 21, "x", None, True, 2.0])
def test_bad_top_n_falls_back(plugin, value):
    plugin.config = {"enabled": True, "top_n": value}
    expected = 2 if value == 2.0 else 3
    assert plugin._top_n() == expected


def test_top_n_one(plugin):
    plugin.config = {"enabled": True, "top_n": 1}
    push(plugin, snapshot())
    push(plugin, snapshot(codes=["VER", "LEC", "NOR"] + ORDER[3:]))
    assert codes(plugin.fetch_data().data) == ORDER
    push(plugin, snapshot(codes=["LEC", "VER"]))
    assert codes(plugin.fetch_data().data) == ["LEC", "VER"]


# ── Bad push ─────────────────────────────────────────────────────────────────


BAD = [
    pytest.param([1, 2], id="not-an-object"),
    pytest.param("text", id="string"),
    pytest.param({**snapshot(), "v": 2}, id="wrong-v"),
    pytest.param({**snapshot(), "v": True}, id="bool-v"),
    pytest.param({k: v for k, v in snapshot().items() if k != "v"}, id="no-v"),
    pytest.param({**snapshot(), "session": "PRACTICE"}, id="bad-session"),
    pytest.param({**snapshot(), "grid": "VER"}, id="grid-not-list"),
    pytest.param({**snapshot(), "grid": ["VER"]}, id="grid-item-not-object"),
    pytest.param({**snapshot(), "grid": grid([f"D{i:02d}" for i in range(31)])}, id="oversized-grid"),
    pytest.param({**snapshot(), "lap": {"n": 1}}, id="lap-object"),
    pytest.param({**snapshot(), "lap": True}, id="lap-bool"),
]


@pytest.mark.parametrize("payload", BAD)
def test_bad_push_is_rejected_and_state_unchanged(plugin, payload, caplog):
    push(plugin, snapshot(track="SC"))
    before = plugin.fetch_data().data
    with caplog.at_level(logging.WARNING, logger="plugins.f1"):
        with pytest.raises(ValueError):
            plugin.receive_payload(payload, {}, raw_body=b"{}")
    assert plugin.fetch_data().data == before
    assert any("rejected push" in r.getMessage() for r in caplog.records)


def test_oversized_body_is_rejected(plugin):
    with pytest.raises(ValueError, match="too large"):
        plugin.receive_payload(snapshot(), {}, raw_body=b"x" * (64 * 1024 + 1))
    assert plugin.fetch_data().data["race_status"] == "NEXT RACE"


def test_bad_push_through_core_route_is_400(plugin):
    """Core's receive route maps the plugin's ValueError to HTTP 400."""
    from unittest.mock import patch

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.plugins import routes

    class Registry:
        def get_plugin(self, plugin_id):
            return plugin if plugin_id == "f1" else None

        def is_enabled(self, plugin_id):
            return True

    app = FastAPI()
    app.include_router(routes.router)
    with (
        patch.object(routes, "get_plugin_registry", return_value=Registry()),
        patch.object(routes, "_require_plugin_system", return_value=None),
    ):
        client = TestClient(app)
        url = "/plugins/f1/receive"  # the router has no prefix; core mounts it at /api/v1
        assert client.post(url, content=b"not json").status_code == 400
        assert client.post(url, json={**snapshot(), "v": 9}).status_code == 400
        assert plugin.fetch_data().data["race_status"] == "NEXT RACE"
        assert client.post(url, json=snapshot()).status_code == 200
    assert plugin.fetch_data().data["race_status"] == "LIVE"


# ── No race data ever ────────────────────────────────────────────────────────


def test_no_push_after_start_stays_next_race(plugin, clock, now_holder):
    from datetime import datetime, timezone

    now_holder["now"] = datetime(2026, 11, 8, 18, 30, tzinfo=timezone.utc)  # after the start
    clock.advance(3 * 3600)
    data = plugin.fetch_data().data
    assert data["race_status"] == "NEXT RACE"
    assert data["grid"] == []


# ── Jolpica down during a race ───────────────────────────────────────────────


def test_live_grid_available_without_jolpica(make_plugin, session):
    import requests

    session.routes = {
        "/current/driverstandings/": requests.ConnectionError("down"),
        "/current/next/": requests.ConnectionError("down"),
    }
    plugin = make_plugin()
    assert plugin.fetch_data().available is False
    push(plugin, snapshot())
    result = plugin.fetch_data()
    assert result.available
    assert result.data["race_status"] == "LIVE"
    assert result.data["standings"] == [] and result.data["next_race"] == ""
