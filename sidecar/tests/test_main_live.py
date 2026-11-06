"""Main loop (window, sign-in, follow, push cadence) and the FastF1 adapter."""

import json
import logging
import time
from datetime import UTC, datetime, timedelta

import pytest

from sidecar import live
from sidecar import main as main_mod
from sidecar.main import FINISH_GRACE_S, HEARTBEAT_S, Pusher, Sidecar
from sidecar.schedule import ScheduleError, Session, parse_races

START = datetime(2026, 11, 8, 17, tzinfo=UTC)
RACE = Session("2026", "20", "RACE", "Brazilian Grand Prix", START)


class FakeNotifier:
    def __init__(self, ok=True):
        self.ok = ok
        self.pushed = []

    def push(self, snapshot, now=None):
        self.pushed.append(snapshot)
        return self.ok


class FakeSchedule:
    def __init__(self, races=(RACE,), error=False):
        self.races = list(races)
        self.error = error

    def current(self, now, done=()):
        if self.error:
            raise ScheduleError("none")
        for race in self.races:
            if race.closes > now and race.key not in done:
                return race
        return None


class Time:
    """Wall clock and monotonic clock moved together by sleep()."""

    def __init__(self, now):
        self.wall = now
        self.mono = 1000.0

    def now(self):
        return self.wall

    def clock(self):
        return self.mono

    def sleep(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds


class FakeClient:
    """A live client whose feed is a script: {tick: [(topic, msg), ...]}."""

    def __init__(self, on_topic, t, script=None, silent_after=None, close_after=None):
        self.on_topic = on_topic
        self.t = t
        self.script = script or {}
        self.silent_after = silent_after
        self.close_after = close_after
        self.ticks = 0
        self.last_message_at = t.clock()
        self.closed = False
        self._connected = True
        self._feed()

    def _feed(self):
        for topic, msg in self.script.get(self.ticks, []):
            self.on_topic(topic, msg)
        if self.silent_after is None or self.ticks < self.silent_after:
            self.last_message_at = self.t.clock()

    def advance(self):
        self.ticks += 1
        if self.close_after is not None and self.ticks >= self.close_after:
            self._connected = False
        self._feed()

    @property
    def connected(self):
        return self._connected

    def close(self):
        self.closed = True


def race_script():
    return {
        0: [
            ("DriverList", {"1": {"Tla": "VER", "LastName": "Verstappen", "Line": 1}, "4": {"Tla": "NOR", "LastName": "Norris", "Line": 2}}),
            ("SessionStatus", {"Status": "Started"}),
            ("LapCount", {"CurrentLap": 1, "TotalLaps": 57}),
        ],
        3: [("TimingData", {"Lines": {"4": {"Position": "1"}, "1": {"Position": "2"}}})],
        40: [("SessionStatus", {"Status": "Finished"})],
    }


def make_sidecar(t, notifier=None, schedule=None, connect=None, sign_in=None, heartbeat=None):
    clients = []

    def default_connect(on_topic):
        client = FakeClient(on_topic, t, race_script())
        clients.append(client)
        return client

    tokens = {"calls": []}

    def default_sign_in(force=False):
        tokens["calls"].append(force)
        return (START + timedelta(days=4)).timestamp()

    def sleep(seconds):
        t.sleep(seconds)
        for client in clients:
            if not client.closed:
                client.advance()

    sidecar = Sidecar(
        schedule=schedule or FakeSchedule(),
        notifier=notifier or FakeNotifier(),
        connect_feed=connect or default_connect,
        sign_in=sign_in or default_sign_in,
        heartbeat_file=heartbeat,
        now=t.now,
        clock=t.clock,
        sleep=sleep,
    )
    return sidecar, clients, tokens


# ── Pusher ───────────────────────────────────────────────────────────────────


def test_pusher_on_change_and_heartbeat():
    notifier = FakeNotifier()
    pusher = Pusher(notifier)
    snap = {"lap": "1"}
    assert pusher.tick(snap, 0.0)
    assert not pusher.tick(dict(snap), 5.0)
    assert pusher.tick({"lap": "2"}, 6.0)  # change: at once
    assert not pusher.tick({"lap": "2"}, 6.0 + HEARTBEAT_S - 0.1)
    assert pusher.tick({"lap": "2"}, 6.0 + HEARTBEAT_S)  # heartbeat
    assert HEARTBEAT_S <= 15


def test_pusher_ignores_a_running_clock_but_not_a_stopped_one():
    notifier = FakeNotifier()
    pusher = Pusher(notifier)
    snap = {"lap": "", "clock": {"remaining": 900, "running": True}}
    assert pusher.tick(snap, 0.0)
    # Counting down alone: no push until the heartbeat, which carries it.
    assert not pusher.tick({**snap, "clock": {"remaining": 899, "running": True}}, 1.0)
    assert not pusher.tick({**snap, "clock": {"remaining": 891, "running": True}}, 9.0)
    assert pusher.tick({**snap, "clock": {"remaining": 890, "running": True}}, HEARTBEAT_S)
    assert notifier.pushed[-1]["clock"]["remaining"] == 890
    # The clock stops (red flag): a change, pushed at once.
    assert pusher.tick({**snap, "clock": {"remaining": 889, "running": False}}, 11.0)
    # A stopped clock that is reset (next part's length) is a change too.
    assert pusher.tick({**snap, "clock": {"remaining": 720, "running": False}}, 12.0)
    # Other changes still push at once while it runs.
    assert pusher.tick({"lap": "", "clock": {"remaining": 700, "running": True}}, 13.0)
    assert pusher.tick({"lap": "x", "clock": {"remaining": 699, "running": True}}, 14.0)


def test_change_key():
    assert main_mod.change_key({"a": 1}) == {"a": 1}
    assert main_mod.change_key({"clock": {"remaining": 5, "running": True}}) == {"clock": {"running": True}}
    assert main_mod.change_key({"clock": {"remaining": 5, "running": False}})["clock"]["remaining"] == 5


def test_pusher_retries_failed_push_sooner():
    notifier = FakeNotifier(ok=False)
    pusher = Pusher(notifier)
    pusher.tick({"lap": "1"}, 0.0)
    assert not pusher.tick({"lap": "1"}, 4.0)
    assert pusher.tick({"lap": "1"}, 5.0)


# ── window logic ─────────────────────────────────────────────────────────────


def test_idle_before_prewarm(tmp_path):
    t = Time(START - timedelta(days=2))
    heartbeat = tmp_path / "hb"
    sidecar, clients, tokens = make_sidecar(t, heartbeat=heartbeat)
    wait = sidecar.step()
    assert wait == pytest.approx((RACE.prewarm_at - t.wall).total_seconds())
    assert tokens["calls"] == [] and clients == []
    assert heartbeat.exists()


def test_prewarm_signs_in_once_before_window():
    t = Time(RACE.prewarm_at + timedelta(minutes=1))
    sidecar, clients, tokens = make_sidecar(t)
    assert sidecar.step() == pytest.approx(29 * 60)
    assert sidecar.step() > 0
    assert tokens["calls"] == [False]  # once
    assert clients == []


def test_prewarm_renews_token_expiring_during_race():
    t = Time(RACE.prewarm_at)
    calls = []

    def sign_in(force=False):
        calls.append(force)
        return START.timestamp() if not force else (START + timedelta(days=4)).timestamp()

    sidecar, _, _ = make_sidecar(t, sign_in=sign_in)
    sidecar.step()
    assert calls == [False, True]


def test_prewarm_failure_retried_after_5_min(caplog):
    t = Time(RACE.prewarm_at)
    calls = []

    def sign_in(force=False):
        calls.append(force)
        raise live.AuthError("Sign-in failed: wrong password")

    sidecar, _, _ = make_sidecar(t, sign_in=sign_in)
    with caplog.at_level(logging.ERROR):
        sidecar.step()
        t.sleep(60)
        sidecar.step()
        t.sleep(4 * 60)
        sidecar.step()
    assert len(calls) == 2
    assert "wrong password" in caplog.text


def test_no_race_and_schedule_errors():
    t = Time(START)
    sidecar, _, _ = make_sidecar(t, schedule=FakeSchedule(races=()))
    assert sidecar.step() == main_mod.IDLE_S
    sidecar, _, _ = make_sidecar(t, schedule=FakeSchedule(error=True))
    assert sidecar.step() == main_mod.SCHEDULE_RETRY_S


def test_run_caps_sleep(monkeypatch):
    t = Time(START - timedelta(days=2))
    sidecar, _, _ = make_sidecar(t)
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 2:
            raise KeyboardInterrupt

    sidecar.sleep = sleep
    with pytest.raises(KeyboardInterrupt):
        sidecar.run()
    assert slept == [main_mod.MAX_SLEEP_S] * 2


# ── follow ───────────────────────────────────────────────────────────────────


def test_follows_race_until_finished_plus_grace():
    t = Time(RACE.opens)
    notifier = FakeNotifier()
    sidecar, clients, _ = make_sidecar(t, notifier=notifier)
    assert sidecar.step() == 0.0
    assert "2026-20-RACE" in sidecar.done
    assert clients[0].closed
    sessions = [p["session"] for p in notifier.pushed]
    assert sessions[0] == "LIVE" and sessions[-1] == "FINISHED"
    orders = [[r["code"] for r in p["grid"]] for p in notifier.pushed]
    assert orders[0] == ["VER", "NOR"]
    assert orders[-1] == ["NOR", "VER"]
    # The swap was pushed on the very next tick.
    assert orders.index(["NOR", "VER"]) == 1
    # Heartbeats keep coming (at most 10 s apart) through the 5 min grace.
    assert len(notifier.pushed) >= FINISH_GRACE_S / HEARTBEAT_S
    # Done: the next step idles until the next race (none here).
    assert sidecar.step() == main_mod.IDLE_S


def test_hard_cap_ends_following():
    t = Time(RACE.closes - timedelta(seconds=20))

    def connect(on_topic):
        script = race_script()
        script.pop(40)  # never finishes
        client = FakeClient(on_topic, t, script)
        clients.append(client)
        return client

    sidecar, clients, _ = make_sidecar(t, connect=connect)
    assert sidecar.step() == 0.0
    assert "2026-20-RACE" in sidecar.done


def quali_script():
    """Q1 and Q2 each send Finished; only Q3's Finished ends the session."""
    drivers = {"1": {"Tla": "VER", "LastName": "Verstappen", "Line": 1}, "4": {"Tla": "NOR", "LastName": "Norris", "Line": 2}}
    return {
        0: [
            ("DriverList", drivers),
            ("TimingData", {"SessionPart": 1, "Lines": {}}),
            ("SessionStatus", {"Status": "Started"}),
            ("ExtrapolatedClock", {"Utc": "2026-11-07T18:00:00Z", "Remaining": "00:18:00", "Extrapolating": True}),
        ],
        20: [("SessionStatus", {"Status": "Finished"})],
        30: [("TimingData", {"SessionPart": 2, "Lines": {"4": {"KnockedOut": True}}})],
        33: [("SessionStatus", {"Status": "Inactive"})],
        40: [("SessionStatus", {"Status": "Started"})],
        60: [("SessionStatus", {"Status": "Finished"})],
        70: [("TimingData", {"SessionPart": 3, "Lines": {}})],
        74: [("SessionStatus", {"Status": "Inactive"})],
        80: [("SessionStatus", {"Status": "Started"})],
        100: [("SessionStatus", {"Status": "Finished"})],
    }


def test_qualifying_stays_connected_until_q3_ends():
    quali = Session("2026", "20", "Q", "Brazilian Grand Prix", START - timedelta(days=1))
    t = Time(quali.opens)
    notifier = FakeNotifier()

    def connect(on_topic):
        client = FakeClient(on_topic, t, quali_script())
        clients.append(client)
        return client

    sidecar, clients, _ = make_sidecar(t, notifier=notifier, schedule=FakeSchedule([quali]), connect=connect)
    assert sidecar.step() == 0.0
    assert len(clients) == 1  # one connection for Q1, Q2 and Q3
    assert "2026-20-Q" in sidecar.done
    parts, sessions = [], []
    for snap in notifier.pushed:
        assert snap["session_type"] == "Q"
        if not parts or parts[-1] != snap["session_part"]:
            parts.append(snap["session_part"])
        if not sessions or sessions[-1] != snap["session"]:
            sessions.append(snap["session"])
    assert parts == ["Q1", "Q2", "Q3"]
    assert sessions == ["LIVE", "FINISHED"]  # never FINISHED after Q1 or Q2
    # Left 5 min after Q3's Finished (tick 100), not after Q1's (tick 20).
    assert t.mono - 1000.0 == pytest.approx(100 + FINISH_GRACE_S, abs=2)
    assert notifier.pushed[-1]["grid"][1]["status"] == "KNOCKED OUT"
    assert "clock" in notifier.pushed[0]


def test_grace_is_disarmed_when_finished_turns_back():
    t = Time(RACE.opens)

    class State:
        def __init__(self):
            self.calls = 0

        @property
        def finished(self):
            self.calls += 1
            return self.calls < 3  # finished for two ticks only

        def snapshot(self, now=None):
            return None

        def apply(self, topic, data):
            pass

    sidecar, _, _ = make_sidecar(t)
    sidecar.state, sidecar.state_key = State(), RACE.key
    t.wall = RACE.closes - timedelta(seconds=FINISH_GRACE_S + 30)
    assert sidecar.follow(RACE) == "cap"  # the early grace never fired


@pytest.mark.parametrize(
    "weekend, expected",
    [
        ("BRAZIL_NORMAL", ["FP1", "FP2", "FP3", "Q", "RACE"]),
        ("SPRINT_WEEKEND", ["FP1", "SQ", "SPRINT", "Q", "RACE"]),
    ],
)
def test_weekend_opens_one_window_per_session_in_order(weekend, expected):
    """Mocked clock over Brazil 2026 and a sprint weekend: one window each."""
    import test_config_notify_schedule as calendar

    sessions = parse_races(calendar.races_body(getattr(calendar, weekend)))
    t = Time(sessions[0].prewarm_at - timedelta(hours=1))
    opened = []

    def connect(on_topic):
        session = sidecar.schedule.current(t.now(), sidecar.done)
        assert session.in_window(t.now())
        opened.append((session.type, sidecar.state.session_type))
        client = FakeClient(on_topic, t, race_script())
        clients.append(client)
        return client

    sidecar, clients, _ = make_sidecar(t, schedule=FakeSchedule(sessions), connect=connect)
    end = sessions[-1].closes + timedelta(hours=1)
    while t.now() < end:
        sidecar.sleep(min(max(sidecar.step(), 0.0), main_mod.MAX_SLEEP_S))
    assert [kind for kind, _ in opened] == expected
    assert [state for _, state in opened] == expected
    assert sidecar.done == {s.key for s in sessions}


def test_silent_feed_stops_heartbeats_then_reconnects_keeping_state():
    t = Time(RACE.opens)
    notifier = FakeNotifier()

    def connect(on_topic):
        if not clients:
            client = FakeClient(on_topic, t, {0: race_script()[0]}, silent_after=5)
        else:
            client = FakeClient(on_topic, t, {0: [("LapCount", {"CurrentLap": 2})]})
        clients.append(client)
        return client

    sidecar, clients, _ = make_sidecar(t, notifier=notifier, connect=connect)
    assert sidecar.step() == main_mod.RECONNECT_S  # dropped after 60 s silence
    first = len(notifier.pushed)
    # Pushes stop once the feed is silent for 30 s: last push before ~t+35 s.
    assert first <= 4
    t.sleep(main_mod.RECONNECT_S)
    t.wall = RACE.closes - timedelta(seconds=3)  # end quickly via the cap
    assert sidecar.step() == 0.0
    assert notifier.pushed[-1]["lap"] == "2"  # same RaceState, merged
    assert [r["code"] for r in notifier.pushed[-1]["grid"]] == ["VER", "NOR"]


def test_closed_connection_reconnects():
    t = Time(RACE.opens)

    def connect(on_topic):
        client = FakeClient(on_topic, t, race_script(), close_after=2)
        clients.append(client)
        return client

    sidecar, clients, _ = make_sidecar(t, connect=connect)
    assert sidecar.step() == main_mod.RECONNECT_S
    assert clients[0].closed


@pytest.mark.parametrize("error", [ConnectionError("x"), live.AuthError("bad")])
def test_connect_failure(error):
    t = Time(RACE.opens)

    def connect(on_topic):
        raise error

    sidecar, _, _ = make_sidecar(t, connect=connect)
    assert sidecar.step() == main_mod.RECONNECT_S


def test_sign_in_failure_in_window_retries_every_5_min(caplog):
    """Wrong password / MFA: logged without secrets, retried; nothing pushed."""
    t = Time(START + timedelta(minutes=10))
    notifier = FakeNotifier()

    def sign_in(force=False):
        raise live.AuthError("Sign-in failed: MFA required")

    sidecar, clients, _ = make_sidecar(t, notifier=notifier, sign_in=sign_in)
    with caplog.at_level(logging.INFO):
        assert sidecar.step() == main_mod.SIGNIN_RETRY_S
    assert clients == [] and notifier.pushed == []
    assert "MFA required" in caplog.text


def test_new_race_gets_fresh_state():
    t = Time(RACE.opens)
    sidecar, _, _ = make_sidecar(t)
    sidecar.state.apply("SessionStatus", {"Status": "Finished"})
    sidecar.state_key = "2026-19-RACE"
    sidecar.step()
    assert sidecar.state_key == "2026-20-RACE"
    assert sidecar.state.session_type == "RACE"


# ── AC: secrets files + expired token -> headless sign-in and connect ───────


def test_expired_token_signs_in_headless_from_secret_files(tmp_path, monkeypatch):
    from fastf1.internals import f1account
    from fastf1.internals.f1account import login as login_mod
    from fastf1.internals.f1account.tokens import SubscriptionToken

    from sidecar.config import load_config

    (tmp_path / "user").write_text("me@example.com\n")
    (tmp_path / "pass").write_text("s3cret\n")
    (tmp_path / "fb").write_text("fb\n")
    env = {
        "F1_USERNAME_FILE": str(tmp_path / "user"),
        "F1_PASSWORD_FILE": str(tmp_path / "pass"),
        "FIESTABOARD_API_TOKEN_FILE": str(tmp_path / "fb"),
        "FIESTABOARD_RECEIVE_URL": "http://fb:3000/api/v1/plugins/f1/receive",
    }
    for name in ("F1_USERNAME", "F1_PASSWORD", "FASTF1_AUTH_METHOD"):
        monkeypatch.delenv(name, raising=False)
    config = load_config(env)

    # An expired token on the (volume) path from the last race weekend.
    token_path = tmp_path / "fastf1" / "f1auth_token.json"
    token_path.parent.mkdir()
    token_path.write_text(json.dumps({"version": 1, "value": "old", "expires_at": time.time() - 3600}))
    monkeypatch.setattr(f1account.token_manager, "_token_path", token_path)
    monkeypatch.setattr(f1account.token_manager, "_token", None)
    # No TTY: FastF1 would raise instead of prompting if the env were empty.
    monkeypatch.setattr("sys.stdin", open("/dev/null"))

    seen = []

    def fake_login(username, password):
        seen.append((username, password))
        return SubscriptionToken(value="new", expires_at=time.time() + 4 * 86400)

    monkeypatch.setattr(login_mod, "login", fake_login)

    live.configure_auth(config.f1_username, config.f1_password)
    t = Time(RACE.opens)
    t.wall = datetime.now(UTC) - (START - RACE.opens)  # window opens "now"
    race = Session("2026", "20", "RACE", "Brazil", t.wall + timedelta(minutes=15))
    connected = []

    def connect(on_topic):
        connected.append(f1account.token_manager.get().value)
        raise ConnectionError("stop here")

    sidecar, _, _ = make_sidecar(t, schedule=FakeSchedule([race]), connect=connect, sign_in=live.ensure_token)
    sidecar.step()
    assert seen == [("me@example.com", "s3cret")]
    assert connected == ["new"]
    assert json.loads(token_path.read_text())["value"] == "new"  # kept for restarts
    assert live.stored_token_expiry() > time.time()


def test_ensure_token_errors(monkeypatch):
    from fastf1.exceptions import F1AuthError
    from fastf1.internals import f1account

    def bad(force_renew=False):
        raise F1AuthError("Sign-in failed: bad credentials")

    monkeypatch.setattr(f1account, "get_token", bad)
    with pytest.raises(live.AuthError, match="bad credentials"):
        live.ensure_token()

    def boom(force_renew=False):
        raise OSError("network")

    monkeypatch.setattr(f1account, "get_token", boom)
    with pytest.raises(live.AuthError, match="OSError"):
        live.ensure_token()


def test_archive_token(monkeypatch):
    from fastf1.internals import f1account

    class Token:
        value = "tok"
        expires_at = 1.0

    monkeypatch.setattr(f1account, "get_token", lambda force_renew=False: Token())
    assert live.archive_token() == "tok"


def test_stored_token_expiry_none(monkeypatch, tmp_path):
    from fastf1.internals import f1account

    monkeypatch.setattr(f1account.token_manager, "_token_path", tmp_path / "none.json")
    monkeypatch.setattr(f1account.token_manager, "_token", None)
    assert live.stored_token_expiry() is None


# ── FastF1 adapter ───────────────────────────────────────────────────────────


def test_dispatch_hands_on_session_info_first():
    from signalrcore.messages.completion_message import CompletionMessage

    got = []
    snapshot = {"SessionStatus": {"Status": "Ends"}, "LapCount": {"CurrentLap": 71}, "SessionInfo": {"Name": "Race"}}
    assert live.dispatch(CompletionMessage("1", snapshot, None), lambda topic, _: got.append(topic)) == 3
    assert got == ["SessionInfo", "SessionStatus", "LapCount"]
    assert "SessionInfo" in live.TOPICS


def test_dispatch_feed_and_completion():
    from signalrcore.messages.completion_message import CompletionMessage

    got = []
    on_topic = lambda topic, data: got.append((topic, data))  # noqa: E731
    assert live.dispatch(["TimingData", {"Lines": {}}, "2026-11-08T17:00:00Z"], on_topic) == 1
    initial = CompletionMessage("1", {"SessionStatus": {"Status": "Started"}, "LapCount": {"CurrentLap": 3}}, None)
    assert live.dispatch(initial, on_topic) == 2
    assert live.dispatch("garbage", on_topic) == 0
    assert live.dispatch([1, 2], on_topic) == 0
    assert got == [
        ("TimingData", {"Lines": {}}),
        ("SessionStatus", {"Status": "Started"}),
        ("LapCount", {"CurrentLap": 3}),
    ]


def test_configure_auth_is_headless(monkeypatch):
    from fastf1.internals.f1account import get_auth_method
    from fastf1.internals.f1account.login import _credentials_from_env_or_prompt

    for name in ("F1_USERNAME", "F1_PASSWORD", "FASTF1_AUTH_METHOD"):
        monkeypatch.delenv(name, raising=False)
    live.configure_auth("me@example.com", "pw")
    assert get_auth_method() == "password"
    assert _credentials_from_env_or_prompt() == ("me@example.com", "pw")


class FakeHub:
    def __init__(self):
        self.handlers = {}
        self.sent = []
        self.stopped = False

    def with_url(self, url, options=None):
        self.url, self.options = url, options
        return self

    def configure_logging(self, level):
        return self

    def build(self):
        return self

    def on_open(self, fn):
        self.handlers["open"] = fn

    def on_close(self, fn):
        self.handlers["close"] = fn

    def on(self, event, fn):
        self.handlers[event] = fn

    def start(self):
        self.handlers["open"]()

    def send(self, method, args, on_invocation=None):
        self.sent.append((method, args))
        self.on_invocation = on_invocation

    def stop(self):
        self.stopped = True


def test_live_client_connects_with_token_and_feeds_callback(monkeypatch):
    import requests
    from fastf1.internals import f1account

    hub = FakeHub()
    monkeypatch.setattr("signalrcore.hub_connection_builder.HubConnectionBuilder", lambda: hub)

    class Options:
        cookies = {"AWSALBCORS": "cookie"}

    monkeypatch.setattr(requests, "options", lambda url, headers=None, timeout=None: Options())
    monkeypatch.setattr(live, "ensure_token", lambda force=False: 1.0)

    class Token:
        value = "tok"

    monkeypatch.setattr(f1account, "get_token", lambda force_renew=False: Token())
    got = []
    client = live.make_client(lambda topic, data: got.append(topic))
    client.connect()
    assert client.connected
    assert hub.options["access_token_factory"]() == "tok"
    assert hub.options["headers"]["Cookie"] == "AWSALBCORS=cookie"
    assert hub.sent == [("Subscribe", [live.TOPICS])]
    hub.handlers["feed"](["LapCount", {"CurrentLap": 2}, "ts"])
    hub.on_invocation("not-a-message")  # ignored
    assert got == ["LapCount"]
    assert client.last_message_at > 0
    hub.handlers["close"]()
    assert not client.connected
    client.close()
    assert hub.stopped


def test_live_client_refuses_without_token(monkeypatch):
    def fail(force=False):
        raise live.AuthError("no")

    monkeypatch.setattr(live, "ensure_token", fail)
    client = live.make_client(lambda topic, data: None)
    with pytest.raises(live.AuthError):
        client.connect()


def test_live_client_connect_timeout(monkeypatch):
    import requests
    from fastf1.internals import f1account

    hub = FakeHub()
    hub.start = lambda: None  # never opens
    monkeypatch.setattr("signalrcore.hub_connection_builder.HubConnectionBuilder", lambda: hub)

    class Options:
        cookies = {}

    monkeypatch.setattr(requests, "options", lambda url, headers=None, timeout=None: Options())
    monkeypatch.setattr(live, "ensure_token", lambda force=False: 1.0)
    monkeypatch.setattr(f1account, "get_token", lambda force_renew=False: None)
    monkeypatch.setattr(live, "CONNECT_TIMEOUT_S", 0.05)
    client = live.make_client(lambda topic, data: None)
    with pytest.raises(ConnectionError):
        client.connect()
    assert hub.stopped


def test_live_client_message_errors_are_contained(caplog):
    def broken(topic, data):
        raise RuntimeError("bug")

    client = live.make_client(broken)
    with caplog.at_level(logging.ERROR):
        client._on_message(["LapCount", {}, "ts"])
    assert "failed to handle" in caplog.text
    client.close()  # no connection yet: harmless


# ── entry point ──────────────────────────────────────────────────────────────


def test_main_config_errors(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "LOUD")
    assert main_mod.main() == 2
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    for name in ("F1_USERNAME", "F1_USERNAME_FILE"):
        monkeypatch.delenv(name, raising=False)
    assert main_mod.main() == 2


def test_main_runs_until_interrupted(monkeypatch, tmp_path):
    monkeypatch.setenv("F1_USERNAME", "me@example.com")
    monkeypatch.setenv("F1_PASSWORD", "pw")
    monkeypatch.setenv("FIESTABOARD_API_TOKEN", "fb")
    monkeypatch.setenv("FIESTABOARD_RECEIVE_URL", "http://fb:3000/api/v1/plugins/f1/receive")
    monkeypatch.setenv("HEARTBEAT_FILE", str(tmp_path / "hb"))

    def run(self):
        raise KeyboardInterrupt

    monkeypatch.setattr(Sidecar, "run", run)
    assert main_mod.main() == 130


def test_module_dispatch(monkeypatch):
    from sidecar import __main__ as entry
    from sidecar import replay

    monkeypatch.setattr(replay, "main", lambda argv: ("replay", argv))
    monkeypatch.setattr(main_mod, "main", lambda: "main")
    assert entry.run(["replay", "2026", "1"]) == ("replay", ["2026", "1"])
    assert entry.run([]) == "main"


def test_connect_feed_uses_live_client(monkeypatch):
    made = []

    class Client:
        def connect(self):
            made.append("connected")

    monkeypatch.setattr(live, "make_client", lambda on_topic: Client())
    assert isinstance(main_mod.connect_feed(lambda t, d: None), Client)
    assert made == ["connected"]


def test_failed_sign_in_in_window_not_retried_before_5_min():
    """run() wakes every 30 s; sign-in must still wait the full 5 minutes."""
    t = Time(START + timedelta(minutes=10))
    calls = []

    def sign_in(force=False):
        calls.append(t.clock())
        raise live.AuthError("Sign-in failed: wrong password")

    sidecar, _, _ = make_sidecar(t, sign_in=sign_in)
    for _ in range(21):  # 10 min 30 s, steps 30 s apart
        sidecar.step()
        t.sleep(main_mod.MAX_SLEEP_S)
    assert len(calls) == 3
    assert calls[1] - calls[0] == main_mod.SIGNIN_RETRY_S
    assert calls[2] - calls[1] == main_mod.SIGNIN_RETRY_S


def test_connect_feed_closes_client_on_failure(monkeypatch):
    class Client:
        closed = False

        def connect(self):
            raise OSError("negotiate failed after start")

        def close(self):
            self.closed = True

    client = Client()
    monkeypatch.setattr(live, "make_client", lambda on_topic: client)
    with pytest.raises(OSError):
        main_mod.connect_feed(lambda t, d: None)
    assert client.closed


# ── 2026-11-06 Brazil FP1: the previous session's state at connect ──────────

# F1 local start + offset of RACE (17:00 UTC) and of the race the week before.
INFO_RACE = {"Name": "Race", "StartDate": "2026-11-08T14:00:00", "GmtOffset": "-03:00:00"}
INFO_PREVIOUS = {"Meeting": {"Name": "Mexico City Grand Prix"}, "Name": "Race", "StartDate": "2026-11-01T14:00:00", "GmtOffset": "-06:00:00"}
DRIVERS = {"1": {"Tla": "VER", "LastName": "Verstappen", "Line": 1}, "4": {"Tla": "NOR", "LastName": "Norris", "Line": 2}}


def stale_script():
    """Connected early: the feed still holds last week's finished race."""
    return {
        0: [
            ("SessionInfo", INFO_PREVIOUS),
            ("DriverList", DRIVERS),
            ("SessionStatus", {"Status": "Ends", "Started": "Finished"}),
            ("LapCount", {"CurrentLap": 71, "TotalLaps": 71}),
            ("TimingData", {"Lines": {"4": {"Position": "1"}, "1": {"Position": "2"}}}),
        ],
        5: [("SessionInfo", {"ArchiveStatus": {"Status": "Generating"}})],  # partial: ignored
        20: [("SessionInfo", INFO_RACE), ("LapCount", {"CurrentLap": 0, "TotalLaps": 71})],
    }


def fresh_script():
    return {
        0: [
            ("SessionInfo", INFO_RACE),
            ("DriverList", DRIVERS),
            ("SessionStatus", {"Status": "Started", "Started": "Started"}),
            ("LapCount", {"CurrentLap": 1, "TotalLaps": 71}),
        ],
    }


def test_previous_session_at_connect_is_ignored_then_resubscribed(caplog):
    caplog.set_level(logging.INFO)
    t = Time(RACE.opens)
    notifier = FakeNotifier()

    def connect(on_topic):
        client = FakeClient(on_topic, t, stale_script() if not clients else fresh_script())
        clients.append(client)
        return client

    sidecar, clients, _ = make_sidecar(t, notifier=notifier, connect=connect)
    # The stale "Ends" never finishes the race and nothing is pushed; when
    # SessionInfo turns to the race, the sidecar subscribes again.
    assert sidecar.step() == main_mod.RECONNECT_S
    assert notifier.pushed == []
    assert "2026-20-RACE" not in sidecar.done
    assert clients[0].closed and clients[0].ticks == 20
    assert "feed still on Mexico City Grand Prix Race" in caplog.text and "subscribing again" in caplog.text
    assert sidecar.state.lap == "" and not sidecar.state.timing  # nothing old survives
    # Second connection: the race itself, on a fresh state.
    t.sleep(main_mod.RECONNECT_S)
    t.wall = RACE.closes - timedelta(seconds=3)  # end quickly via the cap
    assert sidecar.step() == 0.0
    first = notifier.pushed[0]
    assert first["session"] == "LIVE" and first["lap"] == "1"
    assert [r["code"] for r in first["grid"]] == ["VER", "NOR"]  # no stale positions


def test_session_info_of_the_followed_session_changes_nothing():
    t = Time(RACE.opens)
    notifier = FakeNotifier()

    def connect(on_topic):
        script = race_script()
        script[0] = [("SessionInfo", INFO_RACE), *script[0]]
        script[10] = [("SessionInfo", INFO_RACE)]
        clients.append(FakeClient(on_topic, t, script))
        return clients[-1]

    sidecar, clients, _ = make_sidecar(t, notifier=notifier, connect=connect)
    assert sidecar.step() == 0.0  # finished + grace, no resubscribe
    assert "2026-20-RACE" in sidecar.done
    assert notifier.pushed[0]["session"] == "LIVE"


def test_session_without_a_time_skips_the_session_check():
    day = Session("2026", "20", "RACE", "Brazilian Grand Prix", datetime(2026, 11, 8, tzinfo=UTC), has_time=False)
    t = Time(day.opens)
    notifier = FakeNotifier()

    def connect(on_topic):
        script = race_script()
        script[0] = [("SessionInfo", INFO_PREVIOUS), *script[0]]
        clients.append(FakeClient(on_topic, t, script))
        return clients[-1]

    sidecar, clients, _ = make_sidecar(t, notifier=notifier, schedule=FakeSchedule([day]), connect=connect)
    assert sidecar.step() == 0.0
    assert notifier.pushed and not sidecar.wrong_session


def test_connected_during_a_red_flag_pushes_at_once():
    t = Time(RACE.opens)
    notifier = FakeNotifier()

    def connect(on_topic):
        return FakeClient(on_topic, t, {
            0: [
                ("SessionInfo", INFO_RACE),
                ("DriverList", DRIVERS),
                ("SessionStatus", {"Status": "Inactive", "Started": "Started"}),
            ],
        })

    sidecar, _, _ = make_sidecar(t, notifier=notifier, connect=connect)
    sidecar.step()
    assert notifier.pushed[0]["session"] == "LIVE"


def test_still_on_the_previous_session_subscribes_again_every_2_min_then_warns_at_cap(caplog):
    caplog.set_level(logging.INFO)
    t = Time(RACE.closes - timedelta(seconds=150))
    notifier = FakeNotifier()

    def connect(on_topic):
        clients.append(FakeClient(on_topic, t, {0: stale_script()[0]}))
        return clients[-1]

    sidecar, clients, _ = make_sidecar(t, notifier=notifier, connect=connect)
    assert sidecar.step() == main_mod.RECONNECT_S  # resubscribes after 2 min
    assert clients[0].closed and clients[0].ticks == main_mod.WRONG_SESSION_RESUBSCRIBE_S
    t.sleep(main_mod.RECONNECT_S)
    assert sidecar.step() == 0.0  # cap
    assert notifier.pushed == []
    assert "feed still on" in caplog.text
    assert "never turned to it" in caplog.text
