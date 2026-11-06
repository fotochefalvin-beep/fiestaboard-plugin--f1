"""Jolpica client: parsing and error mapping (HTTP faked)."""

from datetime import datetime, timezone

import pytest
import requests

from .conftest import NEXT, STANDINGS, FakeResponse, FakeSession, driver_row, next_body, standings_body


@pytest.fixture
def jolpica():
    from plugins.f1 import jolpica

    return jolpica


def client_for(jolpica, routes):
    return jolpica.JolpicaClient(session=FakeSession(routes), timeout=3)


def test_driver_standings_parsed(jolpica):
    client = client_for(jolpica, {STANDINGS: FakeResponse(200, standings_body([driver_row(1, "ANT", "Antonelli", 380, 9)]))})
    assert client.driver_standings() == [
        {"position": "1", "points": "380", "wins": "9", "code": "ANT", "family_name": "Antonelli"}
    ]
    assert client.session.calls[0]["timeout"] == 3


def test_driver_standings_tolerates_odd_rows(jolpica):
    body = standings_body([{"positionText": "-", "Driver": "bad"}, "junk"])
    client = client_for(jolpica, {STANDINGS: FakeResponse(200, body)})
    assert client.driver_standings() == [
        {"position": "-", "points": "0", "wins": "0", "code": "", "family_name": ""}
    ]


def test_driver_standings_empty(jolpica):
    client = client_for(jolpica, {STANDINGS: FakeResponse(200, standings_body(None))})
    assert client.driver_standings() == []


@pytest.mark.parametrize(
    "body",
    [
        {"MRData": {}},
        {"MRData": {"StandingsTable": {"StandingsLists": "x"}}},
        {"MRData": {"StandingsTable": {"StandingsLists": [{"DriverStandings": None}]}}},
        {"MRData": "x"},
        [],
    ],
)
def test_driver_standings_bad_shape(jolpica, body):
    client = client_for(jolpica, {STANDINGS: FakeResponse(200, body)})
    with pytest.raises(jolpica.JolpicaError):
        client.driver_standings()


def test_next_race_parsed(jolpica):
    client = client_for(jolpica, {NEXT: FakeResponse(200, next_body())})
    race = client.next_race()
    utc = timezone.utc
    assert race == {
        "race_name": "Brazilian Grand Prix",
        "start": datetime(2026, 11, 8, 17, 0, tzinfo=utc),
        "has_time": True,
        "sessions": [
            ("FP1", datetime(2026, 11, 6, 15, 30, tzinfo=utc), True),
            ("FP2", datetime(2026, 11, 6, 19, 0, tzinfo=utc), True),
            ("FP3", datetime(2026, 11, 7, 14, 30, tzinfo=utc), True),
            ("Q", datetime(2026, 11, 7, 18, 0, tzinfo=utc), True),
            ("RACE", datetime(2026, 11, 8, 17, 0, tzinfo=utc), True),
        ],
    }


def test_next_race_sprint_weekend_sessions(jolpica):
    sessions = {
        "FirstPractice": {"date": "2026-11-06", "time": "14:30:00Z"},
        "SprintQualifying": {"date": "2026-11-06", "time": "18:30:00Z"},
        "SprintShootout": {"date": "2026-11-06", "time": "09:00:00Z"},  # SprintQualifying wins
        "Sprint": {"date": "2026-11-07", "time": "14:00:00Z"},
        "Qualifying": {"date": "2026-11-07"},  # no time
    }
    client = client_for(jolpica, {NEXT: FakeResponse(200, next_body(sessions=sessions))})
    got = [(kind, start.isoformat(), has_time) for kind, start, has_time in client.next_race()["sessions"]]
    assert got == [
        ("FP1", "2026-11-06T14:30:00+00:00", True),
        ("SQ", "2026-11-06T18:30:00+00:00", True),
        ("Q", "2026-11-07T00:00:00+00:00", False),
        ("SPRINT", "2026-11-07T14:00:00+00:00", True),
        ("RACE", "2026-11-08T17:00:00+00:00", True),
    ]


def test_next_race_shootout_and_bad_sessions_skipped(jolpica):
    sessions = {
        "SprintShootout": {"date": "2023-04-29", "time": "08:30:00Z"},
        "Sprint": {"time": "13:30:00Z"},  # no date
        "Qualifying": "soon",  # not an object
        "FirstPractice": {"date": "bad", "time": "x"},
    }
    client = client_for(jolpica, {NEXT: FakeResponse(200, next_body(sessions=sessions))})
    assert [kind for kind, _, _ in client.next_race()["sessions"]] == ["SQ", "RACE"]


def test_next_race_offset_time_and_no_time(jolpica):
    client = client_for(jolpica, {NEXT: FakeResponse(200, next_body(time="19:00:00+02:00"))})
    assert client.next_race()["start"] == datetime(2026, 11, 8, 17, 0, tzinfo=timezone.utc)
    client = client_for(jolpica, {NEXT: FakeResponse(200, next_body(time="17:00:00"))})
    assert client.next_race()["start"] == datetime(2026, 11, 8, 17, 0, tzinfo=timezone.utc)
    client = client_for(jolpica, {NEXT: FakeResponse(200, next_body(time=None))})
    race = client.next_race()
    assert race["start"] == datetime(2026, 11, 8, 0, 0, tzinfo=timezone.utc)
    assert race["has_time"] is False


def test_next_race_season_over(jolpica):
    client = client_for(jolpica, {NEXT: FakeResponse(200, next_body(None))})
    assert client.next_race() is None


@pytest.mark.parametrize(
    "body",
    [
        {"MRData": {}},
        {"MRData": {"RaceTable": {"Races": "x"}}},
        {"MRData": {"RaceTable": {"Races": ["x"]}}},
        {"MRData": {"RaceTable": {"Races": [{"raceName": "X"}]}}},
        {"MRData": {"RaceTable": {"Races": [{"raceName": "X", "date": "soon"}]}}},
    ],
)
def test_next_race_bad_shape(jolpica, body):
    client = client_for(jolpica, {NEXT: FakeResponse(200, body)})
    with pytest.raises(jolpica.JolpicaError):
        client.next_race()


@pytest.mark.parametrize(
    "route",
    [requests.Timeout("t"), requests.ConnectionError("c"), FakeResponse(429, {}), FakeResponse(200, ValueError("x"))],
)
def test_http_failures_raise(jolpica, route):
    client = client_for(jolpica, {NEXT: route})
    with pytest.raises(jolpica.JolpicaError):
        client.next_race()


def test_default_session(jolpica):
    client = jolpica.JolpicaClient()
    assert isinstance(client.session, requests.Session)
    assert client.timeout == jolpica.HTTP_TIMEOUT_SECONDS
