"""F1 Live Standings plugin for FiestaBoard.

Story 1: the driver championship and the next Grand Prix, from Jolpica
(``api.jolpi.ca``, free, no key). Template variables (plugin id ``f1``),
every value upper-case ASCII and at most 15 characters (Vestaboard Note):

* ``race_status`` -- ``NEXT RACE`` (``LIVE``/``FINISHED``/``NO DATA`` come
  with the live grid, story 2).
* ``next_race`` -- Jolpica ``raceName`` without "Grand Prix"
  (``BRAZILIAN``, ``MEXICO CITY``); empty when the season is over.
* ``next_race_time`` -- the start in FiestaBoard's general timezone
  (``SUN 18:00``; UTC when unset); empty when the season is over.
* ``standings`` -- array, 0-indexed, leader first
  (``{{f1.standings.0.code}}``), fields ``position``, ``code``, ``name``,
  ``points``, ``wins``; ``[]`` before the first race of a season.

Request economy: each endpoint is cached for an hour (two requests an hour
normally). Once the cached race's start has passed, ``/current/next/`` is
refetched on the next update and then at most every 15 minutes until
Jolpica moves on. After a failure the endpoint backs off 5 minutes while the
last good values stay on the board; with nothing cached yet it is retried on
the next update.

There is no default page: Alvin builds his layouts from the variables.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from datetime import datetime, timezone, tzinfo
from typing import Any, Callable, Dict, List, Optional

from src.plugins.base import PluginBase, PluginResult

try:
    from . import jolpica
except ImportError:  # pragma: no cover - imported outside its package, e.g. by
    # pytest collecting this repo's root as a top-level module.
    import jolpica  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

PLUGIN_ID = "f1"
MAX_VARIABLE_LENGTH = 15
STATUS_NEXT_RACE = "NEXT RACE"

# Normal refresh of each Jolpica endpoint.
REFRESH_SECONDS = 3600
# Once the cached race has started: refetch /current/next/ at most this often
# until it returns a later race.
STARTED_REFRESH_SECONDS = 15 * 60
# Back-off after a failed request (when good values are cached).
FAILURE_BACKOFF_SECONDS = 5 * 60

WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
_GRAND_PRIX_RE = re.compile(r"\bgrand\s+prix\b", re.IGNORECASE)


# ── text helpers ─────────────────────────────────────────────────────────────


def board_text(value: Any) -> str:
    """Upper-case ASCII for the board: ``Hülkenberg`` -> ``HULKENBERG``."""
    text = unicodedata.normalize("NFKD", str(value if value is not None else ""))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"\s+", " ", text).strip()
    return text.upper()


def fit(value: Any, limit: int = MAX_VARIABLE_LENGTH) -> str:
    """:func:`board_text` cut to *limit* characters."""
    return board_text(value)[:limit].rstrip()


def short_race_name(race_name: Any) -> str:
    """``Mexico City Grand Prix`` -> ``MEXICO CITY`` (at most 15 chars)."""
    text = board_text(_GRAND_PRIX_RE.sub(" ", str(race_name or "")))
    return fit(text or race_name)


def driver_code(code: Any, family_name: Any) -> str:
    """Jolpica's three-letter code, else the first 3 letters of the family name."""
    text = board_text(code)
    if text:
        return text[:MAX_VARIABLE_LENGTH]
    letters = re.sub(r"[^A-Z]", "", board_text(family_name))
    return letters[:3]


def format_points(points: Any) -> str:
    """``"380"`` -> ``380``, ``"12.5"`` -> ``12.5``, ``"380.0"`` -> ``380``."""
    text = str(points if points is not None else "").strip()
    try:
        number = float(text)
    except ValueError:
        return fit(text)
    if number.is_integer():
        return fit(str(int(number)))
    return fit(text)


def format_start(start: datetime, tz: tzinfo, has_time: bool = True) -> str:
    """``SUN 18:00`` in *tz* (just ``SUN`` when Jolpica has no start time)."""
    if not has_time:
        # Midnight UTC is only a placeholder: keep Jolpica's calendar day.
        return WEEKDAYS[start.weekday()]
    local = start.astimezone(tz)
    return f"{WEEKDAYS[local.weekday()]} {local:%H:%M}"


def standings_rows(standings: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Parsed Jolpica rows -> the ``standings`` array items."""
    return [
        {
            "position": fit(row.get("position")),
            "code": driver_code(row.get("code"), row.get("family_name")),
            "name": fit(row.get("family_name")),
            "points": format_points(row.get("points")),
            "wins": fit(row.get("wins")),
        }
        for row in standings
    ]


def general_timezone_name() -> str:
    """FiestaBoard's general timezone setting (``""`` when unset/unreadable)."""
    try:
        from src.config import Config

        return str(Config.GENERAL_TIMEZONE or "")
    except Exception as exc:  # pragma: no cover - host config unavailable
        logger.debug("F1: could not read the general timezone: %s", exc)
        return ""


def resolve_timezone(name: str) -> tzinfo:
    """IANA name -> tzinfo; UTC when empty or unknown."""
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:
            logger.warning("F1: unknown timezone %r, using UTC", name)
    return timezone.utc


# ── plugin ───────────────────────────────────────────────────────────────────


class F1Plugin(PluginBase):
    """Driver championship and next Grand Prix as board variables."""

    def __init__(self, manifest: Dict[str, Any]):
        super().__init__(manifest)
        # Injectable for tests: monotonic clock, wall clock, HTTP session.
        self._clock: Callable[[], float] = time.monotonic
        self._now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
        self._timezone_name: Callable[[], str] = general_timezone_name
        self.session: Any = None
        self._client: Optional[jolpica.JolpicaClient] = None
        self._lock = threading.Lock()

        self._standings: Optional[List[Dict[str, str]]] = None  # None = never fetched
        self._standings_at = 0.0
        self._standings_retry_at = 0.0

        self._next_loaded = False
        self._next_race: Optional[Dict[str, Any]] = None  # None after load = season over
        self._next_at = 0.0
        self._next_wall: Optional[datetime] = None  # wall time of the last good fetch
        self._next_retry_at = 0.0

    @property
    def plugin_id(self) -> str:
        return PLUGIN_ID

    def _get_client(self) -> jolpica.JolpicaClient:
        if self._client is None:
            self._client = jolpica.JolpicaClient(session=self.session)
        return self._client

    # ── refresh decisions ────────────────────────────────────────────────

    def _standings_due(self, clock: float) -> bool:
        if clock < self._standings_retry_at:
            return False
        return self._standings is None or clock - self._standings_at >= REFRESH_SECONDS

    def _next_due(self, clock: float, now: datetime) -> bool:
        if clock < self._next_retry_at:
            return False
        if not self._next_loaded:
            return True
        age = clock - self._next_at
        if age >= REFRESH_SECONDS:
            return True
        race = self._next_race
        if race is not None and race["start"] <= now:
            # First update after the start: refetch at once; then at most
            # every STARTED_REFRESH_SECONDS until Jolpica lists a later race.
            fetched_before_start = self._next_wall is None or self._next_wall < race["start"]
            return fetched_before_start or age >= STARTED_REFRESH_SECONDS
        return False

    def _refresh(self) -> None:
        client = self._get_client()
        clock, now = self._clock(), self._now()

        if self._standings_due(clock):
            try:
                self._standings = client.driver_standings()
                self._standings_at = clock
                self._standings_retry_at = 0.0
            except jolpica.JolpicaError as exc:
                logger.warning("F1: Jolpica standings unavailable: %s", exc)
                if self._standings is not None:
                    self._standings_retry_at = clock + FAILURE_BACKOFF_SECONDS

        if self._next_due(clock, now):
            try:
                self._next_race = client.next_race()
                self._next_loaded = True
                self._next_at = clock
                self._next_wall = now
                self._next_retry_at = 0.0
            except jolpica.JolpicaError as exc:
                logger.warning("F1: Jolpica next race unavailable: %s", exc)
                if self._next_loaded:
                    self._next_retry_at = clock + FAILURE_BACKOFF_SECONDS

    # ── output ───────────────────────────────────────────────────────────

    def _variables(self) -> Dict[str, Any]:
        race = self._next_race
        if race is not None:
            tz = resolve_timezone(self._timezone_name())
            next_race = short_race_name(race["race_name"])
            next_time = fit(format_start(race["start"], tz, race.get("has_time", True)))
        else:
            next_race = next_time = ""
        return {
            "race_status": STATUS_NEXT_RACE,
            "next_race": next_race,
            "next_race_time": next_time,
            "standings": standings_rows(self._standings or []),
        }

    def fetch_data(self) -> PluginResult:
        """Championship standings and the next race (cached, see module doc)."""
        with self._lock:
            self._refresh()
            if self._standings is None and not self._next_loaded:
                return PluginResult(available=False, error="Jolpica unavailable")
            return PluginResult(available=True, data=self._variables())


Plugin = F1Plugin
