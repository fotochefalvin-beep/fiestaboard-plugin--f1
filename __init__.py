"""F1 Live Standings plugin for FiestaBoard.

The driver championship and the next Grand Prix from Jolpica
(``api.jolpi.ca``, free, no key), plus the live race grid pushed by the
FastF1 sidecar (``sidecar/``) to ``/api/v1/plugins/f1/receive``. Template
variables (plugin id ``f1``), every value upper-case ASCII and at most 15
characters (Vestaboard Note):

* ``race_status`` -- ``LIVE`` (sidecar push < 60 s old), ``NO DATA`` (no
  push for 60 s during a race; the last grid/lap/track status stay),
  ``FINISHED`` (classification held 2 h from the first FINISHED push), else
  ``NEXT RACE``.
* ``lap``, ``total_laps``, ``track_status`` (``GREEN``/``YELLOW``/``SC``/
  ``VSC``/``RED``) -- always the latest push; empty outside a race.
* ``grid`` -- array, 0-indexed, by race position (``{{f1.grid.0.code}}`` is
  the leader), fields ``position``, ``code``, ``name``, ``number``, ``team``,
  ``gap``, ``interval``, ``status``. The published grid is replaced only when
  the order of P1..N changes (setting ``top_n``, default 3), on the first push
  of a race and on every FINISHED push; ``[]`` outside a race.
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
STATUS_LIVE = "LIVE"
STATUS_NO_DATA = "NO DATA"
STATUS_FINISHED = "FINISHED"

# ── live grid (sidecar pushes) ──
PAYLOAD_VERSION = 1
SESSION_LIVE = "LIVE"
SESSION_FINISHED = "FINISHED"
# race_status turns NO DATA when the last push is this old (SPEC CAP-8).
NO_DATA_AFTER_SECONDS = 60
# The final classification stays this long after the first FINISHED push.
RESULT_HOLD_SECONDS = 2 * 3600
# A race that never sent FINISHED (sidecar stopped, race abandoned) is
# dropped this long after its last push: the sidecar's hard cap.
LIVE_EXPIRY_SECONDS = 4 * 3600
TRACK_STATUSES = ("GREEN", "YELLOW", "SC", "VSC", "RED")
GRID_FIELDS = ("position", "code", "name", "number", "team", "gap", "interval", "status")
# More than any F1 field (22 cars in 2026); a bigger grid is a bad push.
MAX_GRID = 30
MAX_PAYLOAD_BYTES = 64 * 1024
DEFAULT_TOP_N = 3
MIN_TOP_N = 1
MAX_TOP_N = 20

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


def _payload_text(payload: Dict[str, Any], key: str) -> str:
    value = payload.get(key, "")
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{key} must be a string")
    return fit(value)


def parse_snapshot(payload: Any, raw_body: bytes = b"") -> Dict[str, Any]:
    """Validate a v1 sidecar push; return it cleaned (every value ``fit()``).

    Raises ``ValueError`` (HTTP 400) for anything that is not a v1 snapshot.
    """
    if len(raw_body) > MAX_PAYLOAD_BYTES:
        raise ValueError("payload too large")
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    if payload.get("v") != PAYLOAD_VERSION or isinstance(payload.get("v"), bool):
        raise ValueError(f"unsupported payload version (expected v={PAYLOAD_VERSION})")
    session = payload.get("session")
    if session not in (SESSION_LIVE, SESSION_FINISHED):
        raise ValueError(f"session must be {SESSION_LIVE} or {SESSION_FINISHED}")
    grid = payload.get("grid")
    if not isinstance(grid, list):
        raise ValueError("grid must be a list")
    if len(grid) > MAX_GRID:
        raise ValueError(f"grid has more than {MAX_GRID} entries")
    rows = []
    for item in grid:
        if not isinstance(item, dict):
            raise ValueError("every grid entry must be an object")
        rows.append({field: _payload_text(item, field) for field in GRID_FIELDS})
    track = _payload_text(payload, "track_status")
    return {
        "session": session,
        "lap": _payload_text(payload, "lap"),
        "total_laps": _payload_text(payload, "total_laps"),
        # Unknown or missing: keep the last known status (caller).
        "track_status": track if track in TRACK_STATUSES else "",
        "grid": rows,
    }


def top_codes(grid: List[Dict[str, str]], n: int) -> List[str]:
    """The driver codes of P1..N, the comparison key for publishing."""
    return [row.get("code", "") for row in grid[:n]]


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
    """Live race grid, driver championship and next Grand Prix as board variables."""

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

        # Live race state from the sidecar (all guarded by self._lock).
        self._live: Optional[Dict[str, Any]] = None  # latest cleaned push
        self._live_at = 0.0  # monotonic time of the latest push
        self._finished_at: Optional[float] = None  # first FINISHED push
        self._published_grid: List[Dict[str, str]] = []
        self._no_data_logged = False

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

    # ── live grid ────────────────────────────────────────────────────────

    def _top_n(self) -> int:
        value = (self.config or {}).get("top_n", DEFAULT_TOP_N)
        if isinstance(value, bool):
            return DEFAULT_TOP_N
        try:
            number = int(value)
        except (TypeError, ValueError):
            return DEFAULT_TOP_N
        return number if MIN_TOP_N <= number <= MAX_TOP_N else DEFAULT_TOP_N

    def _live_status(self, clock: float) -> Optional[str]:
        """LIVE / NO DATA / FINISHED, or None (no race: NEXT RACE).

        Drops an expired race (result hold over, or silent past the cap).
        """
        if self._live is None:
            return None
        if self._finished_at is not None:
            if clock - self._finished_at < RESULT_HOLD_SECONDS:
                return STATUS_FINISHED
            self._clear_live()
            return None
        age = clock - self._live_at
        if age < NO_DATA_AFTER_SECONDS:
            return STATUS_LIVE
        if age < LIVE_EXPIRY_SECONDS:
            if not self._no_data_logged:
                logger.warning("F1: no live data for %d s, race_status NO DATA", int(age))
                self._no_data_logged = True
            return STATUS_NO_DATA
        logger.info("F1: live race dropped after %d s without data", int(age))
        self._clear_live()
        return None

    def _clear_live(self) -> None:
        self._live = None
        self._live_at = 0.0
        self._finished_at = None
        self._published_grid = []
        self._no_data_logged = False

    def receive_payload(
        self,
        payload: Dict[str, Any],
        headers: Dict[str, str],
        raw_body: bytes = b"",
    ) -> None:
        """Store a v1 race snapshot from the sidecar (latest wins).

        ``ValueError`` (HTTP 400) for a bad push; the state is then unchanged.
        """
        try:
            snapshot = parse_snapshot(payload, raw_body)
        except ValueError as exc:
            logger.warning("F1: rejected push: %s", exc)
            raise

        with self._lock:
            clock = self._clock()
            status = self._live_status(clock)  # expires an old race first
            finished = snapshot["session"] == SESSION_FINISHED
            if status == STATUS_FINISHED and not finished:
                # A LIVE push after the flag: a new race begins.
                self._clear_live()
                status = None
            new_race = status is None
            if not snapshot["track_status"] and self._live is not None:
                snapshot["track_status"] = self._live["track_status"]

            n = self._top_n()
            if (
                new_race
                or finished
                or top_codes(snapshot["grid"], n) != top_codes(self._published_grid, n)
            ):
                self._published_grid = snapshot["grid"]

            if finished and self._finished_at is None:
                self._finished_at = clock
            if status == STATUS_NO_DATA:
                logger.info("F1: live data resumed")
            self._live = snapshot
            self._live_at = clock
            self._no_data_logged = False

    # ── output ───────────────────────────────────────────────────────────

    def _variables(self) -> Dict[str, Any]:
        race = self._next_race
        if race is not None:
            tz = resolve_timezone(self._timezone_name())
            next_race = short_race_name(race["race_name"])
            next_time = fit(format_start(race["start"], tz, race.get("has_time", True)))
        else:
            next_race = next_time = ""
        status = self._live_status(self._clock())
        live = self._live if status is not None else None
        return {
            "race_status": status or STATUS_NEXT_RACE,
            "lap": live["lap"] if live else "",
            "total_laps": live["total_laps"] if live else "",
            "track_status": live["track_status"] if live else "",
            "next_race": next_race,
            "next_race_time": next_time,
            "grid": [dict(row) for row in self._published_grid] if live else [],
            "standings": standings_rows(self._standings or []),
        }

    def fetch_data(self) -> PluginResult:
        """Live race state (memory) plus championship and next race (cached)."""
        with self._lock:
            self._refresh()
            if (
                self._standings is None
                and not self._next_loaded
                and self._live_status(self._clock()) is None
            ):
                return PluginResult(available=False, error="Jolpica unavailable")
            return PluginResult(available=True, data=self._variables())


Plugin = F1Plugin
