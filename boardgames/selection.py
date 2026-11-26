import re
from dataclasses import dataclass
from functools import lru_cache

import snowballstemmer

from boardgames.data import Game


@dataclass(frozen=True)
class Preferences:
    players: int
    minutes: int
    age: int

    def validate(self) -> None:
        for value, low, high, label in (
            (self.players, 1, 20, "Число игроков: от 1 до 20."),
            (self.minutes, 5, 600, "Время партии: от 5 до 600 минут."),
            (self.age, 1, 100, "Возраст младшего игрока: от 1 до 100 лет."),
        ):
            if type(value) is not int or not low <= value <= high:
                raise ValueError(label)


@lru_cache(maxsize=512)
def word_stems(text: str) -> frozenset[str]:
    stemmer = snowballstemmer.stemmer("russian")
    words = re.findall(r"[а-яёa-z0-9]{3,}", text.casefold().replace("ё", "е"))
    return frozenset(stemmer.stemWords(words))


def topic_tokens(wish: str) -> frozenset[str]:
    stopwords = {
        "хочу",
        "хотим",
        "игра",
        "игры",
        "игру",
        "играть",
        "нам",
        "нас",
        "про",
        "для",
        "что",
        "чтобы",
        "можно",
        "нравится",
        "нравятся",
        "какую",
        "какая",
        "есть",
        "очень",
        "пожалуйста",
        "подбери",
        "настольную",
        "где",
    }
    return word_stems(
        " ".join(
            word
            for word in re.findall(r"[а-яёa-z0-9]{3,}", wish.casefold())
            if word not in stopwords
        )
    )


def topic_score(text: str, wish: str) -> int:
    return len(word_stems(text) & topic_tokens(wish))


def supports_players(game: Game, players: int) -> bool:
    return (
        (game.players_min is not None or game.players_max is not None)
        and (game.players_min is None or game.players_min <= players)
        and (game.players_max is None or players <= game.players_max)
    )


def select_games(games: list[Game], preferences: Preferences, wish: str = "") -> list[Game]:
    preferences.validate()
    if len(wish) > 500:
        raise ValueError("Опишите пожелания короче: до 500 символов.")
    matched = []
    for game in games:
        if not supports_players(game, preferences.players):
            continue
        if game.duration_max is None or game.duration_max > preferences.minutes:
            continue
        if game.age_min is None or game.age_min > preferences.age:
            continue
        matched.append(game)

    def order(game: Game) -> tuple:
        text = f"{game.name} {' '.join(game.tags)} {game.description}"
        relevance = topic_score(text, wish)
        return (-relevance, game.duration_max, game.name.casefold())

    return sorted(matched, key=order)
