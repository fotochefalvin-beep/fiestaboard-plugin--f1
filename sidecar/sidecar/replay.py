"""Replay an archived Grand Prix through `RaceState` (develop and verify).

    python -m sidecar replay <year> <gp> [--speed 20] [--print] [--samples 1,20]
    python -m sidecar replay --from-dir fixtures/ [--print]

- Downloads the archived topic streams of the race
  (`https://livetiming.formula1.com<api_path><Topic>.jsonStream`) through
  FastF1's signed-in session (F1_USERNAME[_FILE]/F1_PASSWORD[_FILE], as the
  live sidecar), falling back to FastF1's mirror like FastF1 does, or
  reads `<Topic>.jsonStream` files from `--from-dir`. `<gp>` is a round
  number or a name FastF1 understands (`Brazil`, `Sao Paulo`).
- Merges the streams by timestamp and feeds them to `RaceState` at
  `--speed` x real time (0 = as fast as possible). A silence of more than
  MAX_GAP_S in the archive (it opens a day or more before the race) is cut
  short, so a timed replay reaches the start within a minute.
- Pushes snapshots to FIESTABOARD_RECEIVE_URL like the live sidecar (on
  change, checked every second of wall time, heartbeat 10 s), unless
  `--print` is given: then nothing is pushed.
- Prints the grid order at the end of each `--samples` lap, the final
  classification, and every lap / track status / session change summary,
  to compare with the official result.

Archived data is never written to disk by this command.
"""

from __future__ import annotations

import argparse
import heapq
import json
import logging
import re
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any, TextIO

import requests

from .state import RaceState

log = logging.getLogger("sidecar.replay")

TOPICS = ("SessionStatus", "DriverList", "LapCount", "TrackStatus", "TimingData")
TIMEOUT_S = 30.0
# A longer silence in the archive is cut to this many seconds of race time.
MAX_GAP_S = 60.0
# With --speed, the replay sleeps at most this long between pushes.
SLEEP_SLICE_S = 1.0


TIMESTAMP = re.compile(r"(?:(\d+)\.)?(\d\d):(\d\d):(\d\d\.\d+)")


def parse_timestamp(text: str) -> float:
    """`01:02:03.456` -> 3723.456 seconds; `1.01:02:03.456` adds a day.

    Streams recorded for over 24 h (the archive starts well before the race)
    prefix the day count.
    """
    match = TIMESTAMP.fullmatch(text)
    if match is None:
        raise ValueError(f"bad timestamp: {text!r}")
    days, hours, minutes, seconds = match.groups()
    return int(days or 0) * 86400 + int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def parse_stream(text: str) -> list[tuple[float, Any]]:
    """An archived `.jsonStream` -> [(seconds, message)]; bad lines skipped."""
    records = []
    for line in text.lstrip("﻿").splitlines():
        match = TIMESTAMP.match(line.strip())
        if match is None:
            continue
        try:
            records.append((parse_timestamp(match.group()), json.loads(line.strip()[match.end():])))
        except (ValueError, json.JSONDecodeError):
            continue
    return records


def merge(streams: dict[str, list[tuple[float, Any]]]) -> Iterator[tuple[float, str, Any]]:
    """All topics in timestamp order (ties keep the TOPICS order)."""
    order = {topic: i for i, topic in enumerate(TOPICS)}
    iterables = [
        [(t, order.get(topic, len(order)), i, topic, msg) for i, (t, msg) in enumerate(records)]
        for topic, records in streams.items()
    ]
    for t, _, _, topic, msg in heapq.merge(*iterables):
        yield t, topic, msg


def read_dir(directory: Path) -> dict[str, list[tuple[float, Any]]]:
    streams = {}
    for topic in TOPICS:
        path = directory / f"{topic}.jsonStream"
        if path.is_file():
            streams[topic] = parse_stream(path.read_text(encoding="utf-8-sig"))
    return streams


def api_path(year: int, gp: str) -> str:
    """FastF1's archive path of the Grand Prix (`/static/2024/.../Race/`)."""
    import fastf1

    event = int(gp) if gp.isdigit() else gp
    return fastf1.get_session(year, event, "R").api_path


def fastf1_get(urls: list[str]) -> Any:
    """GET through FastF1's own session, as `fastf1.get_session(...).load()` does.

    That session adds the F1 token (and signs in again on a 401) and sends
    FastF1's livetiming headers; a hand-made request with only the token was
    answered 403. FastF1's HTTP cache is bypassed, so nothing is written to
    disk.
    """
    from fastf1 import _api
    from fastf1.req import Cache

    with Cache.disabled():
        return Cache.requests_get(urls, headers=_api.headers, timeout=TIMEOUT_S)


def download(path: str, get: Callable[[list[str]], Any] | None = None) -> dict[str, list[tuple[float, Any]]]:
    """Each topic's archived stream, from F1 or FastF1's mirror (FastF1's order)."""
    from fastf1 import _api

    get = get or fastf1_get
    streams = {}
    for topic in TOPICS:
        name = f"{path}{topic}.jsonStream"
        # One URL per call, so a failure names F1's status, not only the mirror's.
        failures = []
        for url in (f"{_api.base_url}{name}", f"{_api.base_url_mirror}{name}"):
            response = get([url])
            if response.status_code == 200:
                break
            failures.append(f"{url.split('/')[2]} HTTP {response.status_code}")
        else:
            raise RuntimeError(f"{topic}: {', '.join(failures)}")
        streams[topic] = parse_stream(response.content.decode("utf-8-sig"))
        log.info("replay: %s %d message(s)", topic, len(streams[topic]))
    return streams


def order(snapshot: dict[str, Any] | None) -> str:
    if not snapshot:
        return "-"
    return " ".join(row["code"] for row in snapshot["grid"])


def replay(
    events: Iterable[tuple[float, str, Any]],
    *,
    speed: float = 0.0,
    samples: Iterable[int] = (),
    push: Callable[[dict[str, Any], float], Any] | None = None,
    out: TextIO | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> RaceState:
    """Feed *events* to a fresh RaceState; print samples; push via *push*."""
    out = out or sys.stdout
    state = RaceState()
    wanted = set(samples)
    first_t: float | None = None
    started = clock()
    last_tick = float("-inf")
    summary: tuple[str, str, str] | None = None

    skipped = 0.0
    prev_t: float | None = None
    snapshot: dict[str, Any] | None = None
    for t, topic, msg in events:
        if first_t is None:
            first_t = t
        if prev_t is not None and t - prev_t > MAX_GAP_S:
            skipped += t - prev_t - MAX_GAP_S
        prev_t = t
        if speed > 0:
            target = (t - first_t - skipped) / speed
            delay = target - (clock() - started)
            while delay > 0:
                # In slices, pushing meanwhile (the pusher sends a heartbeat
                # when due), so a quiet stretch never reads as NO DATA.
                sleep(min(delay, SLEEP_SLICE_S))
                if push is not None and snapshot is not None:
                    push(snapshot, clock())
                    last_tick = clock()
                delay = target - (clock() - started)

        if topic == "LapCount" and isinstance(msg, dict) and "CurrentLap" in msg:
            ended = int(state.lap) if state.lap.isdigit() else None
            if ended in wanted and str(msg["CurrentLap"]) != state.lap:
                print(f"lap {ended:>3} end: {order(state.snapshot())}", file=out)
                wanted.discard(ended)
        state.apply(topic, msg)

        snapshot = state.snapshot()
        if snapshot is not None:
            current = (snapshot["session"], snapshot["track_status"], snapshot["lap"])
            if summary is None or current[:2] != summary[:2]:
                print(
                    f"t={t - first_t:8.1f}s lap={snapshot['lap'] or '-':>3} "
                    f"session={snapshot['session']} track={snapshot['track_status'] or '-'}",
                    file=out,
                )
            summary = current
            if push is not None and clock() - last_tick >= 1.0:
                push(snapshot, clock())
                last_tick = clock()

    final = state.snapshot()
    if push is not None and final is not None:
        push(final, clock())
    print(f"final ({state.session}, lap {state.lap}/{state.total_laps}): {order(final)}", file=out)
    return state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sidecar replay")
    parser.add_argument("year", nargs="?", type=int)
    parser.add_argument("gp", nargs="?")
    parser.add_argument("--from-dir", type=Path, help="read <Topic>.jsonStream files from here")
    parser.add_argument("--path", help="archive path (/static/...), instead of year/gp")
    parser.add_argument("--speed", type=float, default=0.0, help="x real time, 0 = no waiting")
    parser.add_argument("--samples", default="", help="laps to print the order at, e.g. 1,20,71")
    parser.add_argument("--print", action="store_true", help="only print, do not push")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    from . import live
    from .config import ConfigError, load_credentials, load_push_config
    from .main import Pusher
    from .notify import Notifier

    try:
        samples = [int(x) for x in args.samples.split(",") if x.strip()]
    except ValueError:
        log.error("--samples must be lap numbers, e.g. 1,20,71")
        return 2

    session = requests.Session()
    try:
        push_config = None if args.print else load_push_config()
        if args.from_dir:
            streams = read_dir(args.from_dir)
        else:
            if not (args.path or (args.year and args.gp)):
                parser.error("give <year> <gp>, --path or --from-dir")
            live.configure_auth(*load_credentials())
            path = args.path or api_path(args.year, args.gp)
            log.info("replay: %s", path)
            live.archive_token()  # sign in up front: a bad password fails here, clearly
            streams = download(path)
    except ConfigError as exc:
        log.error("config error: %s", exc)
        return 2
    except (live.AuthError, RuntimeError, ValueError, requests.RequestException) as exc:
        log.error("replay: cannot load the race: %s", exc)
        return 1

    push = None
    if push_config is not None:
        pusher = Pusher(
            Notifier(push_config.fiestaboard_receive_url, push_config.fiestaboard_api_token, session)
        )
        push = pusher.tick
    replay(merge(streams), speed=args.speed, samples=samples, push=push)
    return 0
