"""Replay of the hand-made mini race (tests/fixtures/mini_race).

The fixture is a 3-lap race with 6 cars: HAM passes LEC for P3 on lap 2 (a
top-3 change), NOR pits and drops to P6 under a safety car, a red flag and a
VSC, PIA takes P3 from LEC on lap 3, then a P5/P6 swap only (below N = 3).
Official order at the end of lap 1: VER NOR LEC HAM PIA HUL; lap 2: VER HAM
LEC PIA HUL NOR; classification: VER HAM PIA LEC NOR HUL.
"""

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


def test_parse_stream_skips_bad_lines():
    text = '﻿00:00:01.000{"a":1}\r\n00:00:02.000{bad\r\nshort\r\n00:00:03.000{"b":2}\r\n'
    assert parse_stream(text) == [(1.0, {"a": 1}), (3.0, {"b": 2})]


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
    assert sleeps == [pytest.approx(2.0)]


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
    """year/gp: credentials -> token -> authenticated download of each topic."""
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

    def get(self, url, headers=None, timeout=None, allow_redirects=True):
        gets.append((url, headers, allow_redirects))
        return Resp(url.rsplit("/", 1)[1].removesuffix(".jsonStream"))

    monkeypatch.setattr("requests.Session.get", get)
    assert replay_mod.main(["2026", "Brazil", "--print", "--samples", "2"]) == 0
    assert gets[0][0] == "https://livetiming.formula1.com/static/2026/Brazil/SessionStatus.jsonStream"
    assert all(h == {"Authorization": "Bearer tok"} and not redirects for _, h, redirects in gets)
    assert "lap   2 end: VER HAM LEC PIA HUL NOR" in capsys.readouterr().out


def test_main_download_failure(monkeypatch):
    from sidecar import live

    monkeypatch.setenv("F1_USERNAME", "me@example.com")
    monkeypatch.setenv("F1_PASSWORD", "pw")
    monkeypatch.setattr(live, "archive_token", lambda: "tok")

    class Resp:
        status_code = 403

    monkeypatch.setattr("requests.Session.get", lambda self, url, **kw: Resp())
    assert replay_mod.main(["--path", "/static/x/", "--print"]) == 1

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
