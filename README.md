# F1 Live Standings

A [FiestaBoard](https://github.com/Fiestaboard/FiestaBoard) plugin (id `f1`)
that puts Formula 1 on the board: the driver championship and the next Grand
Prix, from [Jolpica](https://api.jolpi.ca) (the free Ergast successor, no
key). Built for a Vestaboard Note (3 x 15): every value is upper-case ASCII
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

The live race grid (lap, track status, `LIVE`/`FINISHED`) is planned for a
later version.

## Settings

Only **Enabled**. Times use FiestaBoard's general timezone (Settings ->
General); UTC when it is not set.

## Variables

| Variable | Example | Notes |
|---|---|---|
| `f1.race_status` | `NEXT RACE` | Always `NEXT RACE` in this version |
| `f1.next_race` | `MEXICO CITY` | Jolpica race name without "Grand Prix"; empty when the season is over |
| `f1.next_race_time` | `SUN 21:00` | Race start, day and time in your timezone; empty when the season is over |

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
must be at least 80% (`.coveragerc`). CI also runs the host's
`validate_manifest` and install check. There is deliberately no
`requirements.txt` in this repo: the host's install check scans the tree for
one, and the only dependency (`requests`) comes with FiestaBoard.

## License

MIT, see [LICENSE](LICENSE).
