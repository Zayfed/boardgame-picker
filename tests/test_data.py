import hashlib
import json
from dataclasses import replace

import pytest

from boardgames.collect import parse_game, parse_interval
from boardgames.data import ROOT, DataError, load_games
from boardgames.selection import Preferences, select_games


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2 - 5", (2, 5)),
        ("30–60 мин", (30, 60)),
        ("от 30 мин", (30, None)),
        ("до 45 мин", (None, 45)),
        ("1 — 2 часа", (60, 120)),
        ("40 мин", (40, 40)),
        ("-", (None, None)),
    ],
)
def test_intervals(value, expected):
    assert parse_interval(value) == expected


def test_dataset_can_be_rebuilt_from_real_html():
    games = load_games()
    report = json.loads((ROOT / "data/quality_report.json").read_text())
    manifest = json.loads((ROOT / "data/sources.json").read_text())
    assert len(games) == len(report["provenance"]) == 300
    assert [game.source_url for game in games] == manifest["urls"]
    assert set(manifest["retained_urls"]) <= set(manifest["urls"])
    assert not {item["url"] for item in manifest["excluded_candidates"]} & set(manifest["urls"])
    for game, provenance in zip(games, report["provenance"], strict=True):
        html = (ROOT / "data" / provenance["html_file"]).read_text()
        assert hashlib.sha256(html.encode()).hexdigest() == provenance["sha256"]
        assert parse_game(html, game.source_url, game.collected_at) == game


def test_two_distinct_real_searches_and_empty_result():
    games = load_games()
    first = select_games(games, Preferences(4, 60, 18))
    second = select_games(games, Preferences(2, 45, 18))
    assert first and second
    assert {game.id for game in first} != {game.id for game in second}
    for game in first:
        assert game.players_min <= 4 <= game.players_max
        assert game.duration_max <= 60
        assert game.age_min <= 18
    assert not select_games(games, Preferences(20, 5, 1))


def test_unknown_time_and_age_are_not_invented():
    game = load_games()[0]
    preferences = Preferences(4, 600, 100)
    assert not select_games([replace(game, duration_max=None)], preferences)
    assert not select_games([replace(game, age_min=None)], preferences)


def test_topic_search_shortlists_birds_and_respects_time_limit():
    games = load_games()
    wish = "Хотим игру про птиц и наблюдение за природой"
    long_session = select_games(games, Preferences(4, 90, 18), wish)
    short_session = select_games(games, Preferences(4, 60, 18), wish)
    assert "wingspan" in {game.id for game in long_session[:8]}
    assert "wingspan" not in {game.id for game in short_session}


@pytest.mark.parametrize(
    "preferences",
    [
        Preferences(0, 60, 18),
        Preferences(4, -1, 18),
        Preferences(4, 60, 0),
        Preferences(True, 60, 18),
    ],
)
def test_invalid_preferences(preferences):
    with pytest.raises(ValueError):
        select_games(load_games(), preferences)


def test_duplicate_records_are_rejected(tmp_path):
    game = load_games()[0].to_dict()
    path = tmp_path / "games.json"
    path.write_text(json.dumps([game, game]))
    with pytest.raises(DataError, match="Повторный"):
        load_games(path)
