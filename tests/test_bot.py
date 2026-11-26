import asyncio
import json
import re
from itertools import count
from unittest.mock import AsyncMock, Mock

import pytest
from telegram import Update
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.request import BaseRequest

from boardgames.ai import AIError, Recommendation
from boardgames.bot import build_application, complete_ai, format_game
from boardgames.data import ROOT, load_games
from boardgames.selection import Preferences, select_games


class TelegramStub(BaseRequest):
    def __init__(self):
        self.calls = []
        self.ids = count(100)
        self.sent = []
        self.send_errors = []
        self.callback_error = None
        self.typing_error = None

    @property
    def read_timeout(self):
        return 5

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        action = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        self.calls.append((action, params))
        if action == "sendChatAction" and self.typing_error:
            raise self.typing_error
        if action == "sendMessage" and self.send_errors:
            error = self.send_errors.pop(0)
            if isinstance(error, Exception):
                raise error
            return error, json.dumps(
                {
                    "ok": False,
                    "error_code": error,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 0},
                }
            ).encode()
        if action == "answerCallbackQuery" and self.callback_error:
            error = self.callback_error
            self.callback_error = None
            raise error
        if action == "getMe":
            result = {"id": 42, "is_bot": True, "first_name": "Picker", "username": "picker_bot"}
        elif action == "sendMessage":
            self.sent.append(params["text"])
            result = {
                "message_id": next(self.ids),
                "date": 1800000000,
                "chat": {"id": int(params["chat_id"]), "type": "private"},
                "text": params["text"],
            }
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()

    def messages(self):
        return self.sent


async def send(application, sequence, text=None, callback=None, user_id=1):
    user = {"id": user_id, "is_bot": False, "first_name": "Test"}
    message = {
        "message_id": sequence,
        "date": 1800000000,
        "chat": {"id": user_id, "type": "private"},
        "from": user,
    }
    if callback:
        message["text"] = "Кнопки"
        message["from"] = {"id": 42, "is_bot": True, "first_name": "Picker"}
        payload = {
            "update_id": sequence,
            "callback_query": {
                "id": str(sequence),
                "chat_instance": "test",
                "from": user,
                "data": callback,
                "message": message,
            },
        }
    else:
        message["text"] = text
        if text.startswith("/"):
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text)}]
        payload = {"update_id": sequence, "message": message}
    await application.process_update(Update.de_json(payload, application.bot))


def test_complete_dialog_buttons_paging_validation_and_empty_result():
    async def scenario():
        transport = TelegramStub()
        application = build_application("123456:offline-test", load_games(), transport)
        async with application:
            await send(application, 1, text="/start")
            await send(application, 2, callback="find")
            await send(application, 3, text="четверо")
            assert "целое число игроков" in transport.messages()[-1]
            await send(application, 4, callback="players:4")
            await send(application, 5, text="0")
            assert "целое число минут" in transport.messages()[-1]
            await send(application, 6, callback="minutes:60")
            await send(application, 7, callback="age:18")
            await send(application, 8, callback="skip")
            assert any("Нашёл" in message for message in transport.messages())
            assert sum("Открыть на Тесере" in message for message in transport.messages()) == 3
            await send(application, 9, callback="more")
            assert sum("Открыть на Тесере" in message for message in transport.messages()) == 6
            await send(application, 10, text="/find")
            await send(application, 11, text="21")
            assert "целое число игроков от 1 до 20" in transport.messages()[-1]
            await send(application, 12, text="/find")
            await send(application, 13, text="4")
            await send(application, 14, text="5")
            await send(application, 15, text="1")
            assert "игр в моей выборке нет" in transport.messages()[-1]
            await send(application, 16, text="/cancel")
            await send(application, 17, callback="more")
            assert "другому шагу" in transport.messages()[-1]

    asyncio.run(scenario())


@pytest.mark.parametrize("players", [8, 20])
def test_unsupported_group_is_explained_before_other_questions(monkeypatch, players):
    model = Mock()
    monkeypatch.setattr("boardgames.bot.recommend", model)

    async def scenario():
        transport = TelegramStub()
        limited_catalog = [game for game in load_games() if game.players_max == 7]
        application = build_application("123456:offline-test", limited_catalog, transport)
        async with application:
            await send(application, 1, text="/find")
            assert "players:7" in str(transport.calls)
            assert "players:8" not in str(transport.calls)
            await send(application, 2, text=str(players))
            assert f"нет игры на {players} участников" in transport.messages()[-1]
            assert "максимум на 7 участников" in transport.messages()[-1]
            assert not any("2/4" in message or "4/4" in message for message in transport.messages())
            await send(application, 3, text="7")
            assert "2/4" in transport.messages()[-1]
            model.assert_not_called()

    asyncio.run(scenario())


def test_empty_parameter_combination_does_not_ask_for_a_wish(monkeypatch):
    model = Mock()
    monkeypatch.setattr("boardgames.bot.recommend", model)

    async def scenario():
        transport = TelegramStub()
        application = build_application("123456:offline-test", load_games(), transport)
        async with application:
            for sequence, text in enumerate(("/find", "4", "5", "1"), 1):
                await send(application, sequence, text=text)
            assert "игр в моей выборке нет" in transport.messages()[-1]
            assert "игроков — 4, до 5 минут, младшему 1 лет" in transport.messages()[-1]
            assert not any("4/4" in message for message in transport.messages())
            model.assert_not_called()

    asyncio.run(scenario())


def test_expanded_catalog_supports_eight_players_through_the_complete_dialog():
    games = load_games()
    candidates = select_games(games, Preferences(8, 180, 18))
    assert len(candidates) >= 3

    async def scenario():
        transport = TelegramStub()
        application = build_application("123456:offline-test", games, transport)
        async with application:
            await send(application, 1, text="/find")
            assert "players:8" in str(transport.calls)
            await send(application, 2, callback="players:8")
            assert "2/4" in transport.messages()[-1]
            await send(application, 3, callback="minutes:180")
            await send(application, 4, callback="age:18")
            assert "4/4" in transport.messages()[-1]
            await send(application, 5, callback="skip")
            ids = re.findall(r"ID: <code>([^<]+)</code>", "\n".join(transport.messages()))
            assert ids == [game.id for game in candidates[:3]]

    asyncio.run(scenario())


def test_two_users_do_not_share_preferences():
    async def scenario():
        transport = TelegramStub()
        application = build_application("123456:offline-test", load_games(), transport)
        async with application:
            await send(application, 1, text="/find", user_id=1)
            await send(application, 2, text="/find", user_id=2)
            await send(application, 3, text="4", user_id=1)
            await send(application, 4, text="2", user_id=2)
            assert application.user_data[1]["players"] == 4
            assert application.user_data[2]["players"] == 2

    asyncio.run(scenario())


def test_start_reply_recovers_from_timeout_and_rate_limit():
    async def scenario():
        transport = TelegramStub()
        transport.send_errors = [TimedOut(), 429]
        application = build_application("123456:offline-test", load_games(), transport)
        async with application:
            await send(application, 1, text="/start")
            assert len(transport.messages()) == 1
            assert "Во что сыграть?" in transport.messages()[0]
            await send(application, 2, callback="find")
            assert "Сколько вас будет?" in transport.messages()[-1]

    asyncio.run(scenario())


def test_expired_callback_does_not_block_dialog():
    async def scenario():
        transport = TelegramStub()
        application = build_application("123456:offline-test", load_games(), transport)
        async with application:
            await send(application, 1, text="/start")
            transport.callback_error = BadRequest("Query is too old")
            await send(application, 2, callback="find")
            assert "Сколько вас будет?" in transport.messages()[-1]
            await send(application, 3, callback="players:4")
            assert "Сколько минут" in transport.messages()[-1]

    asyncio.run(scenario())


def test_hung_callback_acknowledgement_does_not_hold_up_dialog():
    class HungCallback(TelegramStub):
        async def do_request(self, url, method, **kwargs):
            if url.endswith("/answerCallbackQuery"):
                await asyncio.Event().wait()
            return await super().do_request(url, method, **kwargs)

    async def scenario():
        transport = HungCallback()
        application = build_application("123456:offline-test", load_games(), transport)
        async with application:
            await asyncio.wait_for(send(application, 1, callback="find"), timeout=2)
            assert "Сколько вас будет?" in transport.messages()[-1]

    asyncio.run(scenario())


def test_ai_result_arrives_when_typing_api_is_unavailable(monkeypatch):
    game = load_games()[0]
    recommendation = Recommendation(game.id, "Выбрана игра", "Цитата")
    model = Mock(return_value=[recommendation])
    monkeypatch.setattr("boardgames.bot.recommend", model)

    async def scenario():
        transport = TelegramStub()
        transport.typing_error = TimedOut()
        application = build_application("123456:offline-test", [game], transport)
        async with application:
            await application.start()
            try:
                await send(application, 1, text="/find")
                await send(application, 2, text=str(game.players_min))
                await send(application, 3, text=str(game.duration_max or 600))
                await send(application, 4, text="18")
                await send(application, 5, text="Хочу киберпанк")
            finally:
                await application.stop()
            model.assert_called_once()
            assert any("Выбрана игра" in text for text in transport.messages())
            assert not any("Не удалось обработать" in text for text in transport.messages())
            assert not application.user_data[1]["busy"]

    asyncio.run(scenario())


def test_six_player_bird_request_delivers_the_recorded_real_model_recommendation(monkeypatch):
    report = json.loads((ROOT / "examples/ai_quality_live.json").read_text())
    case = next(
        case
        for case in report["cases"]
        if case["wish"] == "Нравятся птицы" and case["preferences"]["players"] == 6
    )
    assert case["ids"] == ["tweeeet"]
    responses = []
    for assessment in case["evidence"]["assessments"]:
        response = Mock()
        response.json.return_value = {
            "model": report["model"],
            "message": {"content": assessment["raw_response"]},
        }
        responses.append(response)
    post = Mock(side_effect=responses)
    monkeypatch.setattr("boardgames.ai.requests.post", post)

    async def scenario():
        transport = TelegramStub()
        application = build_application("123456:offline-test", load_games(), transport)
        async with application:
            await application.start()
            try:
                for sequence, text in enumerate(("/find", "6", "180", "18", case["wish"]), 1):
                    await send(application, sequence, text=text)
            finally:
                await application.stop()
            cards = [text for text in transport.messages() if "Открыть на Тесере" in text]
            assert len(cards) == 1
            assert "ID: <code>tweeeet</code>" in cards[0]
            assert "2 - 6 игроков" in cards[0]
            assert "Птицы" in cards[0]
            assert not any("не нашёл подтверждения" in text for text in transport.messages())
            assert post.call_count == len(responses)

    asyncio.run(scenario())


def test_ai_recommendation_is_not_repeated_on_any_following_page(monkeypatch):
    candidates = select_games(load_games(), Preferences(4, 180, 18), "Хочу птиц")
    selected = candidates[2]
    monkeypatch.setattr(
        "boardgames.bot.recommend",
        Mock(return_value=[Recommendation(selected.id, "Выбор", "Цитата")]),
    )

    async def scenario():
        transport = TelegramStub()
        application = build_application("123456:offline-test", load_games(), transport)
        async with application:
            await application.start()
            try:
                for sequence, text in enumerate(("/find", "4", "180", "18", "Хочу птиц"), 1):
                    await send(application, sequence, text=text)
            finally:
                await application.stop()
            for sequence in range(6, 6 + len(candidates)):
                if len(application.user_data[1]["shown_ids"]) == len(candidates):
                    break
                await send(application, sequence, callback="more")
            ids = re.findall(r"ID: <code>([^<]+)</code>", "\n".join(transport.messages()))
            assert ids[0] == selected.id
            assert len(ids) == len(set(ids)) == len(candidates)
            assert set(ids) == {game.id for game in candidates}

    asyncio.run(scenario())


@pytest.mark.parametrize(("failed_index", "expected_count"), [(2, 5), (5, 8)])
def test_page_retry_skips_cards_confirmed_before_a_network_failure(failed_index, expected_count):
    candidates = select_games(load_games(), Preferences(4, 180, 18))

    class PartialFailure(TelegramStub):
        failures = 3

        async def do_request(self, url, method, request_data=None, **kwargs):
            params = request_data.parameters if request_data else {}
            if (
                url.endswith("/sendMessage")
                and f"<code>{candidates[failed_index].id}</code>" in params.get("text", "")
                and self.failures
            ):
                self.failures -= 1
                raise NetworkError("temporary send failure")
            return await super().do_request(url, method, request_data=request_data, **kwargs)

    async def scenario():
        transport = PartialFailure()
        application = build_application("123456:offline-test", load_games(), transport)
        async with application:
            for sequence, text in enumerate(("/find", "4", "180", "18"), 1):
                await send(application, sequence, text=text)
            await send(application, 5, callback="skip")
            if failed_index == 5:
                await send(application, 6, callback="more")
            assert len(application.user_data[1]["shown_ids"]) == failed_index
            await send(application, 7, callback="skip" if failed_index == 2 else "more")
            ids = re.findall(r"ID: <code>([^<]+)</code>", "\n".join(transport.messages()))
            assert ids == [game.id for game in candidates[:expected_count]]

    asyncio.run(scenario())


@pytest.mark.parametrize(("players", "wish"), [(4, "Киберпанк"), (6, "Нравятся птицы")])
def test_no_thematic_match_does_not_silently_show_an_unrelated_ai_card(monkeypatch, players, wish):
    monkeypatch.setattr("boardgames.bot.recommend", Mock(return_value=[]))

    async def scenario():
        transport = TelegramStub()
        application = build_application("123456:offline-test", load_games(), transport)
        async with application:
            await application.start()
            try:
                for sequence, text in enumerate(("/find", str(players), "180", "18", wish), 1):
                    await send(application, sequence, text=text)
            finally:
                await application.stop()
            assert "не нашёл подтверждения" in transport.messages()[-1]
            assert f"игроков — {players}" in transport.messages()[-1]
            assert "до 180 минут" in transport.messages()[-1]
            assert "большим временем" not in transport.messages()[-1]
            assert not any("Открыть на Тесере" in text for text in transport.messages())
            await send(application, 6, callback="more")
            assert sum("Открыть на Тесере" in text for text in transport.messages()) == 3

    asyncio.run(scenario())


def test_permanent_api_error_is_not_retried_as_network_failure():
    async def scenario():
        transport = TelegramStub()
        transport.send_errors = [BadRequest("Invalid request")]
        application = build_application("123456:offline-test", load_games(), transport)
        async with application:
            await send(application, 1, text="/start")
            assert len([method for method, _ in transport.calls if method == "sendMessage"]) == 2
            assert "Не удалось обработать запрос" in transport.messages()[-1]

    asyncio.run(scenario())


def test_ai_failure_still_sends_games_and_cancellation_suppresses_old_answer(monkeypatch):
    monkeypatch.setattr("boardgames.bot.recommend", Mock(side_effect=AIError("ИИ недоступен")))

    async def scenario():
        application = Mock()
        application.bot.send_message = AsyncMock()
        state = {"session": "active", "busy": True}
        await complete_ai(
            application,
            1,
            state,
            "active",
            load_games()[:3],
            Preferences(4, 60, 18),
            "Строительство",
        )
        assert application.bot.send_message.await_count == 5
        assert not state["busy"]
        application.bot.send_message.reset_mock()
        await complete_ai(
            application, 1, {}, "old", load_games()[:3], Preferences(4, 60, 18), "Строительство"
        )
        application.bot.send_message.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [TimedOut(), BadRequest("Invalid request")])
def test_ai_delivery_recovers_or_releases_busy_state(monkeypatch, failure):
    game = load_games()[0]
    monkeypatch.setattr(
        "boardgames.bot.recommend",
        Mock(return_value=[Recommendation(game.id, "Выбрана игра", "Цитата")]),
    )

    async def scenario():
        application = Mock()
        application.bot.send_message = AsyncMock(side_effect=[failure, Mock(), Mock()])
        state = {"session": "active", "busy": True}
        arguments = (application, 1, state, "active", [game], Preferences(4, 180, 18), "Киберпанк")
        if isinstance(failure, BadRequest):
            with pytest.raises(BadRequest):
                await complete_ai(*arguments)
            assert application.bot.send_message.await_count == 1
        else:
            await complete_ai(*arguments)
            assert application.bot.send_message.await_count == 3
        assert not state["busy"]

    asyncio.run(scenario())


def test_game_cards_fit_telegram_limit():
    for game in load_games():
        assert len(format_game(game)) < 4096
