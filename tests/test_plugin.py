"""F1 plugin: variables, caching, back-off and the I/O edge-case matrix."""

from datetime import datetime, timedelta, timezone

import pytest
import requests

from .conftest import (
    DEFAULT_ROWS,
    NEXT,
    STANDINGS,
    FakeResponse,
    driver_row,
    next_body,
    standings_body,
)


def all_values(data):
    yield data["race_status"]
    yield data["next_race"]
    yield data["next_race_time"]
    for row in data["standings"]:
        yield from row.values()


def assert_board_safe(data):
    for value in all_values(data):
        assert isinstance(value, str)
        assert len(value) <= 15, value
        assert value == value.upper()
        assert value.isascii()


# ── manifest ─────────────────────────────────────────────────────────────────


def test_manifest_is_valid_for_core(manifest):
    from src.plugins.manifest import validate_manifest

    ok, errors = validate_manifest(manifest)
    assert ok, errors


def test_manifest_install_check_passes(manifest):
    from pathlib import Path

    from src.plugins.install_check import validate_install
    from src.plugins.manifest import PluginManifest

    plugin_dir = Path(__file__).resolve().parent.parent
    result = validate_install(manifest["id"], plugin_dir, PluginManifest.from_dict(manifest))
    assert result.ok, result.errors


def test_manifest_declares_variables(manifest):
    assert manifest["id"] == "f1"
    assert manifest["version"] == "0.1.0"
    assert set(manifest["settings_schema"]["properties"]) == {"enabled"}
    assert set(manifest["variables"]["simple"]) == {"race_status", "next_race", "next_race_time"}
    array = manifest["variables"]["arrays"]["standings"]
    assert array["label_field"] == "code"
    assert array["item_fields"] == ["position", "code", "name", "points", "wins"]
    assert all(limit == 15 for limit in manifest["max_lengths"].values())
    assert "standings.*.code" in manifest["max_lengths"]
    assert "formatted_lines" not in manifest and "demo" not in manifest


def test_no_requirements_txt():
    from pathlib import Path

    assert not (Path(__file__).resolve().parent.parent / "requirements.txt").exists()


# ── happy path ───────────────────────────────────────────────────────────────


def test_happy_path(make_plugin):
    plugin = make_plugin()
    assert plugin.plugin_id == "f1"
    result = plugin.fetch_data()
    assert result.available and result.error is None
    assert result.formatted_lines is None
    data = result.data
    assert data["race_status"] == "NEXT RACE"
    assert data["next_race"] == "BRAZILIAN"
    assert data["next_race_time"] == "SUN 18:00"
    assert data["standings"][0] == {
        "position": "1",
        "code": "ANT",
        "name": "ANTONELLI",
        "points": "380",
        "wins": "9",
    }
    assert [row["code"] for row in data["standings"]] == ["ANT", "VER", "HAM"]
    assert_board_safe(data)


def test_template_renders_leader(make_plugin):
    """{{f1.standings.0.code}} {{f1.standings.0.points}} through core's engine."""
    from src.templates.engine import TemplateEngine

    data = make_plugin().fetch_data().data
    engine = TemplateEngine()
    rendered = engine.render("{{f1.standings.0.code}} {{f1.standings.0.points}}", {"f1": data})
    assert rendered == "ANT 380"
    assert engine.render("{{f1.next_race}}", {"f1": data}) == "BRAZILIAN"


def test_utc_when_timezone_unset(make_plugin):
    data = make_plugin(tz="").fetch_data().data
    assert data["next_race_time"] == "SUN 17:00"


def test_unknown_timezone_falls_back_to_utc(make_plugin):
    data = make_plugin(tz="Mars/Olympus").fetch_data().data
    assert data["next_race_time"] == "SUN 17:00"


def test_timezone_can_change_the_day(make_plugin, session):
    session.routes[NEXT] = FakeResponse(200, next_body("Las Vegas Grand Prix", "2026-11-22", "04:00:00Z"))
    data = make_plugin(tz="America/Los_Angeles").fetch_data().data
    assert data["next_race"] == "LAS VEGAS"
    assert data["next_race_time"] == "SAT 20:00"


def test_reads_general_timezone_from_core(manifest, monkeypatch):
    import plugins.f1 as f1

    monkeypatch.setattr("src.config.Config.GENERAL_TIMEZONE", "Europe/Brussels", raising=False)
    assert f1.general_timezone_name() == "Europe/Brussels"
    assert f1.F1Plugin(manifest)._timezone_name is f1.general_timezone_name


def test_race_without_start_time(make_plugin, session):
    session.routes[NEXT] = FakeResponse(200, next_body(time=None))
    data = make_plugin().fetch_data().data
    assert data["next_race_time"] == "SUN"


def test_race_without_start_time_west_of_utc(make_plugin, session):
    session.routes[NEXT] = FakeResponse(200, next_body(time=None))
    data = make_plugin(tz="America/New_York").fetch_data().data
    assert data["next_race_time"] == "SUN"


# ── names, codes, points ─────────────────────────────────────────────────────


def test_accents_and_long_names(make_plugin, session):
    session.routes[STANDINGS] = FakeResponse(
        200,
        standings_body(
            [
                driver_row(1, "HUL", "Hülkenberg", 50, 0),
                driver_row(2, "XXX", "Wolfeschlegelsteinhausenberger", 10, 0),
            ]
        ),
    )
    session.routes[NEXT] = FakeResponse(200, next_body("Emilia Romagna Grand Prix Made Of Long Words"))
    data = make_plugin().fetch_data().data
    assert data["standings"][0]["name"] == "HULKENBERG"
    assert data["standings"][1]["name"] == "WOLFESCHLEGELST"
    assert data["next_race"] == "EMILIA ROMAGNA"
    assert_board_safe(data)


def test_next_race_names(make_plugin, session):
    from plugins.f1 import short_race_name

    assert short_race_name("Mexico City Grand Prix") == "MEXICO CITY"
    assert short_race_name("São Paulo Grand Prix") == "SAO PAULO"
    assert short_race_name("Abu Dhabi Grand Prix") == "ABU DHABI"
    assert short_race_name("Grand Prix") == "GRAND PRIX"
    assert short_race_name("Saudi Arabian Grand Prix") == "SAUDI ARABIAN"


def test_missing_code_uses_family_name(make_plugin, session):
    session.routes[STANDINGS] = FakeResponse(
        200, standings_body([driver_row(1, None, "Hülkenberg", 50, 0), driver_row(2, "", "de Vries", 1, 0)])
    )
    data = make_plugin().fetch_data().data
    assert data["standings"][0]["code"] == "HUL"
    assert data["standings"][1]["code"] == "DEV"


def test_half_points(make_plugin, session):
    session.routes[STANDINGS] = FakeResponse(
        200, standings_body([driver_row(1, "ANT", "Antonelli", "12.5", 0), driver_row(2, "VER", "Verstappen", "12.0", 0)])
    )
    data = make_plugin().fetch_data().data
    assert data["standings"][0]["points"] == "12.5"
    assert data["standings"][1]["points"] == "12"


def test_format_points_non_numeric():
    from plugins.f1 import format_points

    assert format_points("n/a") == "N/A"
    assert format_points(None) == ""


# ── season edges ─────────────────────────────────────────────────────────────


def test_season_over(make_plugin, session):
    session.routes[NEXT] = FakeResponse(200, next_body(None))
    result = make_plugin().fetch_data()
    assert result.available
    assert result.data["next_race"] == ""
    assert result.data["next_race_time"] == ""
    assert result.data["race_status"] == "NEXT RACE"
    assert len(result.data["standings"]) == 3


def test_new_season_no_standings(make_plugin, session):
    session.routes[STANDINGS] = FakeResponse(200, standings_body(None))
    result = make_plugin().fetch_data()
    assert result.available
    assert result.data["standings"] == []
    assert result.data["next_race"] == "BRAZILIAN"


# ── caching and request economy ──────────────────────────────────────────────


def test_second_fetch_within_hour_makes_no_request(make_plugin, session, clock):
    plugin = make_plugin()
    plugin.fetch_data()
    assert len(session.calls) == 2
    clock.advance(3599)
    result = plugin.fetch_data()
    assert len(session.calls) == 2
    assert result.data["standings"][0]["code"] == "ANT"


def test_hourly_refresh(make_plugin, session, clock):
    plugin = make_plugin()
    plugin.fetch_data()
    session.routes[STANDINGS] = FakeResponse(
        200, standings_body([driver_row(1, "VER", "Verstappen", 400, 10)] + DEFAULT_ROWS[:1])
    )
    clock.advance(3600)
    data = plugin.fetch_data().data
    assert session.paths() == [STANDINGS, NEXT, STANDINGS, NEXT]
    assert data["standings"][0]["code"] == "VER"


def test_standings_request_asks_for_whole_grid(make_plugin, session):
    make_plugin().fetch_data()
    assert session.calls[0]["params"] == {"limit": 100}
    assert session.calls[0]["timeout"] == 10


def test_race_start_passed_refetches_next_race(make_plugin, session, clock, now_holder):
    plugin = make_plugin()
    plugin.fetch_data()
    # Sunday, 10 minutes after the 17:00Z start; only 10 minutes of clock.
    now_holder["now"] = datetime(2026, 11, 8, 17, 10, tzinfo=timezone.utc)
    clock.advance(600)
    plugin.fetch_data()
    assert session.paths() == [STANDINGS, NEXT, NEXT]

    # Jolpica still lists Brazil: no refetch until 15 minutes later.
    clock.advance(60)
    plugin.fetch_data()
    assert session.paths().count(NEXT) == 2
    clock.advance(15 * 60)
    session.routes[NEXT] = FakeResponse(200, next_body("Las Vegas Grand Prix", "2026-11-22", "04:00:00Z"))
    data = plugin.fetch_data().data
    assert session.paths().count(NEXT) == 3
    assert data["next_race"] == "LAS VEGAS"
    assert data["next_race_time"] == "SUN 05:00"

    # Future race again: back to hourly.
    clock.advance(60)
    plugin.fetch_data()
    assert session.paths().count(NEXT) == 3


# ── Jolpica down ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "failure",
    [
        requests.Timeout("slow"),
        requests.ConnectionError("down"),
        FakeResponse(503, {}),
        FakeResponse(200, ValueError("not json")),
        FakeResponse(200, {"nope": 1}),
    ],
    ids=["timeout", "connection", "5xx", "bad-json", "bad-shape"],
)
def test_down_after_success_keeps_last_values(make_plugin, session, clock, caplog, failure):
    plugin = make_plugin()
    good = plugin.fetch_data().data
    session.routes[STANDINGS] = failure
    session.routes[NEXT] = failure
    clock.advance(3600)
    result = plugin.fetch_data()
    assert result.available
    assert result.data == good
    assert "Jolpica" in caplog.text
    assert len(session.calls) == 4

    # Back-off: no request for 5 minutes, then retried.
    clock.advance(299)
    assert plugin.fetch_data().data == good
    assert len(session.calls) == 4
    session.routes[STANDINGS] = FakeResponse(200, standings_body([driver_row(1, "VER", "Verstappen", 1, 1)]))
    session.routes[NEXT] = FakeResponse(200, next_body("Qatar Grand Prix", "2026-11-29", "16:00:00Z"))
    clock.advance(1)
    data = plugin.fetch_data().data
    assert len(session.calls) == 6
    assert data["standings"][0]["code"] == "VER"
    assert data["next_race"] == "QATAR"


def test_down_with_nothing_cached(make_plugin, session, clock):
    session.routes[STANDINGS] = requests.Timeout("slow")
    session.routes[NEXT] = FakeResponse(500, {})
    plugin = make_plugin()
    result = plugin.fetch_data()
    assert not result.available
    assert result.error == "Jolpica unavailable"

    # Retried on the next update, no back-off.
    session.routes[STANDINGS] = FakeResponse(200, standings_body(DEFAULT_ROWS))
    session.routes[NEXT] = FakeResponse(200, next_body())
    result = plugin.fetch_data()
    assert result.available
    assert len(session.calls) == 4
    assert result.data["next_race"] == "BRAZILIAN"


def test_one_endpoint_down_still_available(make_plugin, session):
    session.routes[NEXT] = FakeResponse(502, {})
    result = make_plugin().fetch_data()
    assert result.available
    assert result.data["next_race"] == ""
    assert result.data["standings"][0]["code"] == "ANT"


def test_default_client_uses_requests_session(manifest):
    import plugins.f1 as f1

    plugin = f1.F1Plugin(manifest)
    client = plugin._get_client()
    assert isinstance(client.session, requests.Session)
    assert plugin._get_client() is client
    assert plugin._now().tzinfo is not None


def test_format_start_helper():
    from plugins.f1 import format_start

    start = datetime(2026, 11, 8, 17, 0, tzinfo=timezone.utc)
    assert format_start(start, timezone(timedelta(hours=-3))) == "SUN 14:00"
    assert format_start(start, timezone.utc, has_time=False) == "SUN"
