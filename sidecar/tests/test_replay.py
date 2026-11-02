"""Replay of the hand-made mini race (tests/fixtures/mini_race).

The fixture is a 3-lap race with 6 cars: HAM passes LEC for P3 on lap 2 (a
top-3 change), NOR pits and drops to P6 under a safety car, a red flag and a
VSC, PIA takes P3 from LEC on lap 3, then a P5/P6 swap only (below N = 3).
Official order at the end of lap 1: VER NOR LEC HAM PIA HUL; lap 2: VER HAM
LEC PIA HUL NOR; classification: VER HAM PIA LEC NOR HUL.
"""

import argparse
import io
from pathlib import Path

import pytest

from sidecar import replay as replay_mod
from sidecar.replay import merge, parse_stream, parse_timestamp, read_dir, replay

FIXTURE = Path(__file__).parent / "fixtures" / "mini_race"


def codes(snapshot, n=None):
    return [row["code"] for row in snapshot["grid"][:n]]


def test_parse_timestamp():
    assert parse_timestamp("01:02:03.456") == pytest.approx(3723.456)
    assert parse_timestamp("1.01:02:03.456") == pytest.approx(86400 + 3723.456)
    with pytest.raises(ValueError):
        parse_timestamp("1:2:3")


def test_parse_stream_skips_bad_lines():
    text = '﻿00:00:01.000{"a":1}\r\n00:00:02.000{bad\r\nshort\r\n00:00:03.000{"b":2}\r\n'
    assert parse_stream(text) == [(1.0, {"a": 1}), (3.0, {"b": 2})]


def test_parse_stream_day_prefixed_timestamps():
    """Archives longer than 24 h write `1.01:46:26.209` (seen in 2026 Mexico)."""
    text = '00:10:18.756{"a":1}\r\n1.01:46:26.209{"b":2}\r\n'
    assert parse_stream(text) == [(618.756, {"a": 1}), (pytest.approx(92786.209), {"b": 2})]


def test_merge_orders_by_time_then_topic():
    streams = {
        "TimingData": [(1.0, "t1"), (2.0, "t2")],
        "SessionStatus": [(1.0, "s1")],
        "Other": [(0.5, "o")],
    }
    assert [(t, topic) for t, topic, _ in merge(streams)] == [
        (0.5, "Other"),
        (1.0, "SessionStatus"),
        (1.0, "TimingData"),
        (2.0, "TimingData"),
    ]


def test_samples_match_official_order_and_fit_the_board():
    out = io.StringIO()
    pushed = []
    state = replay(
        merge(read_dir(FIXTURE)),
        samples=[1, 2],
        push=lambda snap, clock: pushed.append(snap),
        out=out,
        clock=iter(range(0, 10**6, 2)).__next__,  # 2 s per call: push every event
    )
    text = out.getvalue()
    assert "lap   1 end: VER NOR LEC HAM PIA HUL" in text
    assert "lap   2 end: VER HAM LEC PIA HUL NOR" in text
    assert "final (FINISHED, lap 3/3): VER HAM PIA LEC NOR HUL" in text
    final = state.snapshot()
    assert final["session"] == "FINISHED"
    assert final["grid"][4]["gap"] == "+3 LAPS"
    assert final["grid"][5]["status"] == "DNF"
    for snap in pushed:
        for row in snap["grid"]:
            for value in row.values():
                assert len(value) <= 15


def test_race_states_and_track_statuses():
    out = io.StringIO()
    pushed = []
    replay(
        merge(read_dir(FIXTURE)),
        push=lambda snap, clock: pushed.append(snap),
        out=out,
        clock=iter(range(0, 10**6, 2)).__next__,
    )
    tracks = []
    for snap in pushed:
        if not tracks or tracks[-1] != snap["track_status"]:
            tracks.append(snap["track_status"])
    # Unknown code "3" kept the last value (GREEN).
    assert tracks == ["GREEN", "SC", "RED", "VSC", "GREEN"]
    sessions = []
    for snap in pushed:
        if not sessions or sessions[-1] != snap["session"]:
            sessions.append(snap["session"])
    assert sessions == ["LIVE", "FINISHED"]
    # Nothing pushed before the start (t < 10 s).
    assert pushed[0]["lap"] == "1"
    # lap follows the lead lap
    assert [s["lap"] for s in pushed][-1] == "3"
    assert "session=LIVE track=GREEN" in out.getvalue()


def test_top3_changes_and_below_n_swap():
    """N = 3 as the plugin applies it: top-3 swaps show, the P5/P6 swap doesn't."""
    pushed = []
    replay(
        merge(read_dir(FIXTURE)),
        push=lambda snap, clock: pushed.append(snap),
        out=io.StringIO(),
        clock=iter(range(0, 10**6, 2)).__next__,
    )
    top3 = []
    for snap in pushed:
        if not top3 or top3[-1] != codes(snap, 3):
            top3.append(codes(snap, 3))
    assert top3 == [
        ["VER", "NOR", "LEC"],
        ["VER", "NOR", "HAM"],
        ["VER", "HAM", "LEC"],
        ["VER", "HAM", "PIA"],
    ]
    fulls = {tuple(codes(s)) for s in pushed}
    # The P5/P6 swap happened (in the full grid) without a top-3 change.
    assert ("VER", "HAM", "PIA", "LEC", "HUL", "NOR") in fulls
    assert ("VER", "HAM", "PIA", "LEC", "NOR", "HUL") in fulls


def test_speed_factor_sleeps():
    sleeps = []
    now = [0.0]

    def clock():
        return now[0]

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    events = [(10.0, "SessionStatus", {"Status": "Started"}), (30.0, "LapCount", {"CurrentLap": 2})]
    replay(events, speed=10, out=io.StringIO(), sleep=sleep, clock=clock)
    # 2 s of waiting, in slices of SLEEP_SLICE_S.
    assert sleeps == [pytest.approx(1.0), pytest.approx(1.0)]


def fake_time():
    now = [0.0]

    def clock():
        return now[0]

    def sleep(seconds):
        now[0] += seconds

    return now, clock, sleep


def test_long_archive_gap_is_cut_short():
    # Mexico 2026: an early "Started", then lights out a day later.
    now, clock, sleep = fake_time()
    events = [
        (0.0, "SessionStatus", {"Status": "Started"}),
        (96000.0, "LapCount", {"CurrentLap": 1}),
        (96020.0, "LapCount", {"CurrentLap": 2}),
    ]
    replay(events, speed=20, out=io.StringIO(), sleep=sleep, clock=clock)
    assert now[0] == pytest.approx((replay_mod.MAX_GAP_S + 20) / 20)


def test_out_of_order_message_does_not_skip_the_gap_twice():
    now, clock, sleep = fake_time()
    events = [
        (0.0, "SessionStatus", {"Status": "Started"}),
        (96000.0, "LapCount", {"CurrentLap": 1}),
        (500.0, "TrackStatus", {"Status": "1"}),  # older than the one before
        (96020.0, "LapCount", {"CurrentLap": 2}),
        (96060.0, "LapCount", {"CurrentLap": 3}),
    ]
    replay(events, speed=1, out=io.StringIO(), sleep=sleep, clock=clock)
    assert now[0] == pytest.approx(replay_mod.MAX_GAP_S + 60)


def test_pushes_while_waiting():
    # A 50 s silence (under MAX_GAP_S) after the mini race: still a push
    # every SLEEP_SLICE_S, so the plugin never sees NO DATA.
    now, clock, sleep = fake_time()
    events = list(merge(read_dir(FIXTURE)))
    last_t = events[-1][0]
    events.append((last_t + 50.0, "LapCount", {"CurrentLap": 3}))
    pushes = []
    replay(
        events,
        speed=1,
        out=io.StringIO(),
        push=lambda snap, at: pushes.append(at),
        sleep=sleep,
        clock=clock,
    )
    quiet_start = now[0] - 50.0
    quiet = [at for at in pushes if at >= quiet_start]
    gaps = [b - a for a, b in zip(quiet, quiet[1:])]
    assert len(quiet) >= 50
    assert max(gaps) <= replay_mod.SLEEP_SLICE_S + 1e-9


def test_laps_window_paces_and_pushes_only_those_laps():
    # Lap 2 of the mini race runs from t=100 s to t=200 s (LapCount 3).
    now, clock, sleep = fake_time()
    out = io.StringIO()
    pushed = []
    replay(
        merge(read_dir(FIXTURE)),
        speed=1,
        samples=[2],
        laps=(2, 2),
        push=lambda snap, at: pushed.append(snap),
        out=out,
        sleep=sleep,
        clock=clock,
    )
    # Lap 1 cost no wall time; lap 2 took its 100 s of race time.
    assert now[0] == pytest.approx(100.0, abs=replay_mod.SLEEP_SLICE_S)
    assert pushed and {s["lap"] for s in pushed} == {"2"}
    assert "SC" in {s["track_status"] for s in pushed}
    # The grid built during lap 1 is there from the first push.
    assert codes(pushed[0], 3) == ["VER", "NOR", "LEC"]
    assert codes(pushed[-1]) == ["VER", "HAM", "LEC", "PIA", "HUL", "NOR"]
    text = out.getvalue()
    assert "lap   2 end: VER HAM LEC PIA HUL NOR" in text
    assert "stopped after lap 2" in text


def test_laps_window_to_the_end_of_the_race():
    pushed = []
    out = io.StringIO()
    replay(
        merge(read_dir(FIXTURE)),
        laps=(3, 9),
        push=lambda snap, at: pushed.append(snap),
        out=out,
        clock=iter(range(0, 10**6, 2)).__next__,
    )
    assert pushed[0]["lap"] == "3"
    assert pushed[-1]["session"] == "FINISHED"
    assert "stopped after" not in out.getvalue()


def test_laps_never_reached_pushes_nothing(caplog):
    pushed = []
    replay(
        merge(read_dir(FIXTURE)),
        speed=1,
        laps=(5, 6),
        push=lambda snap, at: pushed.append(snap),
        out=io.StringIO(),
        sleep=lambda s: pytest.fail("slept before lap 5"),
    )
    assert pushed == []
    assert "lap 5 never reached" in caplog.text


def test_parse_laps():
    assert replay_mod.parse_laps("44-45") == (44, 45)
    assert replay_mod.parse_laps(" 7 ") == (7, 7)
    for bad in ("0-2", "45-44", "x", "1-", "-3", "1-2-3"):
        with pytest.raises(argparse.ArgumentTypeError):
            replay_mod.parse_laps(bad)


def test_main_laps(capsys):
    assert replay_mod.main(["--from-dir", str(FIXTURE), "--print", "--laps", "2-2"]) == 0
    assert "stopped after lap 2" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc:
        replay_mod.main(["--from-dir", str(FIXTURE), "--print", "--laps", "45-44"])
    assert exc.value.code == 2


def test_main_print_from_dir(capsys):
    assert replay_mod.main(["--from-dir", str(FIXTURE), "--print", "--samples", "1"]) == 0
    out = capsys.readouterr().out
    assert "lap   1 end: VER NOR LEC HAM PIA HUL" in out


def test_main_pushes_from_dir(monkeypatch, tmp_path):
    token = tmp_path / "token"
    token.write_text("abc\n")
    monkeypatch.setenv("FIESTABOARD_RECEIVE_URL", "http://fb:3000/api/v1/plugins/f1/receive")
    monkeypatch.setenv("FIESTABOARD_API_TOKEN_FILE", str(token))
    posts = []

    class Resp:
        status_code = 200

        def close(self):
            pass

    def post(self, url, json=None, headers=None, timeout=None, allow_redirects=True):
        posts.append((url, json, headers))
        return Resp()

    monkeypatch.setattr("requests.Session.post", post)
    assert replay_mod.main(["--from-dir", str(FIXTURE)]) == 0
    assert posts
    assert posts[0][2] == {"Authorization": "Bearer abc"}
    assert posts[-1][1]["session"] == "FINISHED"


def test_main_errors(monkeypatch, capsys):
    monkeypatch.delenv("FIESTABOARD_RECEIVE_URL", raising=False)
    monkeypatch.delenv("FIESTABOARD_RECEIVE_URL_FILE", raising=False)
    assert replay_mod.main(["--from-dir", str(FIXTURE)]) == 2  # no push config
    assert replay_mod.main(["--from-dir", str(FIXTURE), "--print", "--samples", "x"]) == 2
    with pytest.raises(SystemExit):
        replay_mod.main(["--print"])


def test_main_download(monkeypatch, capsys):
    """year/gp: credentials -> sign-in -> each topic through FastF1's session, mirror second."""
    from sidecar import live

    monkeypatch.setenv("F1_USERNAME", "me@example.com")
    monkeypatch.setenv("F1_PASSWORD", "pw")
    monkeypatch.setattr(live, "archive_token", lambda: "tok")
    monkeypatch.setattr(replay_mod, "api_path", lambda year, gp: f"/static/{year}/{gp}/")
    gets = []

    class Resp:
        def __init__(self, topic):
            self.status_code = 200
            self.content = (FIXTURE / f"{topic}.jsonStream").read_bytes()

    def get(urls):
        gets.append(urls)
        return Resp(urls[0].rsplit("/", 1)[1].removesuffix(".jsonStream"))

    monkeypatch.setattr(replay_mod, "fastf1_get", get)
    assert replay_mod.main(["2026", "Brazil", "--print", "--samples", "2"]) == 0
    assert gets[0] == ["https://livetiming.formula1.com/static/2026/Brazil/SessionStatus.jsonStream"]
    assert "lap   2 end: VER HAM LEC PIA HUL NOR" in capsys.readouterr().out


def test_fastf1_get_uses_fastf1_session_without_cache(monkeypatch):
    """FastF1's session (token adapter, livetiming headers); its HTTP cache bypassed."""
    from fastf1 import _api
    from fastf1.req import Cache

    seen = {}

    def requests_get(urls, **kwargs):
        seen.update(urls=urls, disabled=Cache._http_tmp_disabled, **kwargs)
        return "resp"

    monkeypatch.setattr(Cache, "requests_get", requests_get)
    assert replay_mod.fastf1_get(["u1", "u2"]) == "resp"
    assert seen["urls"] == ["u1", "u2"] and seen["headers"] == _api.headers
    assert seen["disabled"] is True and Cache._http_tmp_disabled is False


def test_main_download_failure(monkeypatch):
    from sidecar import live

    monkeypatch.setenv("F1_USERNAME", "me@example.com")
    monkeypatch.setenv("F1_PASSWORD", "pw")
    monkeypatch.setattr(live, "archive_token", lambda: "tok")

    class Resp:
        status_code = 403

    monkeypatch.setattr(replay_mod, "fastf1_get", lambda urls: Resp())
    assert replay_mod.main(["--path", "/static/x/", "--print"]) == 1
    with pytest.raises(RuntimeError, match="livetiming.formula1.com HTTP 403, livetiming-mirror.fastf1.dev HTTP 403"):
        replay_mod.download("/static/x/", lambda urls: Resp())

    class Mirror:
        def __init__(self, url):
            self.status_code = 403 if "formula1.com" in url else 200
            self.content = b""

    assert replay_mod.download("/static/x/", lambda urls: Mirror(urls[0]))["SessionStatus"] == []

    def fail():
        raise live.AuthError("bad password")

    monkeypatch.setattr(live, "archive_token", fail)
    assert replay_mod.main(["--path", "/static/x/", "--print"]) == 1


def test_main_other_value_errors_are_load_errors(monkeypatch, caplog):
    from sidecar import live

    monkeypatch.setenv("F1_USERNAME", "me@example.com")
    monkeypatch.setenv("F1_PASSWORD", "pw")

    def unknown(year, gp):
        raise ValueError("no event named Atlantis")

    monkeypatch.setattr(replay_mod, "api_path", unknown)
    monkeypatch.setattr(live, "archive_token", lambda: "tok")
    assert replay_mod.main(["2026", "Atlantis", "--print"]) == 1
    assert "cannot load the race: no event named Atlantis" in caplog.text
    assert "--samples" not in caplog.text
