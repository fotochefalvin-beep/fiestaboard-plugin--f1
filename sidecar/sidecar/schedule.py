"""When to follow the live feed: every session of each weekend, from Jolpica.

- Calendar: `GET {JOLPICA_URL}/current/races/?limit=100` (free, no key),
  refetched every 6 h; when no session of the current season is left, the
  next season (`/{year+1}/races/`) is tried. On failure the last good
  calendar is kept and the fetch retried after 5 minutes.
- Sessions per race: `FirstPractice` (FP1), `SecondPractice` (FP2),
  `ThirdPractice` (FP3), `SprintQualifying` or `SprintShootout` (SQ),
  `Sprint` (SPRINT), `Qualifying` (Q) and the race's own `date`/`time`
  (RACE). Missing keys are skipped (a normal weekend has no SQ/SPRINT, a
  sprint weekend no FP2/FP3).
- Window per session (key `season-round-TYPE`): from 15 min before Jolpica's
  start until start + cap (FP/SQ/Q 1.5 h, SPRINT 2 h, RACE 4 h); the sidecar
  leaves earlier once the session is FINISHED. A session without a start
  time (rare, early calendar) gets its whole UTC day.
- Token pre-warm: from 30 min before the window opens, the sidecar makes
  sure it holds a valid F1 token, so a failed sign-in shows up in the log
  before the session and not at its start.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import requests

log = logging.getLogger(__name__)

LEAD = timedelta(minutes=15)
# Hard cap of each session's window, from its start.
CAPS = {
    "FP1": timedelta(hours=1, minutes=30),
    "FP2": timedelta(hours=1, minutes=30),
    "FP3": timedelta(hours=1, minutes=30),
    "SQ": timedelta(hours=1, minutes=30),
    "SPRINT": timedelta(hours=2),
    "Q": timedelta(hours=1, minutes=30),
    "RACE": timedelta(hours=4),
}
SESSION_TYPES = tuple(CAPS)
# Jolpica's per-race session keys -> session type (the race itself is the
# race object's own date/time).
JOLPICA_KEYS = (
    ("FirstPractice", "FP1"),
    ("SecondPractice", "FP2"),
    ("ThirdPractice", "FP3"),
    ("SprintQualifying", "SQ"),
    ("SprintShootout", "SQ"),
    ("Sprint", "SPRINT"),
    ("Qualifying", "Q"),
)
PREWARM = timedelta(minutes=30)
REFRESH_S = 6 * 3600
FAILURE_RETRY_S = 5 * 60
TIMEOUT_S = 10.0


class ScheduleError(Exception):
    """The calendar could not be loaded and nothing is cached."""


@dataclass(frozen=True)
class Session:
    season: str
    round: str
    type: str  # one of SESSION_TYPES
    name: str  # the race name (`Brazilian Grand Prix`)
    start: datetime
    has_time: bool = True

    @property
    def key(self) -> str:
        return f"{self.season}-{self.round}-{self.type}"

    @property
    def opens(self) -> datetime:
        return self.start - LEAD if self.has_time else self.start

    @property
    def closes(self) -> datetime:
        if not self.has_time:
            return self.start + timedelta(days=1)
        return self.start + CAPS[self.type]

    @property
    def prewarm_at(self) -> datetime:
        return self.opens - PREWARM

    def in_window(self, now: datetime) -> bool:
        return self.opens <= now < self.closes


def _start(item: Any) -> tuple[datetime, bool]:
    """A Jolpica `{date, time}` -> (aware UTC start, has_time)."""
    date = str(item["date"])
    time_text = item.get("time")
    has_time = bool(time_text)
    stamp = f"{date}T{time_text}" if has_time else f"{date}T00:00:00Z"
    start = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    return start.astimezone(UTC), has_time


def parse_races(body: Any) -> list[Session]:
    """Jolpica race-table JSON -> every session, sorted by start.

    A malformed race is skipped; so is a malformed session of a good race.
    """
    try:
        items = body["MRData"]["RaceTable"]["Races"]
    except (KeyError, TypeError) as exc:
        raise ValueError("not a Jolpica race table") from exc
    if not isinstance(items, list):
        raise ValueError("not a Jolpica race table")
    sessions = []
    for item in items:
        try:
            season, round_ = str(item["season"]), str(item["round"])
            name = str(item.get("raceName", ""))
            start, has_time = _start(item)
        except (KeyError, TypeError, ValueError, AttributeError):
            log.warning("schedule: skipping a malformed race entry")
            continue
        found = {"RACE": (start, has_time)}
        for key, kind in JOLPICA_KEYS:
            if key not in item or kind in found:
                continue
            try:
                found[kind] = _start(item[key])
            except (KeyError, TypeError, ValueError, AttributeError):
                log.warning("schedule: skipping a malformed %s of %s-%s", key, season, round_)
        for kind, (when, timed) in found.items():
            sessions.append(Session(season, round_, kind, name, when, timed))
    return sorted(sessions, key=lambda s: s.start)


def pick(sessions: list[Session], now: datetime, done: Collection[str] = ()) -> Session | None:
    """The session whose window is open now, else the next one (None if none)."""
    for session in sessions:
        if session.closes > now and session.key not in done:
            return session
    return None


class Schedule:
    """Cached session calendar."""

    def __init__(
        self,
        base_url: str,
        session: requests.Session,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._session = session
        self._clock = clock
        self._races: list[Session] | None = None
        self._fetched_at = 0.0
        self._retry_at = 0.0

    def _get(self, path: str) -> list[Session]:
        response = self._session.get(
            f"{self._base}{path}", params={"limit": 100}, timeout=TIMEOUT_S
        )
        if response.status_code != 200:
            raise requests.HTTPError(f"HTTP {response.status_code}")
        return parse_races(response.json())

    def _fetch(self, now: datetime) -> list[Session]:
        races = self._get("/current/races/")
        if not any(r.closes > now for r in races):
            # Season over: the next one, once Jolpica lists it.
            races = races + self._get(f"/{now.year + 1}/races/")
        return races

    def races(self, now: datetime) -> list[Session]:
        clock = self._clock()
        due = self._races is None or clock - self._fetched_at >= REFRESH_S
        if due and clock >= self._retry_at:
            try:
                self._races = self._fetch(now)
                self._fetched_at = clock
                self._retry_at = 0.0
                log.info("schedule: %d session(s) loaded", len(self._races))
            except (requests.RequestException, ValueError) as exc:
                self._retry_at = clock + FAILURE_RETRY_S
                log.warning("schedule: Jolpica unavailable (%s)", type(exc).__name__)
        if self._races is None:
            raise ScheduleError("no calendar yet")
        return self._races

    def current(self, now: datetime, done: Collection[str] = ()) -> Session | None:
        return pick(self.races(now), now, done)
