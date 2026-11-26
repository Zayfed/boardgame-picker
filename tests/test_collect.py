import json
from unittest.mock import Mock

import pytest
import requests

from boardgames.collect import collect
from boardgames.data import ROOT, DataError, load_games


def prepare_collection(tmp_path):
    games = load_games()[:2]
    (tmp_path / "html").mkdir()
    (tmp_path / "sources.json").write_text(
        json.dumps(
            {
                "source": "https://tesera.ru/",
                "selection": "Проверка",
                "urls": [game.source_url for game in games],
            }
        )
    )
    first = ROOT / "data/html" / f"{games[0].id}.html"
    (tmp_path / "html" / first.name).write_bytes(first.read_bytes())
    return games


def test_interrupted_collection_resumes_without_refetching_saved_cards(tmp_path, monkeypatch):
    games = prepare_collection(tmp_path)
    robots = Mock(text="User-agent: *\nAllow: /\n")
    page = Mock(text=(ROOT / "data/html" / f"{games[1].id}.html").read_text())
    session = Mock()
    session.headers = {}
    session.get.side_effect = [robots, page]
    monkeypatch.setattr("boardgames.collect.requests.Session", Mock(return_value=session))
    report = collect(tmp_path, offline=False, delay=1, resume=True)
    assert [call.args[0] for call in session.get.call_args_list] == [
        "https://tesera.ru/robots.txt",
        games[1].source_url,
    ]
    assert report["collected"] == 2
    assert report["reused"] == report["downloaded"] == 1
    rebuilt = load_games(tmp_path / "games.json")
    assert rebuilt[0] == games[0]
    assert rebuilt[1].id == games[1].id


def test_failed_resume_preserves_previous_dataset(tmp_path, monkeypatch):
    prepare_collection(tmp_path)
    previous = b"previous dataset"
    (tmp_path / "games.json").write_bytes(previous)
    session = Mock()
    session.headers = {}
    session.get.side_effect = [Mock(text="User-agent: *\nAllow: /\n"), requests.Timeout()]
    monkeypatch.setattr("boardgames.collect.requests.Session", Mock(return_value=session))
    with pytest.raises(DataError, match="Не удалось собрать"):
        collect(tmp_path, offline=False, delay=1, resume=True)
    assert (tmp_path / "games.json").read_bytes() == previous
    assert (tmp_path / "last_failed_collection.json").is_file()


def test_cached_card_must_belong_to_its_manifest_url(tmp_path):
    games = prepare_collection(tmp_path)
    path = tmp_path / "html" / f"{games[0].id}.html"
    path.write_text(path.read_text().replace(games[0].source_url, games[1].source_url))
    with pytest.raises(DataError, match="источник или дата"):
        collect(tmp_path, offline=True, delay=1)
