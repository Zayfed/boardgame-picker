import json
from unittest.mock import Mock

import pytest
import requests

from boardgames.ai import AIConfig, AIError, model_records, recommend
from boardgames.data import ROOT, load_games
from boardgames.selection import Preferences, select_games


def theme_response(theme, match):
    response = Mock()
    response.json.return_value = {
        "model": "qwen3.5:4b",
        "message": {"content": json.dumps({"main_theme": theme, "match": match})},
    }
    return response


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"message": {"content": "invalid-json"}},
        {"message": {"content": "[]"}},
        {"message": {"content": '{"main_theme": " ", "match": "direct"}'}},
        {"message": {"content": json.dumps({"main_theme": "x" * 301, "match": "direct"})}},
    ],
)
def test_malformed_theme_responses_are_rejected(monkeypatch, payload):
    response = Mock()
    response.json.return_value = payload
    monkeypatch.setattr("boardgames.ai.requests.post", Mock(return_value=response))
    with pytest.raises(AIError, match="непроверяемую оценку"):
        recommend(load_games(), Preferences(6, 180, 18), "Нравятся птицы")


def test_game_id_and_quote_come_from_the_source_even_if_the_model_invents_them(monkeypatch):
    game = next(game for game in load_games() if game.id == "tweeeet")
    response = theme_response("Птицы", "direct")
    response.json.return_value["message"]["content"] = json.dumps(
        {"main_theme": "Птицы", "match": "direct", "id": "invented", "evidence": "invented"}
    )
    monkeypatch.setattr("boardgames.ai.requests.post", Mock(return_value=response))
    result = recommend([game], Preferences(6, 180, 18), "Нравятся птицы")
    assert result[0].game_id == game.id
    assert result[0].evidence in game.description
    assert "птиц" in result[0].evidence.lower()


def test_model_failure_is_readable(monkeypatch):
    monkeypatch.setattr("boardgames.ai.requests.post", Mock(side_effect=requests.Timeout))
    with pytest.raises(AIError, match="недоступен"):
        recommend(load_games(), Preferences(4, 180, 18), "Хочу птиц")


def test_no_data_does_not_call_model(monkeypatch):
    post = Mock()
    monkeypatch.setattr("boardgames.ai.requests.post", post)
    with pytest.raises(AIError, match="Нет подходящих"):
        recommend([], Preferences(4, 60, 18), "Строительство")
    post.assert_not_called()


@pytest.mark.parametrize("wish", ["Хочу киберпанк", "Хочу виноделие"])
def test_absent_topic_returns_no_match_without_forcing_an_unrelated_game(monkeypatch, wish):
    post = Mock()
    monkeypatch.setattr("boardgames.ai.requests.post", post)
    preferences = Preferences(4, 180, 18)
    unrelated = [game for game in load_games() if game.id == "wingspan"]
    result = recommend(select_games(unrelated, preferences, wish), preferences, wish)
    assert result == []
    post.assert_not_called()


@pytest.mark.parametrize(
    ("wish", "expected"),
    [
        ("Хочу наблюдать за птицами", {"wingspan"}),
        ("Хочу строить средневековый город", {"carcassonne", "carcassonne-new-edition"}),
        ("Хочу игру про животных", {"harmonies", "robinson-crusoe-adventures-cursed-island"}),
        ("Хочу освоение космоса", {"race-for-the-galaxy"}),
    ],
)
def test_thematic_candidates_and_evidence_change_with_the_wish(wish, expected):
    games = select_games(load_games(), Preferences(4, 180, 18), wish)
    records = model_records(games, wish)
    assert records
    ids = {record["id"] for record in records}
    assert ids & expected
    assert "welcome-to-the-moon" not in ids
    current = {game.id: game for game in games}
    for record in records:
        for quote in record["evidence_options"]:
            assert quote in current[record["id"]].description


@pytest.mark.parametrize("match", ["none", "partial"])
def test_incidental_bird_comparison_is_not_presented_as_a_thematic_match(monkeypatch, match):
    game = next(game for game in load_games() if game.id == "carcassonne-big-box-6")
    post = Mock(return_value=theme_response("Освоение территорий", match))
    monkeypatch.setattr("boardgames.ai.requests.post", post)
    result = recommend([game], Preferences(4, 60, 18), "нравятся птицы")
    assert not result
    post.assert_called_once()


def test_rejected_candidates_do_not_prevent_a_later_direct_match(monkeypatch):
    post = Mock(
        side_effect=[
            theme_response("Садоводство", "partial"),
            theme_response("Освоение территорий", "none"),
            theme_response("Птицы", "direct"),
        ]
    )
    monkeypatch.setattr("boardgames.ai.requests.post", post)
    preferences = Preferences(4, 90, 18)
    legacy_candidates = [
        game
        for game in load_games()
        if game.id in {"azul-queens-garden", "carcassonne-big-box-6", "wingspan"}
    ]
    games = select_games(legacy_candidates, preferences, "нравятся птицы")
    result = recommend(games, preferences, "нравятся птицы")
    assert [item.game_id for item in result] == ["wingspan"]
    assert "птиц" in result[0].evidence
    assert post.call_count == 3


@pytest.mark.parametrize("match", [True, "maybe", None])
def test_invalid_thematic_assessment_is_not_accepted(monkeypatch, match):
    monkeypatch.setattr(
        "boardgames.ai.requests.post", Mock(return_value=theme_response("Птицы", match))
    )
    with pytest.raises(AIError, match="непроверяемую оценку"):
        recommend(load_games(), Preferences(4, 180, 18), "Хочу птиц")


def test_recorded_live_semantic_decisions_are_replayed_against_the_same_source(monkeypatch):
    report = json.loads((ROOT / "examples/ai_quality_live.json").read_text())
    for case in report["cases"]:
        assessments = case["evidence"].get("assessments", [])
        responses = []
        for assessment in assessments:
            response = Mock()
            response.json.return_value = {
                "model": report["model"],
                "message": {"content": assessment["raw_response"]},
            }
            responses.append(response)
        post = Mock(side_effect=responses)
        monkeypatch.setattr("boardgames.ai.requests.post", post)
        preferences = Preferences(**case["preferences"])
        result = recommend(
            select_games(load_games(), preferences, case["wish"]),
            preferences,
            case["wish"],
            AIConfig(model=report["model"]),
        )
        assert [item.game_id for item in result] == case["ids"]
        assert post.call_count == len(assessments)
        for call, assessment in zip(post.call_args_list, assessments, strict=True):
            actual_input = json.loads(call.kwargs["json"]["messages"][1]["content"])
            assert actual_input == assessment["input"]


def test_saved_live_response_uses_current_data():
    path = ROOT / "examples/ai_live.json"
    evidence = json.loads(path.read_text())
    assert evidence["model"]
    assert evidence["called_at"]
    records = evidence["input"]["games"]
    current = {game.id: game for game in load_games()}
    for record in records:
        assert record["description"] == current[record["id"]].description[:2400]
    assert evidence["method"] == "primary-theme-classification"
    last = evidence["assessments"][-1]
    assert json.loads(evidence["raw_response"])["match"] == "direct"
    assert last["id"] == evidence["recommendations"][0]["game_id"]
    quote = evidence["recommendations"][0]["evidence"]
    assert quote in current[last["id"]].description
