"""When to follow the live feed: the Grand Prix calendar from Jolpica.

- Calendar: `GET {JOLPICA_URL}/current/races/?limit=100` (free, no key),
  refetched every 6 h; when no race of the current season is left, the next
  season (`/{year+1}/races/`) is tried. On failure the last good calendar is
  kept and the fetch retried after 5 minutes.
- Window per Grand Prix: from 15 min before Jolpica's start until the start
  + 4 h (hard cap); the sidecar leaves earlier once the race is FINISHED. A
  race without a start time (rare, early calendar) gets its whole UTC day.
- Token pre-warm: from 30 min before the window opens, the sidecar makes
  sure it holds a valid F1 token, so a failed sign-in shows up in the log
  before the race and not at lights out.

Only each race's top-level `date`/`time` (the Grand Prix itself) is used;
the sprint, qualifying and practice times Jolpica lists alongside are
ignored, so those sessions are never followed.
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
CAP = timedelta(hours=4)
PREWARM = timedelta(minutes=30)
REFRESH_S = 6 * 3600
FAILURE_RETRY_S = 5 * 60
TIMEOUT_S = 10.0


class ScheduleError(Exception):
    """The calendar could not be loaded and nothing is cached."""


@dataclass(frozen=True)
class Race:
    season: str
    round: str
    name: str
    start: datetime
    has_time: bool = True

    @property
    def key(self) -> str:
        return f"{self.season}-{self.round}"

    @property
    def opens(self) -> datetime:
        return self.start - LEAD if self.has_time else self.start

    @property
    def closes(self) -> datetime:
        return self.start + CAP if self.has_time else self.start + timedelta(days=1)

    @property
    def prewarm_at(self) -> datetime:
        return self.opens - PREWARM

    def in_window(self, now: datetime) -> bool:
        return self.opens <= now < self.closes


def parse_races(body: Any) -> list[Race]:
    """Jolpica race-table JSON -> races sorted by start. Bad entries skipped."""
    try:
        items = body["MRData"]["RaceTable"]["Races"]
    except (KeyError, TypeError) as exc:
        raise ValueError("not a Jolpica race table") from exc
    if not isinstance(items, list):
        raise ValueError("not a Jolpica race table")
    races = []
    for item in items:
        try:
            date = str(item["date"])
            time_text = item.get("time")
            has_time = bool(time_text)
            stamp = f"{date}T{time_text}" if has_time else f"{date}T00:00:00Z"
            start = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if start.tzinfo is None:
                start = start.replace(tzinfo=UTC)
            races.append(
                Race(
                    season=str(item["season"]),
                    round=str(item["round"]),
                    name=str(item.get("raceName", "")),
                    start=start.astimezone(UTC),
                    has_time=has_time,
                )
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            log.warning("schedule: skipping a malformed race entry")
    return sorted(races, key=lambda r: r.start)


def pick(races: list[Race], now: datetime, done: Collection[str] = ()) -> Race | None:
    """The race whose window is open now, else the next one (None if none)."""
    for race in races:
        if race.closes > now and race.key not in done:
            return race
    return None


class Schedule:
    """Cached Grand Prix calendar."""

    def __init__(
        self,
        base_url: str,
        session: requests.Session,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._session = session
        self._clock = clock
        self._races: list[Race] | None = None
        self._fetched_at = 0.0
        self._retry_at = 0.0

    def _get(self, path: str) -> list[Race]:
        response = self._session.get(
            f"{self._base}{path}", params={"limit": 100}, timeout=TIMEOUT_S
        )
        if response.status_code != 200:
            raise requests.HTTPError(f"HTTP {response.status_code}")
        return parse_races(response.json())

    def _fetch(self, now: datetime) -> list[Race]:
        races = self._get("/current/races/")
        if not any(r.closes > now for r in races):
            # Season over: the next one, once Jolpica lists it.
            races = races + self._get(f"/{now.year + 1}/races/")
        return races

    def races(self, now: datetime) -> list[Race]:
        clock = self._clock()
        due = self._races is None or clock - self._fetched_at >= REFRESH_S
        if due and clock >= self._retry_at:
            try:
                self._races = self._fetch(now)
                self._fetched_at = clock
                self._retry_at = 0.0
                log.info("schedule: %d race(s) loaded", len(self._races))
            except (requests.RequestException, ValueError) as exc:
                self._retry_at = clock + FAILURE_RETRY_S
                log.warning("schedule: Jolpica unavailable (%s)", type(exc).__name__)
        if self._races is None:
            raise ScheduleError("no calendar yet")
        return self._races

    def current(self, now: datetime, done: Collection[str] = ()) -> Race | None:
        return pick(self.races(now), now, done)
