"""Race state: F1 live-timing messages -> the v1 snapshot the plugin receives.

Pure and synchronous: `RaceState.apply(topic, msg)` merges one message (a
full state or a delta, as the live feed and the archived `.jsonStream` files
both deliver them) and `RaceState.snapshot()` returns the push payload:

    {"v": 1, "session": "LIVE" | "FINISHED", "lap": "34", "total_laps": "57",
     "track_status": "SC", "grid": [{"position", "code", "name", "number",
     "team", "gap", "interval", "status"}, ...]}

`sent_at` is added by the notifier. Every value is upper-case ASCII, at most
15 characters; `grid` is sorted by race position (leader first).

Topics used: DriverList, TimingData, LapCount, TrackStatus, SessionStatus.
Everything else (Heartbeat, RaceControlMessages, ...) only counts as "the
feed is alive" for the caller. A snapshot exists only once the race has
started (SessionStatus Started, or a finished state) and there is a grid:
before the start the plugin keeps showing NEXT RACE.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

PAYLOAD_VERSION = 1
MAX_LEN = 15
SESSION_LIVE = "LIVE"
SESSION_FINISHED = "FINISHED"

# TrackStatus.Status -> board value. Unknown codes keep the last value.
TRACK_CODES = {
    "1": "GREEN",
    "2": "YELLOW",
    "4": "SC",
    "5": "RED",
    "6": "VSC",
    "7": "VSC",  # VSC ending
}
STARTED_STATUSES = {"Started"}
FINISHED_STATUSES = {"Finished", "Finalised", "Ends"}

# Long team names -> what fits and reads well on a 15-column row.
TEAM_NAMES = {
    "RED BULL RACING": "RED BULL",
    "RED BULL RACING HONDA RBPT": "RED BULL",
    "HAAS F1 TEAM": "HAAS",
    "KICK SAUBER": "SAUBER",
    "STAKE F1 TEAM KICK SAUBER": "SAUBER",
    "ASTON MARTIN ARAMCO": "ASTON MARTIN",
    "MERCEDES-AMG PETRONAS": "MERCEDES",
    "SCUDERIA FERRARI": "FERRARI",
    "VISA CASH APP RB": "RACING BULLS",
    "CADILLAC F1 TEAM": "CADILLAC",
}

_GAP_SECONDS_RE = re.compile(r"^\+?(\d+(?::\d+)?)(?:\.(\d+))?$")
_GAP_LAPS_RE = re.compile(r"^\+?\s*(\d+)\s*L(?:AP|APS)?$")

# TimingData line fields kept per driver.
_TIMING_FIELDS = ("Position", "GapToLeader", "InPit", "PitOut", "Retired", "Stopped")


def board_text(value: Any, limit: int = MAX_LEN) -> str:
    """Upper-case ASCII, whitespace collapsed, cut to *limit*."""
    text = unicodedata.normalize("NFKD", "" if value is None else str(value))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"\s+", " ", text).strip().upper()
    return text[:limit].rstrip()


def format_gap(value: Any) -> str:
    """`+18.432` -> `+18.4`; `1 L`/`1L` -> `+1 LAP`; `3 LAPS` -> `+3 LAPS`.

    The leader's `LAP 34` and anything empty -> ``""``. Tenths are cut, not
    rounded, like the timing screens.
    """
    text = board_text(value, limit=64)
    if not text or text.startswith("LAP"):
        return ""
    match = _GAP_LAPS_RE.match(text)
    if match:
        laps = int(match.group(1))
        return f"+{laps} LAP" if laps == 1 else f"+{laps} LAPS"
    match = _GAP_SECONDS_RE.match(text)
    if match:
        whole, decimals = match.group(1), match.group(2)
        return f"+{whole}.{decimals[0]}" if decimals else f"+{whole}"
    return board_text(text)


def team_name(value: Any) -> str:
    text = board_text(value, limit=64)
    return board_text(TEAM_NAMES.get(text, text))


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return bool(value)


def _is_driver_key(key: Any) -> bool:
    return isinstance(key, str) and key.isdigit()


def _int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


class RaceState:
    """Merged live-timing state of one race."""

    def __init__(self) -> None:
        self.drivers: dict[str, dict[str, Any]] = {}  # DriverList, by number
        self.timing: dict[str, dict[str, Any]] = {}  # TimingData lines, by number
        self.lap = ""
        self.total_laps = ""
        self.track_status = ""
        self.session_status = ""
        self.started = False
        self.finished = False

    # ── merging ──────────────────────────────────────────────────────────

    def apply(self, topic: str, msg: Any) -> bool:
        """Merge one message. Returns True when the snapshot may have changed."""
        if not isinstance(msg, dict):
            return False
        handler = {
            "DriverList": self._driver_list,
            "TimingData": self._timing_data,
            "LapCount": self._lap_count,
            "TrackStatus": self._track_status,
            "SessionStatus": self._session_status,
        }.get(topic)
        if handler is None:
            return False
        handler(msg)
        return True

    def _driver_list(self, msg: dict) -> None:
        for number, info in msg.items():
            if _is_driver_key(number) and isinstance(info, dict):
                self.drivers.setdefault(number, {}).update(info)

    def _timing_data(self, msg: dict) -> None:
        lines = msg.get("Lines")
        if not isinstance(lines, dict):
            return
        for number, line in lines.items():
            if not _is_driver_key(number) or not isinstance(line, dict):
                continue
            current = self.timing.setdefault(number, {})
            for field in _TIMING_FIELDS:
                if field in line:
                    current[field] = line[field]
            interval = line.get("IntervalToPositionAhead")
            if isinstance(interval, dict):
                if "Value" in interval:
                    current["Interval"] = interval["Value"]
            elif interval is not None:
                current["Interval"] = interval

    def _lap_count(self, msg: dict) -> None:
        if "CurrentLap" in msg and _int(msg["CurrentLap"]) is not None:
            self.lap = str(_int(msg["CurrentLap"]))
        if "TotalLaps" in msg and _int(msg["TotalLaps"]) is not None:
            self.total_laps = str(_int(msg["TotalLaps"]))

    def _track_status(self, msg: dict) -> None:
        code = str(msg.get("Status", "")).strip()
        if code in TRACK_CODES:
            self.track_status = TRACK_CODES[code]

    def _session_status(self, msg: dict) -> None:
        status = str(msg.get("Status", "")).strip()
        if not status:
            return
        self.session_status = status
        if status in STARTED_STATUSES:
            self.started = True
        if status in FINISHED_STATUSES:
            self.finished = True

    # ── output ───────────────────────────────────────────────────────────

    def _position(self, number: str) -> int | None:
        position = _int(self.timing.get(number, {}).get("Position"))
        if position is None:
            position = _int(self.drivers.get(number, {}).get("Line"))
        return position if position and position > 0 else None

    def _row(self, number: str, position: int | None) -> dict[str, str]:
        info = self.drivers.get(number, {})
        timing = self.timing.get(number, {})
        family = info.get("LastName") or str(info.get("BroadcastName") or "").split(" ")[-1]
        code = info.get("Tla") or board_text(family)[:3]
        leader = position == 1
        if _truthy(timing.get("Retired")):
            status = "DNF"
        elif _truthy(timing.get("InPit")):
            status = "PIT"
        elif _truthy(timing.get("PitOut")):
            status = "OUT"
        else:
            status = ""
        return {
            "position": str(position) if position else "",
            "code": board_text(code),
            "name": board_text(family),
            "number": board_text(info.get("RacingNumber") or number),
            "team": team_name(info.get("TeamName")),
            "gap": "" if leader else format_gap(timing.get("GapToLeader")),
            "interval": "" if leader else format_gap(timing.get("Interval")),
            "status": status,
        }

    def grid(self) -> list[dict[str, str]]:
        """All known drivers by race position; unknown positions last."""
        numbers = set(self.drivers) | set(self.timing)
        ranked = sorted(
            numbers,
            key=lambda n: (self._position(n) is None, self._position(n) or 0, int(n)),
        )
        return [self._row(n, self._position(n)) for n in ranked]

    @property
    def session(self) -> str:
        return SESSION_FINISHED if self.finished else SESSION_LIVE

    def snapshot(self) -> dict[str, Any] | None:
        """The v1 push payload (without `sent_at`), or None before the start."""
        if not (self.started or self.finished):
            return None
        grid = self.grid()
        if not grid:
            return None
        return {
            "v": PAYLOAD_VERSION,
            "session": self.session,
            "lap": self.lap,
            "total_laps": self.total_laps,
            "track_status": self.track_status,
            "grid": grid,
        }
