"""Config, notifier and calendar."""

import logging
from datetime import UTC, datetime, timedelta

import pytest
import requests

from sidecar.config import ConfigError, load_config, load_credentials, load_push_config
from sidecar.notify import Notifier, iso_utc
from sidecar.schedule import Race, Schedule, ScheduleError, parse_races, pick

URL = "http://fiestaboard:3000/api/v1/plugins/f1/receive"


@pytest.fixture
def secrets(tmp_path):
    files = {}
    for name, value in {
        "f1_username": "me@example.com",
        "f1_password": "s3cret-pw",
        "fiestaboard_api_token": "fb-token",
    }.items():
        path = tmp_path / name
        path.write_text(value + "\n")
        files[name] = str(path)
    return {
        "F1_USERNAME_FILE": files["f1_username"],
        "F1_PASSWORD_FILE": files["f1_password"],
        "FIESTABOARD_API_TOKEN_FILE": files["fiestaboard_api_token"],
        "FIESTABOARD_RECEIVE_URL": URL,
    }


# ── config ───────────────────────────────────────────────────────────────────


def test_config_from_secret_files(secrets):
    config = load_config(secrets)
    assert (config.f1_username, config.f1_password) == ("me@example.com", "s3cret-pw")
    assert config.fiestaboard_api_token == "fb-token"
    assert config.fiestaboard_receive_url == URL
    assert config.jolpica_url == "https://api.jolpi.ca/ergast/f1"
    assert str(config.heartbeat_file) == "/tmp/heartbeat"
    # Never in repr (logs, tracebacks).
    text = repr(config)
    assert "s3cret-pw" not in text and "me@example.com" not in text and "fb-token" not in text


def test_direct_value_wins(secrets):
    env = {**secrets, "F1_USERNAME": "other@example.com", "JOLPICA_URL": "http://j.local/f1/"}
    config = load_config(env)
    assert config.f1_username == "other@example.com"
    assert config.jolpica_url == "http://j.local/f1"


@pytest.mark.parametrize(
    "change, message",
    [
        ({"F1_PASSWORD_FILE": None}, "F1_PASSWORD is not set"),
        ({"F1_PASSWORD_FILE": "/nonexistent/x"}, "cannot be read"),
        ({"FIESTABOARD_RECEIVE_URL": "ftp://x/api/v1/plugins/f1/receive"}, "http(s)"),
        ({"FIESTABOARD_RECEIVE_URL": "http://x/api/v1/plugins/other/receive"}, "must end in"),
        ({"FIESTABOARD_RECEIVE_URL": "http://x:bad/api/v1/plugins/f1/receive"}, "not a valid URL"),
        ({"F1_USERNAME": "YOUR_F1_EMAIL"}, "placeholder"),
        ({"JOLPICA_URL": "nope"}, "JOLPICA_URL"),
    ],
)
def test_config_errors(secrets, change, message):
    env = dict(secrets)
    for key, value in change.items():
        if value is None:
            env.pop(key)
        else:
            env[key] = value
    with pytest.raises(ConfigError, match=message.replace("(", r"\(").replace(")", r"\)")):
        load_config(env)


def test_empty_secret_file(secrets, tmp_path):
    empty = tmp_path / "empty"
    empty.write_text("  \n")
    with pytest.raises(ConfigError, match="is empty"):
        load_credentials({**secrets, "F1_PASSWORD_FILE": str(empty)})


def test_push_config_needs_no_credentials(secrets):
    env = {k: v for k, v in secrets.items() if not k.startswith("F1_")}
    assert load_push_config(env).fiestaboard_api_token == "fb-token"


# ── notify ───────────────────────────────────────────────────────────────────


class FakeResponse:
    def __init__(self, status):
        self.status_code = status
        self.closed = False

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, result=200):
        self.result = result
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return FakeResponse(self.result)


SNAP = {"v": 1, "session": "LIVE", "lap": "3", "total_laps": "57", "track_status": "GREEN", "grid": []}


def test_push_request():
    session = FakeSession()
    now = datetime(2026, 11, 8, 17, 34, 0, 123000, tzinfo=UTC)
    assert Notifier(URL, "fb-token", session).push(SNAP, now) is True
    url, kwargs = session.calls[0]
    assert url == URL
    assert kwargs["json"] == {**SNAP, "sent_at": "2026-11-08T17:34:00.123Z"}
    assert kwargs["headers"] == {"Authorization": "Bearer fb-token"}
    assert kwargs["timeout"] == 5.0
    assert kwargs["allow_redirects"] is False
    assert "sent_at" not in SNAP  # caller's dict untouched


@pytest.mark.parametrize(
    "result, level",
    [
        (401, logging.ERROR),
        (403, logging.ERROR),
        (500, logging.WARNING),
        (requests.ConnectionError("down"), logging.WARNING),
        (requests.Timeout("slow"), logging.WARNING),
        (UnicodeEncodeError("latin-1", "x", 0, 1, "bad"), logging.WARNING),
    ],
)
def test_push_failures_logged_once_without_token(result, level, caplog):
    notifier = Notifier(URL, "fb-token", FakeSession(result))
    with caplog.at_level(logging.DEBUG, logger="sidecar.notify"):
        assert notifier.push(SNAP) is False
        assert notifier.push(SNAP) is False
    loud = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(loud) == 1 and loud[0].levelno == level
    assert all("fb-token" not in r.getMessage() for r in caplog.records)


def test_recovery_logged(caplog):
    session = FakeSession(500)
    notifier = Notifier(URL, "t", session)
    notifier.push(SNAP)
    session.result = 200
    with caplog.at_level(logging.INFO, logger="sidecar.notify"):
        assert notifier.push(SNAP) is True
    assert any("outcome=ok" in r.getMessage() for r in caplog.records)


def test_iso_utc():
    assert iso_utc(datetime(2026, 1, 1, 1, 0, tzinfo=UTC)) == "2026-01-01T01:00:00.000Z"


# ── schedule ─────────────────────────────────────────────────────────────────


def races_body(*races):
    return {"MRData": {"RaceTable": {"Races": list(races)}}}


def race_json(round_, date, time="17:00:00Z", season="2026", name="Brazilian Grand Prix"):
    item = {"season": season, "round": str(round_), "raceName": name, "date": date}
    if time is not None:
        item["time"] = time
    return item


def test_parse_races_and_window():
    races = parse_races(
        races_body(
            race_json(21, "2026-11-22", "04:00:00Z"),
            race_json(20, "2026-11-08"),
            {"season": "2026", "round": "x"},  # malformed: skipped
            race_json(22, "2026-12-06", None),
        )
    )
    assert [r.round for r in races] == ["20", "21", "22"]
    brazil = races[0]
    assert brazil.key == "2026-20"
    assert brazil.start == datetime(2026, 11, 8, 17, tzinfo=UTC)
    assert brazil.opens == datetime(2026, 11, 8, 16, 45, tzinfo=UTC)
    assert brazil.closes == datetime(2026, 11, 8, 21, tzinfo=UTC)
    assert brazil.prewarm_at == datetime(2026, 11, 8, 16, 15, tzinfo=UTC)
    assert brazil.in_window(datetime(2026, 11, 8, 16, 45, tzinfo=UTC))
    assert not brazil.in_window(datetime(2026, 11, 8, 21, tzinfo=UTC))
    no_time = races[2]
    assert (no_time.opens, no_time.closes) == (
        datetime(2026, 12, 6, tzinfo=UTC),
        datetime(2026, 12, 7, tzinfo=UTC),
    )


def test_parse_races_rejects_garbage():
    with pytest.raises(ValueError):
        parse_races({"nope": 1})
    with pytest.raises(ValueError):
        parse_races({"MRData": {"RaceTable": {"Races": "x"}}})


def test_parse_naive_time_is_utc():
    (race,) = parse_races(races_body(race_json(1, "2026-03-08", "05:00:00")))
    assert race.start == datetime(2026, 3, 8, 5, tzinfo=UTC)


def test_pick():
    races = parse_races(races_body(race_json(20, "2026-11-08"), race_json(21, "2026-11-22")))
    during = datetime(2026, 11, 8, 18, tzinfo=UTC)
    assert pick(races, during).round == "20"
    assert pick(races, during, done={"2026-20"}).round == "21"
    assert pick(races, datetime(2026, 11, 8, 21, tzinfo=UTC)).round == "21"
    assert pick(races, datetime(2026, 12, 1, tzinfo=UTC)) is None


class JolpicaSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, params=None, timeout=None):
        path = url.split("/ergast/f1", 1)[1]
        self.calls.append(path)
        result = self.routes[path]
        if isinstance(result, Exception):
            raise result
        status, body = result

        class R:
            status_code = status

            def json(self):
                return body

        return R()


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_schedule_caches_and_backs_off():
    session = JolpicaSession({"/current/races/": (200, races_body(race_json(20, "2026-11-08")))})
    clock = Clock()
    schedule = Schedule("https://api.jolpi.ca/ergast/f1/", session, clock)
    now = datetime(2026, 11, 2, tzinfo=UTC)
    assert schedule.current(now).round == "20"
    schedule.current(now)
    assert session.calls == ["/current/races/"]
    clock.t = 6 * 3600
    session.routes["/current/races/"] = requests.ConnectionError("down")
    assert schedule.current(now).round == "20"  # last good kept
    clock.t += 60
    schedule.current(now)
    assert len(session.calls) == 2  # backing off
    clock.t += 5 * 60
    session.routes["/current/races/"] = (500, None)
    schedule.current(now)
    assert len(session.calls) == 3


def test_schedule_without_cache_raises():
    session = JolpicaSession({"/current/races/": (200, {"bad": 1})})
    with pytest.raises(ScheduleError):
        Schedule("https://api.jolpi.ca/ergast/f1", session, Clock()).current(datetime.now(UTC))


def test_schedule_next_season_after_the_last_race():
    session = JolpicaSession(
        {
            "/current/races/": (200, races_body(race_json(24, "2026-12-06"))),
            "/2027/races/": (200, races_body(race_json(1, "2027-03-07", season="2027"))),
        }
    )
    schedule = Schedule("https://api.jolpi.ca/ergast/f1", session, Clock())
    race = schedule.current(datetime(2026, 12, 20, tzinfo=UTC))
    assert race.key == "2027-1"
    assert session.calls == ["/current/races/", "/2027/races/"]


def test_race_closes_before_now_is_never_picked():
    race = Race("2026", "1", "X", datetime(2026, 1, 1, tzinfo=UTC))
    assert pick([race], race.closes + timedelta(seconds=1)) is None
