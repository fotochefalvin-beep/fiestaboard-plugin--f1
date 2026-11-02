"""Jolpica (Ergast-compatible F1 API) client for the F1 Live Standings plugin.

Kept apart from the plugin so the HTTP/JSON shape can be tested with fakes.
Free, no key: ``https://api.jolpi.ca/ergast/f1/current/...``.

* :meth:`JolpicaClient.driver_standings` -- ``/current/driverstandings/``
  -> list of plain dicts (``position``, ``points``, ``wins``, ``code``,
  ``family_name``; raw strings as Jolpica sends them).
* :meth:`JolpicaClient.next_race` -- ``/current/next/`` -> ``{"race_name",
  "start", "has_time"}`` (``start`` an aware UTC datetime) or ``None`` when
  the season has no race left.

Every failure (timeout, connection error, non-200, bad JSON, unexpected
shape) raises :class:`JolpicaError`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

API_BASE = "https://api.jolpi.ca/ergast/f1"
STANDINGS_PATH = "/current/driverstandings/"
NEXT_RACE_PATH = "/current/next/"
HTTP_TIMEOUT_SECONDS = 10
# The whole grid fits in one page (Jolpica's default page is 30).
STANDINGS_LIMIT = 100


class JolpicaError(Exception):
    """A Jolpica request failed or returned something unusable."""


def _parse_start(date_text: Any, time_text: Any) -> tuple[datetime, bool]:
    """``"2026-11-08"`` + ``"17:00:00Z"`` -> aware UTC datetime.

    Returns ``(start, has_time)``; without a time the start is midnight UTC
    of that day and ``has_time`` is False.
    """
    if not isinstance(date_text, str) or not date_text:
        raise JolpicaError("race without a date")
    has_time = isinstance(time_text, str) and bool(time_text.strip())
    text = f"{date_text}T{time_text.strip() if has_time else '00:00:00Z'}"
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        start = datetime.fromisoformat(text)
    except ValueError as exc:
        raise JolpicaError(f"bad race date/time {date_text!r} {time_text!r}") from exc
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return start.astimezone(timezone.utc), has_time


class JolpicaClient:
    """Tiny REST client: one GET per call, parsed into plain dicts."""

    def __init__(self, session: Any = None, timeout: float = HTTP_TIMEOUT_SECONDS):
        self.session = session if session is not None else requests.Session()
        self.timeout = timeout

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = API_BASE + path
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise JolpicaError(f"{path}: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise JolpicaError(f"{path}: HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise JolpicaError(f"{path}: invalid JSON") from exc
        data = body.get("MRData") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            raise JolpicaError(f"{path}: no MRData")
        return data

    def driver_standings(self) -> list[dict[str, str]]:
        """The current driver championship, leader first ([] before round 1)."""
        data = self._get(STANDINGS_PATH, {"limit": STANDINGS_LIMIT})
        table = data.get("StandingsTable")
        if not isinstance(table, dict):
            raise JolpicaError(f"{STANDINGS_PATH}: no StandingsTable")
        lists = table.get("StandingsLists") or []
        if not isinstance(lists, list):
            raise JolpicaError(f"{STANDINGS_PATH}: bad StandingsLists")
        if not lists:
            return []
        rows = lists[0].get("DriverStandings") if isinstance(lists[0], dict) else None
        if not isinstance(rows, list):
            raise JolpicaError(f"{STANDINGS_PATH}: bad DriverStandings")
        standings = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            driver = row.get("Driver") if isinstance(row.get("Driver"), dict) else {}
            standings.append(
                {
                    "position": str(row.get("position") or row.get("positionText") or ""),
                    "points": str(row.get("points") or "0"),
                    "wins": str(row.get("wins") or "0"),
                    "code": str(driver.get("code") or ""),
                    "family_name": str(driver.get("familyName") or ""),
                }
            )
        return standings

    def next_race(self) -> dict[str, Any] | None:
        """The next Grand Prix of the season, or None when the season is over."""
        data = self._get(NEXT_RACE_PATH)
        table = data.get("RaceTable")
        if not isinstance(table, dict):
            raise JolpicaError(f"{NEXT_RACE_PATH}: no RaceTable")
        races = table.get("Races") or []
        if not isinstance(races, list):
            raise JolpicaError(f"{NEXT_RACE_PATH}: bad Races")
        if not races:
            return None
        race = races[0]
        if not isinstance(race, dict):
            raise JolpicaError(f"{NEXT_RACE_PATH}: bad race")
        start, has_time = _parse_start(race.get("date"), race.get("time"))
        return {"race_name": str(race.get("raceName") or ""), "start": start, "has_time": has_time}
