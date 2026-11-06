"""Session state: F1 live-timing messages -> the v1 snapshot the plugin receives.

Pure and synchronous: `RaceState(session_type).apply(topic, msg)` merges one
message (a full state or a delta, as the live feed and the archived
`.jsonStream` files both deliver them) and `RaceState.snapshot(now)` returns
the push payload:

    {"v": 1, "session": "LIVE" | "FINISHED", "session_type": "Q",
     "session_part": "Q2", "lap": "", "total_laps": "", "track_status": "SC",
     "clock": {"remaining": 754, "running": true},
     "grid": [{"position", "code", "name", "number", "team", "gap",
     "interval", "best", "status"}, ...]}

`sent_at` is added by the notifier. Every value is upper-case ASCII, at most
15 characters; `grid` is sorted by position (leader / fastest first).

Session types: FP1/FP2/FP3 (practice), SQ/Q (qualifying, `session_part`
`SQ1`..`SQ3` / `Q1`..`Q3`), SPRINT/RACE. `session_part` is sent in
qualifying only, `clock` in practice and qualifying only (once the feed has
sent an ExtrapolatedClock); `remaining` is extrapolated to *now* while the
clock runs.

- Race types: `gap` to the leader / `interval` to the car ahead in tenths
  (`+18.4`, `+1 LAP`), `best` empty, `lap`/`total_laps` from LapCount.
- Practice: `best` = BestLapTime, `gap` = TimeDiffToFastest, `interval` =
  TimeDiffToPositionAhead, thousandths kept (`+0.234`).
- Qualifying: the same from the current part's `BestLapTimes[part-1]` and
  `Stats[part-1]`; a knocked-out driver (`KnockedOut`) shows `status`
  `KNOCKED OUT` and the times of the last part he set one in. A part's
  `Finished` is not the end: only `Finished` in part 3, or
  `Finalised`/`Ends`, is FINISHED.

TimingData lines are deep-merged (a list arrives as the full state, later
deltas address its items as `{"1": {...}}`; `_deleted` removes keys).

Topics used: DriverList, TimingData, LapCount, TrackStatus, SessionStatus,
ExtrapolatedClock. Everything else (Heartbeat, RaceControlMessages, ...)
only counts as "the feed is alive" for the caller. A snapshot exists only
once the session has started (SessionStatus Started, or a finished state)
and there is a grid: before the start the plugin keeps showing NEXT RACE.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
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
# In qualifying, "Finished" ends a part; only these end the session.
FINAL_STATUSES = {"Finalised", "Ends"}

SESSION_TYPES = ("FP1", "FP2", "FP3", "SQ", "SPRINT", "Q", "RACE")
PRACTICE_TYPES = ("FP1", "FP2", "FP3")
QUALI_TYPES = ("SQ", "Q")
RACE_TYPES = ("SPRINT", "RACE")
QUALI_PARTS = 3
STATUS_KNOCKED_OUT = "KNOCKED OUT"

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
_DIFF_RE = re.compile(r"^\+?\d+(?::\d+)?(?:\.\d+)?$")
_CLOCK_RE = re.compile(r"^(\d+):(\d\d):(\d\d)(?:\.\d+)?$")


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


def format_diff(value: Any) -> str:
    """Timesheet difference: `+0.234` / `0.234` -> `+0.234` (thousandths kept)."""
    text = board_text(value, limit=64)
    if not text:
        return ""
    if _DIFF_RE.match(text):
        return board_text(text if text.startswith("+") else f"+{text}")
    return board_text(text)


def parse_clock(value: Any) -> int | None:
    """`00:17:59` -> 1079 seconds (None when not a clock)."""
    match = _CLOCK_RE.match(str(value if value is not None else "").strip())
    if match is None:
        return None
    hours, minutes, seconds = (int(x) for x in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def parse_utc(value: Any) -> datetime | None:
    """`2026-10-31T21:00:01.014Z` -> aware UTC datetime (None when bad)."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        when = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(UTC)


def deep_merge(target: dict, delta: dict) -> None:
    """Merge a live-timing delta into *target* in place.

    Lists become index dicts (`[a, b]` -> `{"0": a, "1": b}`) so later deltas
    (`{"1": {...}}`) address their items; `_deleted: [keys]` removes keys.
    """
    deleted = delta.get("_deleted")
    if isinstance(deleted, list):
        for key in deleted:
            target.pop(str(key), None)
    for key, value in delta.items():
        if key == "_deleted":
            continue
        key = str(key)
        if isinstance(value, list):
            value = {str(i): item for i, item in enumerate(value)}
        if isinstance(value, dict):
            current = target.get(key)
            if not isinstance(current, dict):
                current = target[key] = {}
            deep_merge(current, value)
        else:
            target[key] = value


def _value(item: Any) -> Any:
    """A `{"Value": x}` field -> x; a plain value as is."""
    return item.get("Value") if isinstance(item, dict) else item


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
    """Merged live-timing state of one session."""

    def __init__(self, session_type: str = "RACE") -> None:
        if session_type not in SESSION_TYPES:
            raise ValueError(f"unknown session type {session_type!r}")
        self.session_type = session_type
        self.drivers: dict[str, dict[str, Any]] = {}  # DriverList, by number
        self.timing: dict[str, dict[str, Any]] = {}  # TimingData lines, by number
        self.session_part: int | None = None  # TimingData SessionPart (quali)
        self.clock: dict[str, Any] = {}  # ExtrapolatedClock (Utc, Remaining, Extrapolating)
        self.lap = ""
        self.total_laps = ""
        self.track_status = ""
        self.session_status = ""
        self.started = False
        self._ended = False  # a status that always ends the session was seen
        # Qualifying: the part the latest "Finished" arrived in (SessionPart
        # moves to the next part ~1 min before that part, while the status
        # still reads "Finished" from the one before).
        self._finished_part: int | None = None

    @property
    def is_quali(self) -> bool:
        return self.session_type in QUALI_TYPES

    @property
    def is_race(self) -> bool:
        return self.session_type in RACE_TYPES

    @property
    def finished(self) -> bool:
        if self._ended:
            return True
        # Qualifying: "Finished" after Q1/Q2 only ends a part.
        return (
            self.is_quali
            and self.session_status == "Finished"
            and (self._finished_part or 0) >= QUALI_PARTS
        )

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
            "ExtrapolatedClock": self._extrapolated_clock,
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
        part = _int(msg.get("SessionPart"))
        if part is not None and 1 <= part <= QUALI_PARTS:
            self.session_part = part
            if self.session_status == "Finished" and self._finished_part is None:
                # Full state on (re)connect: the status came before the part.
                self._finished_part = part
        lines = msg.get("Lines")
        if not isinstance(lines, dict):
            return
        for number, line in lines.items():
            if not _is_driver_key(number) or not isinstance(line, dict):
                continue
            deep_merge(self.timing.setdefault(number, {}), line)

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
        if status == "Finished":
            self._finished_part = self.session_part
        if status in STARTED_STATUSES:
            self.started = True
        final = FINAL_STATUSES if self.is_quali else FINISHED_STATUSES
        if status in final:
            self._ended = True

    def _extrapolated_clock(self, msg: dict) -> None:
        for field in ("Utc", "Remaining", "Extrapolating"):
            if field in msg:
                self.clock[field] = msg[field]

    # ── output ───────────────────────────────────────────────────────────

    def _position(self, number: str) -> int | None:
        position = _int(self.timing.get(number, {}).get("Position"))
        if position is None:
            position = _int(self.drivers.get(number, {}).get("Line"))
        return position if position and position > 0 else None

    def _timesheet(self, timing: dict[str, Any], knocked_out: bool) -> tuple[str, str, str]:
        """(best, gap, interval) raw values for practice / qualifying."""
        if not self.is_quali:
            return (
                _value(timing.get("BestLapTime")),
                timing.get("TimeDiffToFastest"),
                timing.get("TimeDiffToPositionAhead") or timing.get("TimeDifftoPositionAhead"),
            )
        bests = timing.get("BestLapTimes") if isinstance(timing.get("BestLapTimes"), dict) else {}
        stats = timing.get("Stats") if isinstance(timing.get("Stats"), dict) else {}
        index = (self.session_part or 1) - 1
        if knocked_out:
            # The last part the driver set a time in.
            for i in range(index, -1, -1):
                if board_text(_value(bests.get(str(i)))):
                    index = i
                    break
        stat = stats.get(str(index)) if isinstance(stats.get(str(index)), dict) else {}
        return (
            _value(bests.get(str(index))),
            stat.get("TimeDiffToFastest"),
            stat.get("TimeDifftoPositionAhead") or stat.get("TimeDiffToPositionAhead"),
        )

    def _row(self, number: str, position: int | None) -> dict[str, str]:
        info = self.drivers.get(number, {})
        timing = self.timing.get(number, {})
        family = info.get("LastName") or str(info.get("BroadcastName") or "").split(" ")[-1]
        code = info.get("Tla") or board_text(family)[:3]
        leader = position == 1
        knocked_out = self.is_quali and _truthy(timing.get("KnockedOut"))
        if knocked_out:
            status = STATUS_KNOCKED_OUT
        elif _truthy(timing.get("Retired")):
            status = "DNF"
        elif _truthy(timing.get("InPit")):
            status = "PIT"
        elif _truthy(timing.get("PitOut")):
            status = "OUT"
        else:
            status = ""
        if self.is_race:
            best = ""
            gap = "" if leader else format_gap(timing.get("GapToLeader"))
            interval = "" if leader else format_gap(_value(timing.get("IntervalToPositionAhead")))
        else:
            raw_best, raw_gap, raw_interval = self._timesheet(timing, knocked_out)
            best = board_text(raw_best)
            gap = "" if leader else format_diff(raw_gap)
            interval = "" if leader else format_diff(raw_interval)
        return {
            "position": str(position) if position else "",
            "code": board_text(code),
            "name": board_text(family),
            "number": board_text(info.get("RacingNumber") or number),
            "team": team_name(info.get("TeamName")),
            "gap": gap,
            "interval": interval,
            "best": best,
            "status": status,
        }

    def grid(self) -> list[dict[str, str]]:
        """All known drivers by position; unknown positions last."""
        numbers = set(self.drivers) | set(self.timing)
        ranked = sorted(
            numbers,
            key=lambda n: (self._position(n) is None, self._position(n) or 0, int(n)),
        )
        return [self._row(n, self._position(n)) for n in ranked]

    @property
    def session(self) -> str:
        return SESSION_FINISHED if self.finished else SESSION_LIVE

    @property
    def part_label(self) -> str:
        """`Q2` / `SQ2` in qualifying once the part is known, else ``""``."""
        if not self.is_quali or self.session_part is None:
            return ""
        return f"{self.session_type}{self.session_part}"

    def clock_snapshot(self, now: datetime | None = None) -> dict[str, Any] | None:
        """`{"remaining": s, "running": bool}` at *now*, or None (no clock)."""
        remaining = parse_clock(self.clock.get("Remaining"))
        if remaining is None:
            return None
        running = _truthy(self.clock.get("Extrapolating"))
        if running:
            at = parse_utc(self.clock.get("Utc"))
            if at is not None:
                now = now or datetime.now(UTC)
                elapsed = max(0.0, (now - at).total_seconds())  # never counts up
                remaining = int(remaining - elapsed)
        return {"remaining": max(0, remaining), "running": running}

    def snapshot(self, now: datetime | None = None) -> dict[str, Any] | None:
        """The v1 push payload (without `sent_at`), or None before the start.

        *now* (default: the wall clock) extrapolates a running session clock.
        """
        if not (self.started or self.finished):
            return None
        grid = self.grid()
        if not grid:
            return None
        snap: dict[str, Any] = {
            "v": PAYLOAD_VERSION,
            "session": self.session,
            "session_type": self.session_type,
        }
        if self.part_label:
            snap["session_part"] = self.part_label
        snap.update(lap=self.lap, total_laps=self.total_laps, track_status=self.track_status)
        if not self.is_race:
            clock = self.clock_snapshot(now)
            if clock is not None:
                snap["clock"] = clock
        snap["grid"] = grid
        return snap
