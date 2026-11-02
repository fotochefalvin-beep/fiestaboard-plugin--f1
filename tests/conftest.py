"""Test fixtures for the F1 Live Standings plugin.

The tests run against a FiestaBoard core checkout (see README, "Plugin
tests"): ``src.*`` must be importable and this repo linked as ``plugins/f1``.
Host imports happen inside fixtures (lazily), so collecting this file does
not need the core on the path.

No test touches the network: HTTP goes through :class:`FakeSession`.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

MANIFEST_PATH = Path(__file__).resolve().parent.parent / "manifest.json"
STANDINGS = "/current/driverstandings/"
NEXT = "/current/next/"


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeSession:
    """``requests.Session`` stand-in: routes by Jolpica path, records calls.

    ``routes[path]`` is a FakeResponse, an exception (raised), a list of
    them (consumed in order, the last one repeats) or a callable.
    """

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []

    def get(self, url, params=None, timeout=None):
        path = url.split("/ergast/f1", 1)[1]
        self.calls.append({"path": path, "params": params, "timeout": timeout})
        route = self.routes.get(path)
        if isinstance(route, list):
            route = route.pop(0) if len(route) > 1 else route[0]
        if callable(route) and not isinstance(route, FakeResponse):
            route = route()
        if isinstance(route, Exception):
            raise route
        if route is None:
            return FakeResponse(404, {})
        return route

    def paths(self):
        return [call["path"] for call in self.calls]


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def driver_row(position, code, family, points, wins):
    driver = {"driverId": family.lower(), "givenName": "X", "familyName": family}
    if code is not None:
        driver["code"] = code
    return {
        "position": str(position),
        "positionText": str(position),
        "points": str(points),
        "wins": str(wins),
        "Driver": driver,
        "Constructors": [],
    }


def standings_body(rows):
    lists = [{"season": "2026", "round": "19", "DriverStandings": list(rows)}] if rows is not None else []
    return {"MRData": {"StandingsTable": {"season": "2026", "StandingsLists": lists}}}


def next_body(race_name="Brazilian Grand Prix", date="2026-11-08", time="17:00:00Z"):
    if race_name is None:
        return {"MRData": {"RaceTable": {"season": "2026", "Races": []}}}
    race = {"season": "2026", "round": "20", "raceName": race_name, "date": date}
    if time is not None:
        race["time"] = time
    return {"MRData": {"RaceTable": {"season": "2026", "round": "20", "Races": [race]}}}


DEFAULT_ROWS = [
    driver_row(1, "ANT", "Antonelli", 380, 9),
    driver_row(2, "VER", "Verstappen", 261, 3),
    driver_row(3, "HAM", "Hamilton", 259, 1),
]


@pytest.fixture
def manifest():
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def session():
    return FakeSession(
        {
            STANDINGS: FakeResponse(200, standings_body(DEFAULT_ROWS)),
            NEXT: FakeResponse(200, next_body()),
        }
    )


@pytest.fixture
def now_holder():
    """Mutable wall clock: Mon 2 Nov 2026 10:00 UTC (Brazil is on Sunday)."""
    return {"now": datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)}


@pytest.fixture
def make_plugin(manifest, session, clock, now_holder):
    """An enabled plugin with fake clocks, fake HTTP, tz Europe/Brussels."""
    from plugins.f1 import Plugin

    def make(tz="Europe/Brussels"):
        plugin = Plugin(manifest)
        plugin._clock = clock
        plugin._now = lambda: now_holder["now"]
        plugin._timezone_name = lambda: tz
        plugin.session = session
        plugin.config = {"enabled": True}
        plugin.enabled = True
        return plugin

    return make
