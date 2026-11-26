import asyncio
import logging
import os
import secrets
import time
from html import escape
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from boardgames.ai import AIError, Recommendation, recommend
from boardgames.data import DEFAULT_DATA, DataError, Game, load_games
from boardgames.polling import telegram_request
from boardgames.selection import Preferences, select_games, supports_players

PLAYERS, MINUTES, AGE, WISH, RESULTS = range(5)
LOGGER = logging.getLogger(__name__)


def keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in rows]
    )


def result_keyboard(has_more: bool, has_results: bool = True) -> InlineKeyboardMarkup:
    rows = [[("✨ Уточнить с ИИ", "ai"), ("🔄 Новая подборка", "find")]]
    if has_more:
        label = "Ещё три игры →" if has_results else "Показать игры по параметрам →"
        rows.insert(0, [(label, "more")])
    return keyboard(rows)


def player_keyboard(games: list[Game]) -> InlineKeyboardMarkup:
    choices = [
        (str(n), f"players:{n}")
        for n in (2, 3, 4, 5, 6, 7, 8)
        if any(supports_players(game, n) for game in games)
    ]
    return keyboard([choices[start : start + 3] for start in range(0, len(choices), 3)])


def format_game(game: Game, recommendation: Recommendation | None = None) -> str:
    parts = [
        f"🎲 <b>{escape(game.name)}</b>",
        f"👥 {escape(game.players_text or 'не указано')} игроков · "
        f"⏱ {escape(game.duration_text or 'не указано')} · "
        f"{game.age_min if game.age_min is not None else '?'}+",
    ]
    if game.tags:
        parts.append(" · ".join(escape(tag) for tag in game.tags[:5]))
    if recommendation:
        parts.extend(
            [
                f"\n✨ {escape(recommendation.reason)}",
                f"\nВ описании источника: «{escape(recommendation.evidence)}»",
            ]
        )
    elif game.description:
        description = game.description[:450]
        parts.append("\n" + escape(description) + ("…" if len(game.description) > 450 else ""))
    parts.extend(
        [
            f'\n<a href="{escape(game.source_url, quote=True)}">Открыть на Тесере ↗</a>',
            f"Сбор: {escape(game.collected_at[:10])} · ID: <code>{escape(game.id)}</code>",
        ]
    )
    return "\n".join(parts)


async def send_text(bot, chat_id: int, text: str, *, update_id=None, **kwargs) -> None:
    for attempt in range(3):
        try:
            sent = await bot.send_message(
                chat_id,
                text,
                parse_mode=ParseMode.HTML,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                **kwargs,
            )
            LOGGER.info("Ответ отправлен: update_id=%s message_id=%s", update_id, sent.message_id)
            return
        except RetryAfter as error:
            if attempt == 2:
                raise
            delay = error.retry_after
            if hasattr(delay, "total_seconds"):
                delay = delay.total_seconds()
            delay += 0.5
        except BadRequest:
            raise
        except NetworkError:
            if attempt == 2:
                raise
            delay = attempt + 1
        LOGGER.warning("Повтор отправки ответа: update_id=%s attempt=%s", update_id, attempt + 2)
        await asyncio.sleep(delay)


async def answer(update: Update, text: str, **kwargs) -> None:
    await send_text(
        update.get_bot(), update.effective_chat.id, text, update_id=update.update_id, **kwargs
    )


async def acknowledge(update: Update) -> None:
    if update.callback_query:
        try:
            await asyncio.wait_for(update.callback_query.answer(), timeout=1)
        except (TelegramError, TimeoutError) as error:
            LOGGER.warning("Подтверждение кнопки не отправлено: %s", type(error).__name__)


def input_text(update: Update, prefix: str) -> str:
    if update.callback_query:
        return update.callback_query.data.removeprefix(prefix + ":")
    return (update.effective_message.text or "").strip()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await acknowledge(update)
    LOGGER.info("Получена команда /start: update_id=%s", update.update_id)
    context.user_data.clear()
    await answer(
        update,
        "🎲 <b>Во что сыграть?</b>\n\n"
        "Подберу настольную игру под вашу компанию, свободное время и возраст игроков. "
        "А если расскажете о настроении — предложу выбор с объяснением от ИИ.\n\n"
        "Начнём?",
        reply_markup=keyboard([[("Подобрать игру →", "find")], [("О данных", "data")]]),
    )
    return ConversationHandler.END


async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await acknowledge(update)
    context.user_data.clear()
    context.user_data["session"] = secrets.token_hex(8)
    await answer(
        update,
        "<b>1/4. Сколько вас будет?</b>\nВыберите число или напишите, сколько вас будет.",
        reply_markup=player_keyboard(context.bot_data["games"]),
    )
    return PLAYERS


async def receive_players(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await acknowledge(update)
    value = input_text(update, "players")
    if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 20:
        await answer(update, "Введите целое число игроков от 1 до 20. Например, 4.")
        return PLAYERS
    games = context.bot_data["games"]
    if not any(supports_players(game, int(value)) for game in games):
        limit = ""
        if games and all(game.players_max is not None for game in games):
            maximum = max(game.players_max for game in games)
            limit = f" В ней есть игры максимум на {maximum} участников."
        await answer(
            update,
            f"В моей выборке из {len(games)} игр нет игры на {int(value)} участников."
            + limit
            + " Выберите другое число игроков или отмените подборку: /cancel.",
            reply_markup=player_keyboard(games),
        )
        return PLAYERS
    context.user_data["players"] = int(value)
    await answer(
        update,
        "<b>2/4. Сколько минут есть на одну партию?</b>\n"
        "Выберите время или напишите число от 5 до 600.",
        reply_markup=keyboard(
            [
                [(f"{n} мин", f"minutes:{n}") for n in (30, 45, 60)],
                [(f"{n} мин", f"minutes:{n}") for n in (90, 120, 180)],
            ]
        ),
    )
    return MINUTES


async def receive_minutes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await acknowledge(update)
    value = input_text(update, "minutes")
    if not value.isascii() or not value.isdecimal() or not 5 <= int(value) <= 600:
        await answer(update, "Введите целое число минут от 5 до 600. Например, 60.")
        return MINUTES
    context.user_data["minutes"] = int(value)
    await answer(
        update,
        "<b>3/4. Сколько лет младшему игроку?</b>\n"
        "Это поможет учесть возрастные ограничения. Можно ввести число от 1 до 100.",
        reply_markup=keyboard(
            [
                [(str(n), f"age:{n}") for n in (6, 8, 10)],
                [(str(n), f"age:{n}") for n in (12, 14, 18)],
            ]
        ),
    )
    return AGE


async def ask_wish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await answer(
        update,
        "<b>4/4. Есть пожелания?</b>\n"
        "Например: «Хотим строить города» или «Нравятся птицы и природа». "
        "ИИ выберет из подходящих игр и объяснит рекомендацию.\n\n"
        "До 500 символов. Или получите обычную подборку кнопкой ниже.",
        reply_markup=keyboard([[("Подобрать без ИИ", "skip")]]),
    )
    return WISH


async def receive_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await acknowledge(update)
    value = input_text(update, "age")
    if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 100:
        await answer(update, "Введите возраст младшего игрока от 1 до 100 лет. Например, 12.")
        return AGE
    context.user_data["age"] = int(value)
    preferences = Preferences(
        *(context.user_data[field] for field in ("players", "minutes", "age"))
    )
    if not select_games(context.bot_data["games"], preferences):
        await empty_parameters(update, context, preferences)
        return RESULTS
    return await ask_wish(update, context)


async def empty_parameters(
    update: Update, context: ContextTypes.DEFAULT_TYPE, preferences: Preferences
) -> None:
    context.user_data["candidates"] = []
    await answer(
        update,
        f"Условия: игроков — {preferences.players}, до {preferences.minutes} минут, "
        f"младшему {preferences.age} лет.\n\n"
        "По этим условиям игр в моей выборке нет. "
        "Измените параметры подборки: /find.",
        reply_markup=keyboard([[("Изменить параметры", "find")]]),
    )


async def show_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    candidates = context.user_data.get("candidates", [])
    shown = context.user_data.setdefault("shown_ids", set())
    page = [game for game in candidates if game.id not in shown][:3]
    for game in page:
        await answer(update, format_game(game))
        shown.add(game.id)
        LOGGER.info("Карточка отправлена: update_id=%s game_id=%s", update.update_id, game.id)
    count = len(shown)
    await answer(
        update,
        f"Показано {count} из {len(candidates)}.",
        reply_markup=result_keyboard(count < len(candidates)),
    )
    return RESULTS


async def complete_ai(
    application: Application,
    chat_id: int,
    user_data: dict,
    session: str,
    candidates: list[Game],
    preferences: Preferences,
    wish: str,
    *,
    update_id=None,
) -> None:
    if user_data.get("session") != session:
        return
    started = time.monotonic()
    LOGGER.info("Подборка ИИ запущена: update_id=%s", update_id)
    try:
        try:
            recommendations = await asyncio.to_thread(recommend, candidates, preferences, wish)
            by_id = {game.id: game for game in candidates}
            messages = [
                (recommendation.game_id, format_game(by_id[recommendation.game_id], recommendation))
                for recommendation in recommendations
            ]
            text = (
                "Это рекомендации ИИ. Цитаты проверены по описаниям; "
                "детали правил уточняйте в источнике."
            )
            if not recommendations:
                text = (
                    f"Условия: игроков — {preferences.players}, до {preferences.minutes} минут, "
                    f"младшему {preferences.age} лет.\n\n"
                    "В описаниях подходящих по параметрам игр не нашёл подтверждения "
                    "вашему пожеланию. Можно изменить параметры через /find, уточнить тему "
                    "или посмотреть обычные игры по выбранным условиям."
                )
        except AIError as error:
            messages = [
                (None, str(error)),
                *((game.id, format_game(game)) for game in candidates[:3]),
            ]
            text = "Можно посмотреть остальные игры или изменить параметры."
        for game_id, message in messages:
            if user_data.get("session") != session:
                return
            shown = user_data.setdefault("shown_ids", set())
            if game_id in shown:
                continue
            await send_text(application.bot, chat_id, message, update_id=update_id)
            if game_id is not None:
                shown.add(game_id)
                LOGGER.info("Карточка отправлена: update_id=%s game_id=%s", update_id, game_id)
        if user_data.get("session") == session:
            count = len(user_data.get("shown_ids", set()))
            await send_text(
                application.bot,
                chat_id,
                text,
                update_id=update_id,
                reply_markup=result_keyboard(count < len(candidates), has_results=count > 0),
            )
            LOGGER.info(
                "Подборка ИИ отправлена: update_id=%s секунд=%.2f",
                update_id,
                time.monotonic() - started,
            )
    finally:
        if user_data.get("session") == session:
            user_data["busy"] = False


async def receive_wish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await acknowledge(update)
    skip = bool(update.callback_query and update.callback_query.data == "skip")
    wish = "" if skip else (update.effective_message.text or "").strip()
    if not skip and (not wish or len(wish) > 500):
        await answer(update, "Напишите от 1 до 500 символов или нажмите «Подобрать без ИИ».")
        return WISH
    preferences = Preferences(
        *(context.user_data[field] for field in ("players", "minutes", "age"))
    )
    candidates = select_games(context.bot_data["games"], preferences, wish)
    LOGGER.info(
        "Параметры подборки: update_id=%s минут=%s кандидатов=%s",
        update.update_id,
        preferences.minutes,
        len(candidates),
    )
    context.user_data["candidates"] = candidates
    if context.user_data.get("pending_wish") != wish:
        context.user_data["shown_ids"] = set()
    context.user_data["pending_wish"] = wish
    if not candidates:
        await empty_parameters(update, context, preferences)
        context.user_data.pop("pending_wish", None)
        return RESULTS
    await answer(
        update,
        f"Нашёл <b>{len(candidates)}</b> игр по параметрам: "
        f"{preferences.players} игроков, до {preferences.minutes} минут, "
        f"младшему {preferences.age} лет.",
    )
    if not wish:
        result = await show_page(update, context)
        context.user_data.pop("pending_wish", None)
        return result
    await answer(
        update,
        "✨ Выбираю с ИИ. Обычно это занимает до пары минут. "
        "Вы можете начать новую подборку командой /find или отменить /cancel.",
    )
    context.user_data["busy"] = True
    context.application.create_task(
        complete_ai(
            context.application,
            update.effective_chat.id,
            context.user_data,
            context.user_data["session"],
            candidates,
            preferences,
            wish,
            update_id=update.update_id,
        ),
        update=update,
    )
    context.user_data.pop("pending_wish", None)
    return RESULTS


async def results_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await acknowledge(update)
    if context.user_data.get("busy"):
        await answer(update, "ИИ ещё готовит ответ. Для новой подборки используйте /find.")
        return RESULTS
    if update.callback_query and update.callback_query.data == "more":
        return await show_page(update, context)
    if context.user_data.get("candidates"):
        return await ask_wish(update, context)
    return await begin(update, context)


async def about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await acknowledge(update)
    games = context.bot_data["games"]
    dates = sorted({game.collected_at[:10] for game in games})
    await answer(
        update,
        f"<b>О данных</b>\n\n{len(games)} карточек с Тесеры. Сбор: {', '.join(dates)}.\n"
        "Название, состав, возраст, время, теги и описания извлечены из HTML через BeautifulSoup. "
        "Выборка ограничена сохранённым списком источников и не охватывает все игры. "
        "Цены и наличие не отслеживаются.\n\n"
        "Если верхняя граница времени или возраст не указаны, игра не участвует в подборке. "
        "Время указано для одной партии, без изучения правил.\n\n"
        "ИИ получает до восьми подходящих записей и ваши пожелания. "
        "При сбое остаётся обычная подборка. /find — начать.",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await answer(
        update,
        "/find — подобрать игру\n/data — источник и ограничения\n"
        "/cancel — отменить подборку\n/start — главное меню\n\n"
        "Выбирайте параметры кнопками или вводите числа. Бот работает в личном чате. "
        "История диалога хранится только в памяти до перезапуска.",
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await answer(update, "Подборка отменена. /find — начать заново.")
    return ConversationHandler.END


async def stale_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await acknowledge(update)
    await answer(
        update, "Эта кнопка относится к другому шагу. Продолжите текущий диалог или /find."
    )


async def unexpected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await answer(update, "Используйте /find для подбора игры или /help для списка команд.")


async def wrong_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await answer(update, "На этом шаге нужен текст или кнопка. Отмена — /cancel.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOGGER.error("Ошибка обработки обновления: %s", type(context.error).__name__)
    if isinstance(update, Update) and update.effective_message:
        try:
            await answer(update, "Не удалось обработать запрос. Начните заново: /find.")
        except TelegramError:
            LOGGER.warning("Не удалось отправить сообщение об ошибке.")


async def trace_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    kind = "кнопка" if update.callback_query else "сообщение"
    delay = (
        round(max(0, time.time() - update.message.date.timestamp()), 1) if update.message else None
    )
    LOGGER.info(
        "Получено обновление: update_id=%s тип=%s задержка_сек=%s", update.update_id, kind, delay
    )


def build_application(token: str, games: list[Game], request=None) -> Application:
    builder = Application.builder().token(token).concurrent_updates(False)
    if request is not None:
        builder = builder.request(request)
    else:
        builder = builder.request(telegram_request()).get_updates_request(
            telegram_request(polling=True)
        )
    application = builder.build()
    application.bot_data["games"] = games
    application.add_handler(TypeHandler(Update, trace_update), group=-1)
    text = filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND
    private = filters.ChatType.PRIVATE
    application.add_handler(
        ConversationHandler(
            entry_points=[
                CommandHandler("find", begin, filters=private),
                CallbackQueryHandler(begin, pattern="^find$"),
            ],
            states={
                PLAYERS: [
                    CallbackQueryHandler(receive_players, pattern=r"^players:\d+$"),
                    MessageHandler(text, receive_players),
                ],
                MINUTES: [
                    CallbackQueryHandler(receive_minutes, pattern=r"^minutes:\d+$"),
                    MessageHandler(text, receive_minutes),
                ],
                AGE: [
                    CallbackQueryHandler(receive_age, pattern=r"^age:\d+$"),
                    MessageHandler(text, receive_age),
                ],
                WISH: [
                    CallbackQueryHandler(receive_wish, pattern="^skip$"),
                    MessageHandler(text, receive_wish),
                ],
                RESULTS: [
                    CallbackQueryHandler(results_action, pattern="^(more|ai)$"),
                    MessageHandler(text, results_action),
                ],
            },
            fallbacks=[
                CommandHandler("cancel", cancel, filters=private),
                CommandHandler("start", start, filters=private),
                CommandHandler("data", about, filters=private),
                CommandHandler("help", help_command, filters=private),
                CallbackQueryHandler(stale_button),
                MessageHandler(private & ~filters.TEXT, wrong_media),
            ],
            allow_reentry=True,
        )
    )
    for command, callback in (
        ("start", start),
        ("help", help_command),
        ("data", about),
        ("cancel", cancel),
    ):
        application.add_handler(CommandHandler(command, callback, filters=private))
    application.add_handler(CallbackQueryHandler(about, pattern="^data$"))
    application.add_handler(CallbackQueryHandler(stale_button))
    application.add_handler(MessageHandler(private, unexpected))
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Задайте TELEGRAM_BOT_TOKEN в окружении. Инструкция — в README.md.")
    try:
        games = load_games(Path(os.environ.get("GAMES_DATA", str(DEFAULT_DATA))))
    except DataError as error:
        raise SystemExit(str(error)) from error
    try:
        application = build_application(token, games)
        application.run_polling(allowed_updates=["message", "callback_query"], bootstrap_retries=-1)
    except TelegramError as error:
        raise SystemExit(f"Не удалось подключиться к Telegram: {type(error).__name__}.") from error


if __name__ == "__main__":
    main()
