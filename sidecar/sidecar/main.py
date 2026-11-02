"""Sidecar entry point: wait for a Grand Prix window, follow the live feed, push.

Loop (`Sidecar.step`, re-run after the delay it returns, at most 30 s so the
healthcheck heartbeat stays fresh):

1. Load the calendar (Jolpica, cached) and pick the race whose window is
   open or next (`schedule.py`). Races already followed to FINISHED are
   skipped.
2. From 30 min before the window: pre-warm the F1 token (sign in if it is
   missing, expired, or would expire before the window closes).
3. In the window: sign in if needed, connect to the live feed and keep a
   `RaceState`; push the snapshot on every change (checked every second)
   and at least every 10 s as a heartbeat, but only while feed messages
   arrive (no message for 30 s: no heartbeat, so the board shows NO DATA
   after 60 s). A dropped or silent (60 s) connection is reopened after
   10 s, keeping the state. After FINISHED the snapshot is pushed for
   5 more minutes (late finishers settle the classification), then the
   feed is left. The window's hard cap (start + 4 h) always ends it.
4. A failed sign-in is logged (never the credentials) and retried every
   5 minutes while the window is open.

Exit codes: 2 configuration missing or malformed, 130 interrupted.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from . import live
from .config import ConfigError, load_config
from .notify import Notifier
from .schedule import Race, Schedule, ScheduleError
from .state import RaceState

log = logging.getLogger("sidecar")

MAX_SLEEP_S = 30.0
TICK_S = 1.0
HEARTBEAT_S = 10.0
PUSH_RETRY_S = 5.0
FEED_SILENT_S = 30.0
FEED_DROPPED_S = 60.0
FINISH_GRACE_S = 5 * 60.0
RECONNECT_S = 10.0
SIGNIN_RETRY_S = 5 * 60.0
SCHEDULE_RETRY_S = 5 * 60.0
IDLE_S = 3600.0


class Pusher:
    """Pushes a snapshot when it changed, or as a heartbeat."""

    def __init__(self, notifier: Notifier) -> None:
        self._notifier = notifier
        self._last: dict[str, Any] | None = None
        self._last_attempt = float("-inf")
        self._last_ok = True

    def tick(self, snapshot: dict[str, Any], clock: float) -> bool:
        """Push if due. Returns True when a push was attempted."""
        wait = HEARTBEAT_S if self._last_ok else PUSH_RETRY_S
        if snapshot == self._last and clock - self._last_attempt < wait:
            return False
        self._last_ok = self._notifier.push(snapshot)
        self._last = snapshot
        self._last_attempt = clock
        return True


def touch(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.touch()
    except OSError:
        log.debug("heartbeat file not writable")


class Sidecar:
    def __init__(
        self,
        schedule: Schedule,
        notifier: Notifier,
        connect_feed: Callable[[Callable[[str, Any], None]], Any],
        sign_in: Callable[..., float],
        heartbeat_file: Path | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.schedule = schedule
        self.notifier = notifier
        self.connect_feed = connect_feed
        self.sign_in = sign_in
        self.heartbeat_file = heartbeat_file
        self.now = now
        self.clock = clock
        self.sleep = sleep
        self.done: set[str] = set()
        self.state = RaceState()
        self.state_key: str | None = None
        self.lock = threading.Lock()
        self._prewarm_retry: dict[str, float] = {}
        self._signin_retry: dict[str, float] = {}
        self._announced: str | None = None

    # ── sign-in ──────────────────────────────────────────────────────────

    def _signed_in(self, race: Race) -> bool:
        """A token valid until the window closes; False (logged) on failure."""
        try:
            expiry = self.sign_in()
            if expiry < race.closes.timestamp():
                log.info("f1 token expires before the race window closes, signing in again")
                self.sign_in(force=True)
            return True
        except live.AuthError as exc:
            log.error("f1 sign-in failed: %s (retrying in %d min)", exc, SIGNIN_RETRY_S // 60)
            return False

    # ── main loop ────────────────────────────────────────────────────────

    def step(self) -> float:
        """One decision; returns how long to wait before the next."""
        touch(self.heartbeat_file)
        now = self.now()
        try:
            race = self.schedule.current(now, self.done)
        except ScheduleError:
            return SCHEDULE_RETRY_S
        if race is None:
            return IDLE_S
        if race.key != self._announced:
            log.info("next race: %s %s, window %s - %s", race.key, race.name, race.opens, race.closes)
            self._announced = race.key

        if now < race.prewarm_at:
            return (race.prewarm_at - now).total_seconds()
        if now < race.opens:
            clock = self.clock()
            if clock >= self._prewarm_retry.get(race.key, float("-inf")):
                ok = self._signed_in(race)
                self._prewarm_retry[race.key] = float("inf") if ok else clock + SIGNIN_RETRY_S
            return (race.opens - now).total_seconds()

        # Window open.
        if race.key != self.state_key:
            self.state, self.state_key = RaceState(), race.key
        clock = self.clock()
        retry_at = self._signin_retry.get(race.key, float("-inf"))
        if clock < retry_at:
            return retry_at - clock  # failed sign-in: wait the full 5 min
        if not self._signed_in(race):
            self._signin_retry[race.key] = clock + SIGNIN_RETRY_S
            return SIGNIN_RETRY_S
        self._signin_retry.pop(race.key, None)
        outcome = self.follow(race)
        if outcome in ("finished", "cap"):
            self.done.add(race.key)
            log.info("race %s done (%s)", race.key, outcome)
            return 0.0
        return RECONNECT_S

    def run(self) -> None:
        while True:
            self.sleep(min(max(self.step(), 0.0), MAX_SLEEP_S))

    # ── one connection ───────────────────────────────────────────────────

    def _on_topic(self, topic: str, data: Any) -> None:
        with self.lock:
            self.state.apply(topic, data)

    def follow(self, race: Race) -> str:
        """Follow the feed until finished, cap, or a dropped connection."""
        try:
            client = self.connect_feed(self._on_topic)
        except live.AuthError as exc:
            log.error("f1 sign-in failed: %s", exc)
            return "dropped"
        except Exception as exc:
            log.warning("live: connect failed (%s)", type(exc).__name__)
            return "dropped"
        log.info("live: following %s", race.key)
        pusher = Pusher(self.notifier)
        grace_until: float | None = None
        try:
            while True:
                touch(self.heartbeat_file)
                clock = self.clock()
                with self.lock:
                    snapshot = self.state.snapshot()
                    finished = self.state.finished
                silent_for = clock - client.last_message_at
                if snapshot is not None and silent_for < FEED_SILENT_S:
                    pusher.tick(snapshot, clock)
                if finished:
                    if grace_until is None:
                        log.info("live: %s finished", race.key)
                        grace_until = clock + FINISH_GRACE_S
                    elif clock >= grace_until:
                        return "finished"
                if self.now() >= race.closes:
                    return "cap"
                if not client.connected or silent_for > FEED_DROPPED_S:
                    log.warning("live: feed dropped (silent %d s), reconnecting", silent_for)
                    return "dropped"
                self.sleep(TICK_S)
        finally:
            client.close()


def connect_feed(on_topic: Callable[[str, Any], None]) -> Any:
    client = live.make_client(on_topic)
    try:
        client.connect()
    except BaseException:
        client.close()  # stop a half-started signalrcore connection
        raise
    return client


def main() -> int:
    level_name = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    level = logging.getLevelNamesMapping().get(level_name)
    logging.basicConfig(
        level=level if level is not None else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if level is None:
        log.error("config error: LOG_LEVEL=%r is not a logging level (e.g. INFO, DEBUG)", level_name)
        return 2
    try:
        config = load_config()
    except ConfigError as exc:
        log.error("config error: %s", exc)
        return 2

    live.configure_auth(config.f1_username, config.f1_password)
    session = requests.Session()
    sidecar = Sidecar(
        schedule=Schedule(config.jolpica_url, session),
        notifier=Notifier(config.fiestaboard_receive_url, config.fiestaboard_api_token, session),
        connect_feed=connect_feed,
        sign_in=live.ensure_token,
        heartbeat_file=config.heartbeat_file,
    )
    log.info("sidecar started")
    try:
        sidecar.run()
    except KeyboardInterrupt:
        log.info("interrupted")
        return 130
    return 1  # pragma: no cover - run() never returns


if __name__ == "__main__":
    sys.exit(main())
