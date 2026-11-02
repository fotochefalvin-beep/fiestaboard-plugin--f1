"""Replay an archived Grand Prix through `RaceState` (develop and verify).

    python -m sidecar replay <year> <gp> [--speed 20] [--print] [--samples 1,20] [--laps 44-45]
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
- `--laps START-END` (or `--laps N`) plays only those laps: everything
  before lap START goes into `RaceState` at once, without pushing or
  waiting, so the grid is right when START begins; the replay stops when
  lap END ends (the LapCount past END is not applied) and pushes its
  last snapshot.
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

from .state import RaceState, _int

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


def parse_laps(text: str) -> tuple[int, int]:
    """`44-45` -> (44, 45); `44` -> (44, 44). Positive, START <= END."""
    match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", text)
    if match is None:
        raise argparse.ArgumentTypeError(f"{text!r} is not START-END or N, e.g. 44-45")
    start = int(match.group(1))
    end = int(match.group(2) or start)
    if start < 1 or end < start:
        raise argparse.ArgumentTypeError(f"{text!r}: laps start at 1 and START <= END")
    return start, end


def order(snapshot: dict[str, Any] | None) -> str:
    if not snapshot:
        return "-"
    return " ".join(row["code"] for row in snapshot["grid"])


def replay(
    events: Iterable[tuple[float, str, Any]],
    *,
    speed: float = 0.0,
    samples: Iterable[int] = (),
    laps: tuple[int, int] | None = None,
    push: Callable[[dict[str, Any], float], Any] | None = None,
    out: TextIO | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> RaceState:
    """Feed *events* to a fresh RaceState; print samples; push via *push*.

    With *laps* = (START, END), only laps START..END are paced and pushed.
    """
    out = out or sys.stdout
    state = RaceState()
    wanted = set(samples)
    first_t: float | None = None
    # Pacing starts here: the first message, or the first one of lap START.
    pace_t: float | None = None
    playing = laps is None
    stopped = False
    started = clock()
    last_tick = float("-inf")
    summary: tuple[str, str, str] | None = None

    skipped = 0.0
    latest_t: float | None = None
    backwards = 0
    snapshot: dict[str, Any] | None = None
    for t, topic, msg in events:
        if first_t is None:
            first_t = pace_t = t
        new_lap = (
            _int(msg["CurrentLap"])
            if topic == "LapCount" and isinstance(msg, dict) and "CurrentLap" in msg
            else None
        )
        # Measured from the latest time seen: an out-of-order message must
        # not make the same gap count twice.
        if latest_t is not None and t < latest_t:
            backwards += 1
        elif latest_t is not None and t - latest_t > MAX_GAP_S:
            skipped += t - latest_t - MAX_GAP_S
            log.info("replay: skipped %.0f s without messages at t=%.1f s", t - latest_t - MAX_GAP_S, t - first_t)
        latest_t = t if latest_t is None else max(latest_t, t)
        if not playing and new_lap is not None and laps[0] <= new_lap <= laps[1]:
            playing = True
            pace_t, skipped, started = t, 0.0, clock()
            log.info("replay: lap %d reached at t=%.1f s, playing to lap %d", new_lap, t - first_t, laps[1])
        if playing and speed > 0:
            target = (t - pace_t - skipped) / speed
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
        if laps is not None and new_lap is not None and new_lap > laps[1]:
            stopped = True
            break
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
            if playing and push is not None and clock() - last_tick >= 1.0:
                push(snapshot, clock())
                last_tick = clock()

    if backwards:
        log.info("replay: %d message(s) were older than one before them", backwards)
    if not playing:
        log.warning("replay: lap %d never reached, nothing played", laps[0])
    elif stopped:
        print(f"stopped after lap {laps[1]}", file=out)
    final = state.snapshot()
    if playing and push is not None and final is not None:
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
    parser.add_argument(
        "--laps", type=parse_laps, help="play only these laps, e.g. 44-45 (earlier ones at once)"
    )
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
    replay(merge(streams), speed=args.speed, samples=samples, laps=args.laps, push=push)
    return 0
