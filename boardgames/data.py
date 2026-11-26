import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "data" / "games.json"


class DataError(ValueError):
    pass


@dataclass(frozen=True)
class Game:
    id: str
    name: str
    players_min: int | None
    players_max: int | None
    age_min: int | None
    duration_min: int | None
    duration_max: int | None
    players_text: str
    duration_text: str
    tags: list[str]
    description: str
    source_url: str
    collected_at: str

    def to_dict(self) -> dict:
        return asdict(self)

    def validate(self) -> None:
        for field in ("id", "name", "source_url", "collected_at"):
            if not isinstance(getattr(self, field), str) or not getattr(self, field).strip():
                raise DataError(f"Не заполнено поле {field}.")
        parsed = urlparse(self.source_url)
        if parsed.scheme != "https" or parsed.netloc != "tesera.ru":
            raise DataError(f"Некорректный источник: {self.id}.")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.id):
            raise DataError("Некорректный идентификатор игры.")
        if parsed.path.rstrip("/") != f"/game/{self.id}":
            raise DataError(f"Ссылка не соответствует идентификатору: {self.id}.")
        try:
            datetime.fromisoformat(self.collected_at)
        except ValueError as error:
            raise DataError(f"Некорректная дата: {self.id}.") from error
        for field in ("players_min", "players_max", "age_min", "duration_min", "duration_max"):
            value = getattr(self, field)
            if value is not None and (type(value) is not int or value < 0):
                raise DataError(f"Некорректное числовое поле {field}: {self.id}.")
        for low, high in (
            (self.players_min, self.players_max),
            (self.duration_min, self.duration_max),
        ):
            if low is not None and high is not None and low > high:
                raise DataError(f"Перепутаны границы диапазона: {self.id}.")
        if not isinstance(self.tags, list) or not all(isinstance(tag, str) for tag in self.tags):
            raise DataError(f"Некорректные теги: {self.id}.")
        for field in ("players_text", "duration_text", "description"):
            if not isinstance(getattr(self, field), str):
                raise DataError(f"Некорректный текст {field}: {self.id}.")


def load_games(path: Path = DEFAULT_DATA) -> list[Game]:
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(rows, list) or not rows:
            raise DataError("Набор данных пуст или имеет неверный формат.")
        games = [Game(**row) for row in rows]
        seen = set()
        for game in games:
            game.validate()
            if game.id in seen:
                raise DataError(f"Повторный идентификатор: {game.id}.")
            seen.add(game.id)
        return games
    except (OSError, json.JSONDecodeError, TypeError) as error:
        raise DataError(
            "Не удалось прочитать данные. Выполните python -m boardgames.collect."
        ) from error
