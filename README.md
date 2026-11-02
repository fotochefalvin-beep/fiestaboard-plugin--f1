# F1 Live Standings

A [FiestaBoard](https://github.com/Fiestaboard/FiestaBoard) plugin (id `f1`)
that puts Formula 1 on the board: the live Grand Prix grid, the driver
championship and the next Grand Prix. The championship and the calendar come
from [Jolpica](https://api.jolpi.ca) (the free Ergast successor, no key); the
live grid comes from F1 live timing through a small
[FastF1](https://docs.fastf1.dev) sidecar container that pushes it to the
plugin. Built for a Vestaboard Note (3 x 15): every value is upper-case ASCII
and at most 15 characters.

There is no default page. Build your own layout from the variables, e.g. on a
Note:

```
{{f1.next_race}}
{{f1.next_race_time}}
{{f1.standings.0.code}} {{f1.standings.0.points}}
```

```
BRAZILIAN
SUN 18:00
ANT 380
```

During a race, e.g.:

```
{{f1.race_status}} {{f1.lap}}/{{f1.total_laps}}
{{f1.grid.0.code}} {{f1.grid.1.code}} {{f1.grid.2.code}}
{{f1.track_status}} {{f1.grid.1.gap}}
```

```
LIVE 34/57
VER NOR LEC
SC +18.4
```

## Settings

* **Enabled**
* **Top N** (1-20, default 3): the live `grid` on the board is replaced when
  the order of the first N drivers changes (an overtake, a pit stop
  shuffle). Changes only below P_N don't touch it; `lap`, `total_laps`,
  `track_status` and `race_status` are always current. FiestaBoard sends at
  most one board update per 15 s, so two swaps in quick succession end with
  the latest order on the board.

Times use FiestaBoard's general timezone (Settings -> General); UTC when it
is not set.

## Variables

| Variable | Example | Notes |
|---|---|---|
| `f1.race_status` | `LIVE` | `LIVE` during a Grand Prix; `NO DATA` when the sidecar has sent nothing for 60 s (the last grid, lap and track status stay); `FINISHED` for 2 h after the flag (the classification stays); else `NEXT RACE` |
| `f1.lap` | `34` | Lead lap; empty outside a race |
| `f1.total_laps` | `57` | Scheduled laps; empty outside a race |
| `f1.track_status` | `SC` | `GREEN`, `YELLOW`, `SC`, `VSC` or `RED`; empty outside a race |
| `f1.next_race` | `MEXICO CITY` | Jolpica race name without "Grand Prix"; empty when the season is over |
| `f1.next_race_time` | `SUN 21:00` | Race start, day and time in your timezone; empty when the season is over |

### Array `grid` (live race, 0-indexed, by race position)

`{{f1.grid.0.code}}` is the leader. Filled while `race_status` is `LIVE`,
`NO DATA` or `FINISHED`; empty otherwise.

| Field | Example | Notes |
|---|---|---|
| `position` | `7` | |
| `code` | `HAM` | Three-letter code |
| `name` | `HAMILTON` | Family name, accents removed |
| `number` | `44` | |
| `team` | `FERRARI` | Short team name (`RED BULL`, `HAAS`, `SAUBER`) |
| `gap` | `+18.4`, `+1 LAP` | To the leader (tenths cut); empty for P1 |
| `interval` | `+1.2` | To the car ahead; empty for P1 |
| `status` | `PIT`, `OUT`, `DNF` | Empty while running |

### Array `standings` (driver championship, 0-indexed, leader first)

`{{f1.standings.0.code}}` is the championship leader, `{{f1.standings.1.code}}`
second, and so on. Empty before the first race of a season.

| Field | Example | Notes |
|---|---|---|
| `position` | `1` | |
| `code` | `ANT` | Three-letter code (first 3 letters of the family name if Jolpica has none) |
| `name` | `ANTONELLI` | Family name, accents removed |
| `points` | `380` | Half points stay (`12.5`) |
| `wins` | `9` | |

## Data and request economy

* `GET /ergast/f1/current/driverstandings/?limit=100` and
  `GET /ergast/f1/current/next/`, each cached for an hour (two requests an
  hour normally).
* Once the next race's start has passed, `/current/next/` is fetched again on
  the next update, then at most every 15 minutes until Jolpica lists the
  following race.
* When Jolpica fails (timeout, 5xx, bad JSON) the last good values stay on the
  board, a warning is logged and that endpoint waits 5 minutes before
  retrying. If nothing was ever loaded the plugin reports
  `Jolpica unavailable` and retries on the next update.

## Live grid: the sidecar

FiestaBoard plugins can't bring their own dependencies, so FastF1 runs in a
separate container on the same Pi (`sidecar/`, `docker-compose.sidecar.yml`).
It has no inbound port: it only connects out to Jolpica, F1 and FiestaBoard,
and pushes the race to `POST /api/v1/plugins/f1/receive`.

* **When:** from 15 minutes before each Grand Prix (Jolpica's start time)
  until the race is finished (plus 5 minutes for the last cars to cross the
  line), at most start + 4 h. Idle otherwise. Sprints, qualifying and
  practice are never followed.
* **What:** a full snapshot (`session`, `lap`, `total_laps`, `track_status`,
  `grid`) on every change, checked every second, and at least every 10 s as
  a heartbeat while the feed is alive. If the feed goes quiet the heartbeats
  stop and the board shows `NO DATA` after 60 s; the sidecar reconnects
  after 60 s of silence.
* **F1 account:** live timing needs a signed-in F1 TV account (since F1's
  October 2026 change). The sidecar signs in headless with the email and
  password from its secret files, keeps the token (about 4 days) on the
  named volume `f1-token`, pre-warms it 30 minutes before each race window
  and signs in again only when it has expired. A failed sign-in (wrong
  password, MFA) is logged without the credentials and retried every 5
  minutes during the window.

### Setup on the Pi

From this repo's folder (next to FiestaBoard):

```bash
cp .env.example .env                       # receive URL + network name
mkdir -p secrets
cp secrets/f1_username.example secrets/f1_username
cp secrets/f1_password.example secrets/f1_password
cp secrets/fiestaboard_api_token.example secrets/fiestaboard_api_token
# Edit the three files: replace the YOUR_... placeholder with the real value
# (F1 account email, password, a FiestaBoard API token). One line each.
# The container reads them as uid 10001 (compose file secrets keep host
# owner/mode); the folder itself stays private.
chmod 700 secrets && chmod 644 secrets/*
docker compose -f docker-compose.sidecar.yml up -d --build
docker logs -f f1-live-standings-sidecar
```

`secrets/` and `.env` are git-ignored; only the `*.example` placeholders are
tracked. The plugin must be enabled in FiestaBoard, else the receive route
answers 400.

### Replaying an archived race

To develop or check the live grid without waiting for a Grand Prix, replay
an archived race through the same state code (needs the F1 secret files, as
the archive also requires sign-in):

```bash
cd sidecar
export UV_PROJECT_ENVIRONMENT=/tmp/f1-sidecar-venv   # keep the venv out of the repo
F1_USERNAME_FILE=../secrets/f1_username F1_PASSWORD_FILE=../secrets/f1_password \
  uv run python -m sidecar replay 2025 "Sao Paulo" --print --samples 1,20,71
```

It prints the order at the end of each sampled lap, every track status and
session change, and the final classification, to compare with the official
results. Without `--print` it pushes to `FIESTABOARD_RECEIVE_URL` (with
`FIESTABOARD_API_TOKEN[_FILE]`) like the live sidecar; `--speed 20` replays
at 20 x real time. `--from-dir DIR` reads `<Topic>.jsonStream` files instead
of downloading (see `sidecar/tests/fixtures/mini_race`, a hand-made 3-lap
race). Archived data is never written to disk.

## Development

### Plugin tests

The tests run against a checkout of
[FiestaBoard core](https://github.com/Fiestaboard/FiestaBoard) at `v9.13.0`,
with this repo linked as `plugins/f1`. They never touch the network (HTTP is
faked). `.github/workflows/ci.yml` is the canonical recipe; locally, from the
folder that *contains* this repo:

```bash
git clone --branch v9.13.0 https://github.com/Fiestaboard/FiestaBoard.git fiestaboard-core
python3.11 -m venv .venv-fb && source .venv-fb/bin/activate
pip install -r fiestaboard-core/requirements.txt pytest pytest-cov

mkdir -p plugins && touch plugins/__init__.py
ln -s ../fiestaboard-plugin--f1-live-standings plugins/f1

cd fiestaboard-plugin--f1-live-standings
PYTHONPATH="$(cd .. && pwd):$(cd ../fiestaboard-core && pwd)" \
BOARD_READ_WRITE_KEY=test_key \
python -m pytest tests/ -v --cov --cov-report=term-missing
```

Coverage is measured on the plugin package (`__init__.py`, `jolpica.py`) and
must be at least 80% (`.coveragerc`; `sidecar/` is not part of it). CI also
runs the host's `validate_manifest` and install check. There is deliberately
no `requirements.txt` in this repo: the host's install check scans the tree
for one, and the plugin's only dependency (`requests`) comes with
FiestaBoard. The sidecar's dependencies (FastF1 3.9.0) live only in
`sidecar/pyproject.toml` and `sidecar/uv.lock`.

### Sidecar tests

```bash
cd sidecar
UV_PROJECT_ENVIRONMENT=/tmp/f1-sidecar-venv uv run --locked pytest
```

Keep the venv outside the repo (`UV_PROJECT_ENVIRONMENT`): a `sidecar/.venv`
inside it makes the host's install check (and its plugin test) fail on files
of the installed packages. The tests use hand-made fixtures and never touch
the network.

## License

MIT, see [LICENSE](LICENSE).
