"""RaceState: merging live-timing messages into the v1 snapshot."""

import pytest

from sidecar.state import RaceState, board_text, format_gap, team_name


def driver(number, tla, last, team="McLaren", line=None):
    info = {
        "RacingNumber": number,
        "Tla": tla,
        "LastName": last,
        "BroadcastName": f"X {last.upper()}",
        "TeamName": team,
    }
    if line is not None:
        info["Line"] = line
    return {number: info}


def started_state():
    state = RaceState()
    state.apply("DriverList", {**driver("1", "VER", "Verstappen", "Red Bull Racing", 1), **driver("4", "NOR", "Norris", line=2)})
    state.apply(
        "TimingData",
        {
            "Lines": {
                "1": {"Position": "1", "GapToLeader": "LAP 12", "IntervalToPositionAhead": {"Value": "LAP 12"}},
                "4": {"Position": "2", "GapToLeader": "+18.432", "IntervalToPositionAhead": {"Value": "+1.234"}},
            }
        },
    )
    state.apply("SessionStatus", {"Status": "Started"})
    return state


# ── Gap formats ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, shown",
    [
        ("+18.432", "+18.4"),
        ("+18.499", "+18.4"),  # cut, not rounded
        ("18.4", "+18.4"),
        ("+0.0", "+0.0"),
        ("+1:02.345", "+1:02.3"),
        ("+12", "+12"),
        ("1 L", "+1 LAP"),
        ("1L", "+1 LAP"),
        ("+1 LAP", "+1 LAP"),
        ("3 LAPS", "+3 LAPS"),
        ("3L", "+3 LAPS"),
        ("LAP 34", ""),
        ("", ""),
        (None, ""),
        ("weird gap text here", "WEIRD GAP TEXT"),
    ],
)
def test_gap_formats(raw, shown):
    assert format_gap(raw) == shown


def test_leader_has_empty_gap_and_interval():
    row = started_state().snapshot()["grid"][0]
    assert (row["position"], row["gap"], row["interval"]) == ("1", "", "")


def test_p2_gap_and_interval():
    row = started_state().snapshot()["grid"][1]
    assert (row["gap"], row["interval"]) == ("+18.4", "+1.2")


# ── Status ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "flags, status",
    [
        ({"InPit": True}, "PIT"),
        ({"PitOut": True}, "OUT"),
        ({"Retired": True}, "DNF"),
        ({"Retired": True, "InPit": True}, "DNF"),
        ({"InPit": "true"}, "PIT"),
        ({"InPit": False, "PitOut": False, "Retired": False}, ""),
        ({"Stopped": True}, ""),
    ],
)
def test_status(flags, status):
    state = started_state()
    state.apply("TimingData", {"Lines": {"4": flags}})
    assert state.snapshot()["grid"][1]["status"] == status


def test_status_clears_when_flag_drops():
    state = started_state()
    state.apply("TimingData", {"Lines": {"4": {"InPit": True}}})
    state.apply("TimingData", {"Lines": {"4": {"InPit": False}}})
    assert state.snapshot()["grid"][1]["status"] == ""


# ── Track codes ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "code, shown",
    [("1", "GREEN"), ("2", "YELLOW"), ("4", "SC"), ("5", "RED"), ("6", "VSC"), ("7", "VSC")],
)
def test_track_codes(code, shown):
    state = started_state()
    state.apply("TrackStatus", {"Status": code, "Message": "x"})
    assert state.snapshot()["track_status"] == shown


@pytest.mark.parametrize("code", ["3", "", "99", None])
def test_unknown_track_code_keeps_last(code):
    state = started_state()
    state.apply("TrackStatus", {"Status": "4"})
    state.apply("TrackStatus", {"Status": code})
    assert state.snapshot()["track_status"] == "SC"


# ── session, lap, grid ───────────────────────────────────────────────────────


def test_no_snapshot_before_start():
    state = RaceState()
    state.apply("DriverList", driver("1", "VER", "Verstappen", line=1))
    state.apply("SessionStatus", {"Status": "Inactive"})
    assert state.snapshot() is None
    state.apply("SessionStatus", {"Status": "Started"})
    assert state.snapshot()["session"] == "LIVE"


def test_no_snapshot_without_drivers():
    state = RaceState()
    state.apply("SessionStatus", {"Status": "Started"})
    assert state.snapshot() is None


@pytest.mark.parametrize("status", ["Finished", "Finalised", "Ends"])
def test_finished(status):
    state = started_state()
    state.apply("SessionStatus", {"Status": status})
    assert state.snapshot()["session"] == "FINISHED"
    state.apply("SessionStatus", {"Status": ""})  # ignored
    assert state.finished


def test_red_flag_is_not_finished():
    state = started_state()
    state.apply("SessionStatus", {"Status": "Aborted"})
    assert state.snapshot()["session"] == "LIVE"


def test_lap_count():
    state = started_state()
    state.apply("LapCount", {"CurrentLap": 34, "TotalLaps": 57})
    state.apply("LapCount", {"CurrentLap": "35"})
    state.apply("LapCount", {"CurrentLap": "x"})  # ignored
    snap = state.snapshot()
    assert (snap["lap"], snap["total_laps"]) == ("35", "57")


def test_snapshot_shape():
    snap = started_state().snapshot()
    assert set(snap) == {"v", "session", "lap", "total_laps", "track_status", "grid"}
    assert snap["v"] == 1
    assert snap["grid"][0] == {
        "position": "1",
        "code": "VER",
        "name": "VERSTAPPEN",
        "number": "1",
        "team": "RED BULL",
        "gap": "",
        "interval": "",
        "status": "",
    }


def test_grid_sorted_by_timing_position_with_line_fallback():
    state = started_state()
    state.apply("DriverList", driver("16", "LEC", "Leclerc", "Ferrari", 3))
    state.apply("DriverList", driver("44", "HAM", "Hamilton", "Ferrari"))  # no position at all
    state.apply("TimingData", {"Lines": {"1": {"Position": "2"}, "4": {"Position": "1"}}})
    codes = [row["code"] for row in state.snapshot()["grid"]]
    assert codes == ["NOR", "VER", "LEC", "HAM"]
    assert state.snapshot()["grid"][3]["position"] == ""


def test_new_leader_gets_empty_gap():
    state = started_state()
    state.apply("TimingData", {"Lines": {"1": {"Position": "2"}, "4": {"Position": "1"}}})
    assert state.snapshot()["grid"][0]["gap"] == ""


def test_non_driver_keys_and_bad_messages_ignored():
    state = started_state()
    assert state.apply("DriverList", {"_kf": True, "1": "x"}) is True
    assert state.apply("TimingData", {"_kf": True, "Lines": {"_kf": True, "4": "x"}})
    assert state.apply("TimingData", {"Withheld": False}) is True
    assert state.apply("TimingData", "garbage") is False
    assert state.apply("Heartbeat", {"Utc": "2026-11-08T17:00:00Z"}) is False
    assert [row["code"] for row in state.snapshot()["grid"]] == ["VER", "NOR"]


def test_interval_as_plain_string():
    state = started_state()
    state.apply("TimingData", {"Lines": {"4": {"IntervalToPositionAhead": "+0.9"}}})
    assert state.snapshot()["grid"][1]["interval"] == "+0.9"


def test_name_and_code_fallbacks():
    state = started_state()
    state.apply("DriverList", {"27": {"RacingNumber": "27", "BroadcastName": "N HÜLKENBERG", "Line": 3}})
    row = state.snapshot()["grid"][2]
    assert (row["code"], row["name"], row["number"], row["team"]) == ("HUL", "HULKENBERG", "27", "")


@pytest.mark.parametrize(
    "team, shown",
    [
        ("Red Bull Racing", "RED BULL"),
        ("Haas F1 Team", "HAAS"),
        ("Kick Sauber", "SAUBER"),
        ("McLaren", "MCLAREN"),
        ("Some Extremely Long Team Name", "SOME EXTREMELY"),
    ],
)
def test_team_names(team, shown):
    assert team_name(team) == shown


def test_every_value_fits_the_board():
    state = started_state()
    state.apply("DriverList", {"99": {"LastName": "Abcdefghijklmnopqrstuvwxyz", "Tla": "ABCDEFGHIJKLMNOPQ", "Line": 3}})
    snap = state.snapshot()
    for row in snap["grid"]:
        for value in row.values():
            assert len(value) <= 15 and value == value.upper() and value.isascii()


def test_board_text():
    assert board_text("Pérez  jr") == "PEREZ JR"
    assert board_text(None) == ""
