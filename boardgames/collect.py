import argparse
import copy
import hashlib
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup, Comment
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from boardgames.data import ROOT, DataError, Game

USER_AGENT = "BoardgamePicker/0.1 (educational project; sequential requests)"


def clean_text(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def parse_interval(value: str) -> tuple[int | None, int | None]:
    normalized = clean_text(value).lower().replace("–", "-").replace("—", "-")
    numbers = [int(part) for part in re.findall(r"\d+", normalized)]
    if not numbers:
        return None, None
    multiplier = 60 if "час" in normalized else 1
    numbers = [number * multiplier for number in numbers]
    if len(numbers) == 2 and numbers[0] <= numbers[1]:
        return numbers[0], numbers[1]
    if len(numbers) != 1:
        raise DataError(f"Неизвестный формат диапазона: {value}")
    if normalized.startswith("от") or "+" in normalized:
        return numbers[0], None
    if normalized.startswith("до"):
        return None, numbers[0]
    return numbers[0], numbers[0]


def parse_game(html: str, url: str, collected_at: str) -> Game:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.select_one("#game_title")
    if not title:
        raise DataError("В HTML нет названия игры; возможно, источник изменился.")

    def parameter(label: str) -> str:
        icon = soup.select_one(f'ul.classnav img[title="{label}"]')
        return clean_text(icon.parent.get_text(" ", strip=True)) if icon else ""

    players_text = parameter("число игроков")
    duration_text = parameter("время партии")
    players_min, players_max = parse_interval(players_text)
    duration_min, duration_max = parse_interval(duration_text)
    age_match = re.search(r"\d+", parameter("возраст"))
    description = soup.select_one("#plsh1 .raw_text_output")
    game = Game(
        id=urlparse(url).path.rstrip("/").split("/")[-1],
        name=clean_text(title.get_text(" ", strip=True)),
        players_min=players_min,
        players_max=players_max,
        age_min=int(age_match.group()) if age_match else None,
        duration_min=duration_min,
        duration_max=duration_max,
        players_text=players_text,
        duration_text=duration_text,
        tags=list(
            dict.fromkeys(
                clean_text(tag.get_text(" ", strip=True))
                for tag in soup.select("ul.tags a")
                if tag.get_text(strip=True)
            )
        ),
        description=clean_text(description.get_text(" ", strip=True)) if description else "",
        source_url=url,
        collected_at=collected_at,
    )
    game.validate()
    return game


def snapshot(html: str, url: str, collected_at: str) -> str:
    original = BeautifulSoup(html, "html.parser")
    saved = BeautifulSoup(
        '<!doctype html><html><head><meta charset="utf-8"></head><body></body></html>',
        "html.parser",
    )
    for name, content in (("source-url", url), ("collected-at", collected_at)):
        saved.head.append(saved.new_tag("meta", attrs={"name": name, "content": content}))
    for selector in ("#game_title", "ul.tags", "ul.classnav", "#plsh1"):
        node = original.select_one(selector)
        if node:
            saved.body.append(copy.deepcopy(node))
    for node in saved.select("script, style, iframe, form"):
        node.decompose()
    for node in saved.find_all(string=lambda text: isinstance(text, Comment)):
        node.extract()
    return str(saved)


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def collect(data_dir: Path, offline: bool, delay: float, resume: bool = False) -> dict:
    manifest = json.loads((data_dir / "sources.json").read_text(encoding="utf-8"))
    urls = manifest["urls"]
    if not urls or len(urls) != len(set(urls)):
        raise DataError("Список источников пуст или содержит повторные ссылки.")
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    session.mount(
        "https://",
        HTTPAdapter(
            max_retries=Retry(total=2, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504])
        ),
    )
    robots = RobotFileParser("https://tesera.ru/robots.txt")
    if not offline:
        response = session.get(robots.url, timeout=30)
        response.raise_for_status()
        robots.parse(response.text.splitlines())
    games = []
    issues = []
    provenance = []
    seen = set()
    downloaded = 0
    reused = 0
    html_dir = data_dir / "html"
    html_dir.mkdir(parents=True, exist_ok=True)
    for index, url in enumerate(urls, start=1):
        if not re.fullmatch(r"https://tesera\.ru/game/[A-Za-z0-9_-]+/", url):
            raise DataError(f"Некорректный URL в списке источников: {url}")
        slug = urlparse(url).path.rstrip("/").split("/")[-1]
        saved_path = html_dir / f"{slug}.html"
        cached = offline or (resume and saved_path.is_file())
        fetched = False
        try:
            if cached:
                html = saved_path.read_text(encoding="utf-8")
                saved = BeautifulSoup(html, "html.parser")
                source = saved.select_one('meta[name="source-url"]')
                meta = saved.select_one('meta[name="collected-at"]')
                if not source or source.get("content") != url or not meta:
                    raise DataError("В сохранённом HTML отсутствует источник или дата сбора.")
                collected_at = meta["content"]
            else:
                if not robots.can_fetch(USER_AGENT, url):
                    raise DataError("Сбор запрещён robots.txt.")
                fetched = True
                response = session.get(url, timeout=30)
                response.raise_for_status()
                collected_at = datetime.now(UTC).isoformat()
                html = snapshot(response.text, url, collected_at)
            game = parse_game(html, url, collected_at)
            if game.id in seen:
                raise DataError("Повторный идентификатор после обработки.")
            seen.add(game.id)
            games.append(game)
            if not cached:
                saved_path.write_text(html, encoding="utf-8")
                downloaded += 1
            else:
                reused += 1
            for field in (
                "players_min",
                "players_max",
                "age_min",
                "duration_min",
                "duration_max",
                "tags",
                "description",
            ):
                if getattr(game, field) is None or getattr(game, field) in ("", []):
                    issues.append(
                        {
                            "url": url,
                            "field": field,
                            "action": "Сохранено без выдуманного значения; учитывается фильтрами.",
                        }
                    )
            provenance.append(
                {
                    "url": url,
                    "html_file": f"html/{saved_path.name}",
                    "sha256": hashlib.sha256(html.encode()).hexdigest(),
                    "collected_at": collected_at,
                }
            )
            print(f"[{index}/{len(urls)}] {game.name}", flush=True)
        except (requests.RequestException, OSError, ValueError, TypeError, KeyError) as error:
            issues.append(
                {
                    "url": url,
                    "error": str(error),
                    "action": "Обновление остановлено; предыдущая выгрузка не заменена.",
                }
            )
            write_json(data_dir / "last_failed_collection.json", {"issues": issues})
            raise DataError(f"Не удалось собрать {url}: {error}") from error
        finally:
            if fetched and index < len(urls):
                time.sleep(max(delay, 1.0))
    report = {
        "source": manifest["source"],
        "selection": manifest["selection"],
        "mode": "saved_html" if offline else "saved_and_live_html" if resume else "live_html",
        "requested": len(urls),
        "collected": len(games),
        "duplicates": 0,
        "rejected": 0,
        "downloaded": downloaded,
        "reused": reused,
        "checked": ["required fields", "types", "ranges", "duplicates", "missing values", "URLs"],
        "issues": issues,
        "provenance": provenance,
    }
    rows = [game.to_dict() for game in games]
    write_json(data_dir / "games.json", rows)
    write_json(data_dir / "quality_report.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Сбор карточек Тесеры через BeautifulSoup")
    parser.add_argument(
        "--offline", action="store_true", help="Повторить обработку сохранённого HTML"
    )
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument(
        "--resume", action="store_true", help="Использовать уже сохранённые карточки"
    )
    args = parser.parse_args()
    try:
        report = collect(args.data_dir, args.offline, args.delay, args.resume)
        print(f"Готово: {report['collected']} игр, замечаний: {len(report['issues'])}.")
    except (DataError, OSError, json.JSONDecodeError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
