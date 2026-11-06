"""F1 plugin, story 3: every session, timesheets, time left, next session.

One test (or more) per plugin-side row of story 3's I/O & edge-case matrix.
The sidecar builds the timesheet and the session part (its own tests cover
those rows at the source); here: what a push turns into on the board.
"""

import logging
from datetime import datetime, timezone

import pytest

from .conftest import NEXT, FakeResponse, next_body
from .test_live import assert_live_safe, codes, grid, push, snapshot


def session_push(kind="Q", session="LIVE", part=None, clock=None, codes_=None, **extra):
    payload = snapshot(session=session, lap="", total="", **({"codes": codes_} if codes_ else {}))
    payload["session_type"] = kind
    if part is not None:
        payload["session_part"] = part
    if clock is not None:
        payload["clock"] = clock
    payload.update(extra)
    return payload


def running(seconds):
    return {"remaining": seconds, "running": True}


def stopped(seconds):
    return {"remaining": seconds, "running": False}


@pytest.fixture
def plugin(make_plugin):
    return make_plugin()


# ── session, session_part ────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["FP1", "FP2", "FP3", "SQ", "SPRINT", "Q", "RACE"])
def test_session_shown_while_live(plugin, kind):
    push(plugin, session_push(kind))
    data = plugin.fetch_data().data
    assert (data["race_status"], data["session"]) == ("LIVE", kind)


@pytest.mark.parametrize("kind, part", [("Q", "Q2"), ("SQ", "SQ2"), ("Q", "Q3"), ("SQ", "SQ1")])
def test_quali_part(plugin, kind, part):
    push(plugin, session_push(kind, part=part))
    assert plugin.fetch_data().data["session_part"] == part


def test_part_change_within_quali_is_the_same_session(plugin, clock):
    """Q1 -> Q2: no new session (no hold reset), but the grid is republished."""
    push(plugin, session_push(part="Q1", track_status="SC"))
    clock.advance(30)
    swapped_low = grid(["VER", "NOR", "LEC", "HAM", "RUS", "PIA", "SAI", "ALO", "GAS", "OCO"])
    payload = session_push(part="Q2", track_status="")
    payload["grid"] = swapped_low
    push(plugin, payload)
    data = plugin.fetch_data().data
    assert data["session_part"] == "Q2"
    assert data["track_status"] == "SC"  # same session: last status kept
    assert codes(data)[6:8] == ["SAI", "ALO"]  # new part: grid republished
    # Within Q2, a below-N swap alone is ignored again.
    payload = session_push(part="Q2")
    push(plugin, payload)
    assert codes(plugin.fetch_data().data)[6:8] == ["SAI", "ALO"]


def test_new_quali_part_republishes_grid_with_same_top_n(plugin, clock):
    """Q1 -> Q2 with the same top N: the Q1 times must not stay under Q2."""
    q1 = session_push(part="Q1")
    q1["grid"][0]["best"] = "1:17.341"
    push(plugin, q1)
    clock.advance(30)
    q2 = session_push(part="Q2")  # same order, part-2 times reset
    push(plugin, q2)
    data = plugin.fetch_data().data
    assert data["session_part"] == "Q2"
    assert data["grid"][0]["best"] == ""
    assert data["grid"] == q2["grid"]
    # Within the part, a P2 lap that keeps the order is not published.
    q2_later = session_push(part="Q2")
    q2_later["grid"][1]["best"] = "1:17.100"
    push(plugin, q2_later)
    assert plugin.fetch_data().data["grid"][1]["best"] == ""


@pytest.mark.parametrize("kind", ["FP1", "FP2", "FP3", "SQ", "Q"])
def test_new_fastest_lap_republishes_grid_with_same_top_n(plugin, clock, kind):
    """P1 improves (or sets the first time) without an order change: published."""
    part = {"SQ": "SQ1", "Q": "Q1"}.get(kind)
    push(plugin, session_push(kind, part=part))
    first = session_push(kind, part=part)
    first["grid"][0]["best"] = "1:17.341"
    first["grid"][1].update(best="1:17.500", gap="+0.159")
    push(plugin, first)
    assert plugin.fetch_data().data["grid"] == first["grid"]
    clock.advance(30)
    faster = session_push(kind, part=part)
    faster["grid"][0]["best"] = "1:16.900"
    faster["grid"][1].update(best="1:17.500", gap="+0.600")
    push(plugin, faster)
    data = plugin.fetch_data().data
    assert data["grid"][0]["best"] == "1:16.900"
    assert data["grid"][1]["gap"] == "+0.600"
    # The same fastest lap again, P2 improving below it: not published.
    p2 = session_push(kind, part=part)
    p2["grid"][0]["best"] = "1:16.900"
    p2["grid"][1].update(best="1:17.000", gap="+0.100")
    push(plugin, p2)
    assert plugin.fetch_data().data["grid"][1]["gap"] == "+0.600"


def test_race_gap_changes_alone_do_not_republish(plugin):
    push(plugin, session_push("RACE"))
    later = session_push("RACE")
    later["grid"][1]["gap"] = "+9.9"
    push(plugin, later)
    assert plugin.fetch_data().data["grid"][1]["gap"] != "+9.9"


def test_session_shown_during_no_data_and_finished(plugin, clock):
    push(plugin, session_push("FP2", clock=running(600)))
    clock.advance(60)
    data = plugin.fetch_data().data
    assert (data["race_status"], data["session"]) == ("NO DATA", "FP2")
    push(plugin, session_push("FP2", session="FINISHED", clock=stopped(0)))
    data = plugin.fetch_data().data
    assert (data["race_status"], data["session"], data["time_left"]) == ("FINISHED", "FP2", "0:00")


# ── timesheet fields ─────────────────────────────────────────────────────────


def test_timesheet_fields_shown_as_pushed(plugin):
    payload = session_push("Q", part="Q2")
    payload["grid"][0].update(best="1:16.728", gap="", interval="")
    payload["grid"][1].update(best="1:16.962", gap="+0.234", interval="+0.234")
    payload["grid"][9].update(best="1:18.100", gap="+1.372", interval="+0.100", status="KNOCKED OUT")
    push(plugin, payload)
    data = plugin.fetch_data().data
    rows = data["grid"]
    assert (rows[0]["best"], rows[0]["gap"], rows[0]["interval"]) == ("1:16.728", "", "")
    assert (rows[1]["best"], rows[1]["gap"]) == ("1:16.962", "+0.234")
    assert (rows[9]["status"], rows[9]["best"]) == ("KNOCKED OUT", "1:18.100")
    assert (data["lap"], data["total_laps"]) == ("", "")
    assert_live_safe(data)


def test_template_renders_timesheet(plugin, clock):
    from src.templates.engine import TemplateEngine

    payload = session_push("Q", part="Q2", clock=running(754))
    payload["grid"][0]["best"] = "1:16.728"
    push(plugin, payload)
    data = plugin.fetch_data().data
    engine = TemplateEngine()
    text = "{{f1.session_part}} {{f1.time_left}}|{{f1.grid.0.code}} {{f1.grid.0.best}}"
    assert engine.render(text, {"f1": data}) == "Q2 12:34|VER 1:16.728"


# ── time_left ────────────────────────────────────────────────────────────────


def test_time_left_counts_down_between_pushes(plugin, clock):
    push(plugin, session_push("FP1", clock=running(1079)))
    assert plugin.fetch_data().data["time_left"] == "17:59"
    clock.advance(15)  # a board tick, no push in between
    assert plugin.fetch_data().data["time_left"] == "17:44"
    clock.advance(44.5)
    assert plugin.fetch_data().data["time_left"] == "17:00"


def test_time_left_never_below_zero(plugin, clock):
    push(plugin, session_push("FP1", clock=running(5)))
    clock.advance(30)
    assert plugin.fetch_data().data["time_left"] == "0:00"


@pytest.mark.parametrize("seconds, shown", [(3600, "60:00"), (754, "12:34"), (59, "0:59"), (0, "0:00")])
def test_time_left_format(plugin, seconds, shown):
    push(plugin, session_push("FP1", clock=stopped(seconds)))
    assert plugin.fetch_data().data["time_left"] == shown


def test_time_left_stopped_is_frozen(plugin, clock):
    """Red flag, or between quali parts (frozen at the next part's length)."""
    push(plugin, session_push("Q", part="Q1", clock=stopped(900)))
    clock.advance(45)
    data = plugin.fetch_data().data
    assert (data["race_status"], data["time_left"]) == ("LIVE", "15:00")


def test_time_left_frozen_at_last_push_during_no_data(plugin, clock):
    push(plugin, session_push("Q", part="Q3", clock=running(600)))
    clock.advance(90)
    data = plugin.fetch_data().data
    assert (data["race_status"], data["time_left"]) == ("NO DATA", "10:00")


def test_each_push_resets_the_count_down(plugin, clock):
    push(plugin, session_push("FP3", clock=running(600)))
    clock.advance(10)
    push(plugin, session_push("FP3", clock=running(590)))
    clock.advance(5)
    assert plugin.fetch_data().data["time_left"] == "9:45"


@pytest.mark.parametrize("kind", ["RACE", "SPRINT"])
def test_no_clock_in_race_types(plugin, kind):
    payload = snapshot()
    payload["session_type"] = kind
    push(plugin, payload)
    data = plugin.fetch_data().data
    assert data["time_left"] == ""
    assert (data["lap"], data["total_laps"]) == ("34", "57")


# ── next session takes over / different session / hold expires ─────────────


def test_next_session_live_drops_the_hold_at_once(plugin, clock):
    push(plugin, session_push("Q", session="FINISHED", part="Q3", clock=stopped(0)))
    clock.advance(20 * 60)
    race = snapshot(lap="1", track="", codes=["NOR", "VER", "LEC"])
    race["session_type"] = "RACE"
    push(plugin, race)
    data = plugin.fetch_data().data
    assert (data["race_status"], data["session"]) == ("LIVE", "RACE")
    assert data["session_part"] == "" and data["time_left"] == ""
    assert codes(data) == ["NOR", "VER", "LEC"]
    assert data["track_status"] == ""  # nothing carried over from Q


def test_finished_push_of_another_session_restarts_the_hold(plugin, clock):
    push(plugin, session_push("SQ", session="FINISHED", part="SQ3"))
    clock.advance(90 * 60)
    push(plugin, session_push("SPRINT", session="FINISHED", codes_=["LEC", "NOR", "VER"]))
    data = plugin.fetch_data().data
    assert (data["race_status"], data["session"], data["session_part"]) == ("FINISHED", "SPRINT", "")
    assert codes(data) == ["LEC", "NOR", "VER"]
    clock.advance(2 * 3600 - 1)  # 2 h from the SPRINT's FINISHED, not the SQ's
    assert plugin.fetch_data().data["race_status"] == "FINISHED"
    clock.advance(1)
    assert plugin.fetch_data().data["race_status"] == "NEXT RACE"


def test_live_session_replaced_by_another_live_session(plugin, clock):
    """FP3 never sent FINISHED (sidecar left at the cap); Q goes live."""
    push(plugin, session_push("FP3", clock=running(100)))
    clock.advance(30)
    push(plugin, session_push("Q", part="Q1", codes_=["HAM", "RUS", "LEC"]))
    data = plugin.fetch_data().data
    assert (data["session"], data["session_part"]) == ("Q", "Q1")
    assert codes(data) == ["HAM", "RUS", "LEC"]  # first push of a new session


def test_hold_expires_to_next_race(plugin, clock):
    push(plugin, session_push("Q", session="FINISHED", part="Q3", clock=stopped(0)))
    clock.advance(2 * 3600)
    data = plugin.fetch_data().data
    assert data["race_status"] == "NEXT RACE"
    assert (data["session"], data["session_part"], data["time_left"]) == ("", "", "")
    assert data["grid"] == []


def test_heartbeats_of_the_same_finished_session_keep_the_hold(plugin, clock):
    push(plugin, session_push("FP1", session="FINISHED"))
    clock.advance(3600)
    push(plugin, session_push("FP1", session="FINISHED"))
    clock.advance(3600)
    assert plugin.fetch_data().data["race_status"] == "NEXT RACE"


# ── next_session ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "now, expected",
    [
        (datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc), ("FP1", "FRI 16:30")),
        (datetime(2026, 11, 6, 15, 30, tzinfo=timezone.utc), ("FP2", "FRI 20:00")),  # FP1 just started
        (datetime(2026, 11, 6, 21, 0, tzinfo=timezone.utc), ("FP3", "SAT 15:30")),  # between FP2 and FP3
        (datetime(2026, 11, 7, 18, 30, tzinfo=timezone.utc), ("RACE", "SUN 18:00")),
        (datetime(2026, 11, 8, 17, 30, tzinfo=timezone.utc), ("", "")),  # race started, none left
    ],
)
def test_next_session(make_plugin, now_holder, now, expected):
    now_holder["now"] = now
    data = make_plugin().fetch_data().data  # tz Europe/Brussels (UTC+1)
    assert (data["next_session"], data["next_session_time"]) == expected


def test_next_session_without_time_and_sprint_weekend(make_plugin, session, now_holder):
    sessions = {
        "FirstPractice": {"date": "2026-11-06", "time": "14:30:00Z"},
        "SprintQualifying": {"date": "2026-11-06", "time": "18:30:00Z"},
        "Sprint": {"date": "2026-11-07"},
    }
    session.routes[NEXT] = FakeResponse(200, next_body(sessions=sessions))
    now_holder["now"] = datetime(2026, 11, 6, 16, 0, tzinfo=timezone.utc)
    data = make_plugin().fetch_data().data
    assert (data["next_session"], data["next_session_time"]) == ("SQ", "FRI 19:30")
    now_holder["now"] = datetime(2026, 11, 6, 19, 0, tzinfo=timezone.utc)
    data = make_plugin().fetch_data().data
    assert (data["next_session"], data["next_session_time"]) == ("SPRINT", "SAT")


def test_next_session_empty_when_season_over(make_plugin, session):
    session.routes[NEXT] = FakeResponse(200, next_body(None))
    data = make_plugin().fetch_data().data
    assert (data["next_session"], data["next_session_time"]) == ("", "")


def test_next_session_with_an_old_cached_race_dict(make_plugin):
    """A race dict without "sessions" (never happens after 0.3.0) is harmless."""
    plugin = make_plugin()
    plugin.fetch_data()
    plugin._next_race.pop("sessions")
    data = plugin.fetch_data().data
    assert (data["next_session"], data["next_race"]) == ("", "BRAZILIAN")


# ── old payload / bad push ───────────────────────────────────────────────────


def test_old_payload_without_new_fields(plugin):
    push(plugin, snapshot())  # story-2 sidecar: no session_type, part or clock
    data = plugin.fetch_data().data
    assert data["race_status"] == "LIVE"
    assert (data["session"], data["session_part"], data["time_left"]) == ("", "", "")
    assert data["grid"][0]["best"] == ""


@pytest.mark.parametrize("value", [None, ""])
def test_empty_optional_fields_accepted(plugin, value):
    push(plugin, session_push("Q", part=value, clock=None, session_type=value or "Q"))
    assert plugin.fetch_data().data["session_part"] == ""


BAD = [
    pytest.param({"session_type": "PRACTICE"}, id="unknown-type"),
    pytest.param({"session_type": 3}, id="type-not-text"),
    pytest.param({"session_part": "Q4"}, id="unknown-part"),
    pytest.param({"session_part": "SQ2"}, id="part-of-another-type"),
    pytest.param({"session_type": "FP1", "session_part": "Q1"}, id="part-outside-quali"),
    pytest.param({"clock": "12:34"}, id="clock-not-object"),
    pytest.param({"clock": {"remaining": -1, "running": True}}, id="clock-negative"),
    pytest.param({"clock": {"remaining": 3 * 3600 + 1, "running": True}}, id="clock-over-3h"),
    pytest.param({"clock": {"remaining": 12.5, "running": True}}, id="clock-float"),
    pytest.param({"clock": {"remaining": True, "running": True}}, id="clock-bool"),
    pytest.param({"clock": {"remaining": 60}}, id="clock-no-running"),
    pytest.param({"clock": {"remaining": 60, "running": "yes"}}, id="clock-running-text"),
]


@pytest.mark.parametrize("change", BAD)
def test_bad_session_fields_rejected_and_state_unchanged(plugin, change, caplog):
    push(plugin, session_push("Q", part="Q1", clock=running(600)))
    before = plugin.fetch_data().data
    payload = session_push("Q", part="Q1", clock=running(600))
    payload.update(change)
    with caplog.at_level(logging.WARNING, logger="plugins.f1"):
        with pytest.raises(ValueError):
            plugin.receive_payload(payload, {}, raw_body=b"{}")
    assert plugin.fetch_data().data == before
    assert any("rejected push" in r.getMessage() for r in caplog.records)


def test_bad_session_push_is_400_through_core_route(plugin):
    from unittest.mock import patch

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.plugins import routes

    class Registry:
        def get_plugin(self, plugin_id):
            return plugin

        def is_enabled(self, plugin_id):
            return True

    app = FastAPI()
    app.include_router(routes.router)
    with (
        patch.object(routes, "get_plugin_registry", return_value=Registry()),
        patch.object(routes, "_require_plugin_system", return_value=None),
    ):
        client = TestClient(app)
        bad = session_push("Q", clock={"remaining": "soon", "running": True})
        assert client.post("/plugins/f1/receive", json=bad).status_code == 400
        assert client.post("/plugins/f1/receive", json=session_push("Q", part="Q1")).status_code == 200
    assert plugin.fetch_data().data["session"] == "Q"
