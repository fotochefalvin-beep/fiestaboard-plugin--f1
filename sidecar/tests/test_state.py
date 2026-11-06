"""RaceState: merging live-timing messages into the v1 snapshot."""

from datetime import UTC, datetime, timedelta

import pytest

from sidecar.state import (
    RaceState,
    board_text,
    deep_merge,
    format_diff,
    format_gap,
    parse_clock,
    parse_utc,
    team_name,
)


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
    assert set(snap) == {"v", "session", "session_type", "lap", "total_laps", "track_status", "grid"}
    assert snap["v"] == 1
    assert snap["session_type"] == "RACE"
    assert snap["grid"][0] == {
        "position": "1",
        "code": "VER",
        "name": "VERSTAPPEN",
        "number": "1",
        "team": "RED BULL",
        "gap": "",
        "interval": "",
        "best": "",
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


def test_unknown_session_type():
    with pytest.raises(ValueError):
        RaceState("PRACTICE")


# ── story 3: practice and qualifying ─────────────────────────────────────────

T0 = datetime(2026, 11, 7, 18, 0, tzinfo=UTC)


def drivers3():
    return {
        **driver("1", "VER", "Verstappen", "Red Bull Racing", 1),
        **driver("4", "NOR", "Norris", line=2),
        **driver("16", "LEC", "Leclerc", "Ferrari", 3),
    }


def practice_state(kind="FP1"):
    state = RaceState(kind)
    state.apply("DriverList", drivers3())
    state.apply(
        "TimingData",
        {
            "Lines": {
                "4": {"Position": "1", "BestLapTime": {"Value": "1:16.000", "Lap": 4}, "TimeDiffToFastest": "", "TimeDiffToPositionAhead": ""},
                "1": {"Position": "2", "BestLapTime": {"Value": "1:16.234"}, "TimeDiffToFastest": "+0.234", "TimeDiffToPositionAhead": "+0.234"},
                "16": {"Position": "3", "BestLapTime": {"Value": "1:17.5"}, "TimeDiffToFastest": "1.500", "TimeDiffToPositionAhead": "+1.266"},
            }
        },
    )
    state.apply("SessionStatus", {"Status": "Started"})
    return state


def quali_state(kind="Q"):
    state = RaceState(kind)
    state.apply("DriverList", drivers3())
    stats = [{"TimeDiffToFastest": "", "TimeDifftoPositionAhead": ""}] * 3
    state.apply(
        "TimingData",
        {
            "SessionPart": 1,
            "Lines": {
                n: {"Position": str(p), "KnockedOut": False, "BestLapTimes": [{"Value": ""}, {}, {}], "Stats": stats}
                for n, p in (("1", 1), ("4", 2), ("16", 3))
            },
        },
    )
    state.apply("SessionStatus", {"Status": "Started"})
    return state


def test_practice_timesheet_rows():
    grid = practice_state().snapshot(T0)["grid"]
    assert [(r["code"], r["best"], r["gap"], r["interval"]) for r in grid] == [
        ("NOR", "1:16.000", "", ""),
        ("VER", "1:16.234", "+0.234", "+0.234"),
        ("LEC", "1:17.5", "+1.500", "+1.266"),  # thousandths kept, "+" added
    ]


def test_practice_has_no_laps_and_no_part():
    snap = practice_state("FP2").snapshot(T0)
    assert (snap["lap"], snap["total_laps"]) == ("", "")
    assert snap["session_type"] == "FP2"
    assert "session_part" not in snap
    assert "clock" not in snap  # no ExtrapolatedClock yet


def test_race_types_have_empty_best_and_no_clock():
    state = started_state()
    state.apply("TimingData", {"Lines": {"4": {"BestLapTime": {"Value": "1:20.000"}}}})
    state.apply("ExtrapolatedClock", {"Utc": "2026-11-08T17:00:00Z", "Remaining": "01:00:00", "Extrapolating": True})
    snap = state.snapshot(T0)
    assert {r["best"] for r in snap["grid"]} == {""}
    assert "clock" not in snap and "session_part" not in snap
    sprint = RaceState("SPRINT")
    sprint.apply("DriverList", drivers3())
    sprint.apply("SessionStatus", {"Status": "Started"})
    sprint.apply("ExtrapolatedClock", {"Remaining": "00:30:00"})
    assert "clock" not in sprint.snapshot(T0)


@pytest.mark.parametrize(
    "raw, shown",
    [("+0.234", "+0.234"), ("0.234", "+0.234"), ("+1:02.345", "+1:02.345"), ("", ""), (None, ""), ("odd", "ODD")],
)
def test_diff_formats(raw, shown):
    assert format_diff(raw) == shown


def test_clock_running_is_extrapolated_and_never_negative():
    state = practice_state()
    state.apply("ExtrapolatedClock", {"Utc": "2026-11-07T18:00:00.000Z", "Remaining": "00:17:59", "Extrapolating": True})
    assert state.snapshot(T0)["clock"] == {"remaining": 1079, "running": True}
    assert state.snapshot(T0 + timedelta(seconds=79.5))["clock"] == {"remaining": 999, "running": True}
    assert state.snapshot(T0 + timedelta(hours=1))["clock"] == {"remaining": 0, "running": True}
    # A *now* before the clock's Utc (skewed clocks) never counts up.
    assert state.snapshot(T0 - timedelta(minutes=5))["clock"]["remaining"] == 1079
    assert state.snapshot()["clock"]["remaining"] <= 1079  # default: wall clock


def test_clock_stopped_is_frozen():
    state = practice_state()
    state.apply("ExtrapolatedClock", {"Utc": "2026-11-07T18:00:00Z", "Remaining": "00:17:59", "Extrapolating": True})
    state.apply("ExtrapolatedClock", {"Utc": "2026-11-07T18:06:54Z", "Remaining": "00:11:05", "Extrapolating": False})
    later = T0 + timedelta(minutes=30)
    assert state.snapshot(later)["clock"] == {"remaining": 665, "running": False}
    # Between quali parts the reset arrives without Extrapolating: still stopped.
    state.apply("ExtrapolatedClock", {"Utc": "2026-11-07T18:20:00Z", "Remaining": "00:15:00"})
    assert state.snapshot(later)["clock"] == {"remaining": 900, "running": False}


def test_clock_with_bad_values():
    state = practice_state()
    state.apply("ExtrapolatedClock", {"Remaining": "soon"})
    assert "clock" not in state.snapshot(T0)
    state.apply("ExtrapolatedClock", {"Remaining": "00:10:00", "Extrapolating": True, "Utc": "bad"})
    assert state.snapshot(T0)["clock"] == {"remaining": 600, "running": True}


def test_parse_clock_and_utc():
    assert parse_clock("01:00:00") == 3600
    assert parse_clock("00:00:07.5") == 7
    assert parse_clock(None) is None
    assert parse_utc("2026-10-31T21:48:00.01Z") == datetime(2026, 10, 31, 21, 48, 0, 10000, tzinfo=UTC)
    assert parse_utc("2026-10-31T21:48:00") == datetime(2026, 10, 31, 21, 48, tzinfo=UTC)
    assert parse_utc("") is None and parse_utc(5) is None and parse_utc("x") is None


def test_deep_merge_lists_deltas_and_deleted():
    line = {}
    deep_merge(line, {"BestLapTimes": [{"Value": ""}, {}, {}], "BestLapTime": {"Value": "1:19.0", "Lap": 2}})
    deep_merge(line, {"BestLapTimes": {"1": {"Value": "1:18.5", "Lap": 7}}})
    deep_merge(line, {"BestLapTime": {"Value": "", "_deleted": ["Lap"]}})
    assert line == {
        "BestLapTimes": {"0": {"Value": ""}, "1": {"Value": "1:18.5", "Lap": 7}, "2": {}},
        "BestLapTime": {"Value": ""},
    }
    deep_merge(line, {"BestLapTime": "plain"})  # a plain value replaces a dict
    deep_merge(line, {"BestLapTime": {"Value": "1:17.0"}})  # and back
    assert line["BestLapTime"] == {"Value": "1:17.0"}


def test_quali_part_and_timesheet_from_the_current_part():
    state = quali_state()
    assert state.snapshot(T0)["session_part"] == "Q1"
    state.apply(
        "TimingData",
        {
            "Lines": {
                "1": {"BestLapTimes": {"0": {"Value": "1:17.341", "Lap": 3}}},
                "4": {"BestLapTimes": {"0": {"Value": "1:17.659"}}, "Stats": {"0": {"TimeDiffToFastest": "+0.318", "TimeDifftoPositionAhead": "+0.318"}}},
                # Either casing of the interval key.
                "16": {"BestLapTimes": {"0": {"Value": "1:17.700"}}, "Stats": {"0": {"TimeDiffToFastest": "+0.359", "TimeDiffToPositionAhead": "+0.041"}}},
            }
        },
    )
    grid = state.snapshot(T0)["grid"]
    assert [(r["code"], r["best"], r["gap"], r["interval"]) for r in grid] == [
        ("VER", "1:17.341", "", ""),
        ("NOR", "1:17.659", "+0.318", "+0.318"),
        ("LEC", "1:17.700", "+0.359", "+0.041"),
    ]
    sq = quali_state("SQ")
    sq.apply("TimingData", {"SessionPart": 2})
    assert sq.snapshot(T0)["session_part"] == "SQ2"
    sq.apply("TimingData", {"SessionPart": 9})  # ignored
    assert sq.snapshot(T0)["session_part"] == "SQ2"


def test_knocked_out_keeps_the_last_part_with_a_time():
    state = quali_state()
    state.apply(
        "TimingData",
        {"Lines": {"16": {"BestLapTimes": {"0": {"Value": "1:18.000"}}, "Stats": {"0": {"TimeDiffToFastest": "+0.700", "TimeDifftoPositionAhead": "+0.300"}}}}},
    )
    # Q2 begins: LEC out, the others' part-2 times are reset.
    state.apply(
        "TimingData",
        {
            "SessionPart": 2,
            "Lines": {
                "16": {"KnockedOut": True},
                "1": {"BestLapTimes": {"1": {"Value": ""}}, "BestLapTime": {"Value": "", "_deleted": ["Lap"]}},
            },
        },
    )
    rows = {r["code"]: r for r in state.snapshot(T0)["grid"]}
    assert rows["LEC"]["status"] == "KNOCKED OUT"
    assert (rows["LEC"]["best"], rows["LEC"]["gap"], rows["LEC"]["interval"]) == ("1:18.000", "+0.700", "+0.300")
    assert rows["VER"]["best"] == "" and rows["VER"]["status"] == ""


def test_knocked_out_without_any_time():
    state = quali_state()
    state.apply("TimingData", {"SessionPart": 2, "Lines": {"16": {"KnockedOut": True, "InPit": True}}})
    row = state.snapshot(T0)["grid"][2]
    assert (row["status"], row["best"], row["gap"]) == ("KNOCKED OUT", "", "")


def test_knocked_out_is_quali_only():
    state = practice_state()
    state.apply("TimingData", {"Lines": {"16": {"KnockedOut": True}}})
    assert state.snapshot(T0)["grid"][2]["status"] == ""


def test_quali_part_finished_is_not_the_end():
    state = quali_state()
    state.apply("SessionStatus", {"Status": "Finished"})  # Q1 over
    assert state.snapshot(T0)["session"] == "LIVE"
    # SessionPart moves on while the status still reads Finished (Mexico 2026).
    state.apply("TimingData", {"SessionPart": 2})
    assert not state.finished
    state.apply("SessionStatus", {"Status": "Inactive"})
    state.apply("SessionStatus", {"Status": "Started"})
    state.apply("SessionStatus", {"Status": "Finished"})  # Q2 over
    state.apply("TimingData", {"SessionPart": 3})
    assert not state.finished
    state.apply("SessionStatus", {"Status": "Started"})
    assert not state.finished
    state.apply("SessionStatus", {"Status": "Finished"})  # Q3 over
    assert state.finished and state.snapshot(T0)["session"] == "FINISHED"


@pytest.mark.parametrize("status", ["Finalised", "Ends"])
def test_quali_final_statuses_end_in_any_part(status):
    state = quali_state()
    state.apply("SessionStatus", {"Status": status})
    assert state.finished


def test_quali_full_state_on_reconnect_after_q3():
    """Subscribe completion: SessionStatus may come before TimingData."""
    state = RaceState("Q")
    state.apply("SessionStatus", {"Status": "Finished"})
    assert not state.finished
    state.apply("DriverList", drivers3())
    state.apply("TimingData", {"SessionPart": 3, "Lines": {}})
    assert state.finished
    early = RaceState("Q")
    early.apply("SessionStatus", {"Status": "Finished"})
    early.apply("TimingData", {"SessionPart": 1})
    assert not early.finished


@pytest.mark.parametrize("status", ["Finished", "Finalised", "Ends"])
def test_practice_ends(status):
    state = practice_state()
    state.apply("SessionStatus", {"Status": status})
    assert state.finished


def test_timesheet_values_fit_the_board():
    state = quali_state()
    state.apply("TimingData", {"Lines": {"4": {"BestLapTimes": {"0": {"Value": "1:17.659123456789012"}}, "Stats": {"0": {"TimeDiffToFastest": "+123456789.123456"}}}}})
    for row in state.snapshot(T0)["grid"]:
        for value in row.values():
            assert len(value) <= 15 and value == value.upper() and value.isascii()
