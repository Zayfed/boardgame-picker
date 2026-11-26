import asyncio
import json
import logging

import httpx
from telegram.error import NetworkError, TimedOut
from telegram.request import HTTPXRequest

LOGGER = logging.getLogger(__name__)


class PollingRequest(HTTPXRequest):
    REQUEST_TIMEOUT = 20

    async def reconnect(self) -> None:
        try:
            await asyncio.wait_for(self.shutdown(), timeout=5)
        except Exception as error:
            LOGGER.warning("Ошибка закрытия соединения опроса: %s", type(error).__name__)
        await self.initialize()
        LOGGER.warning("Соединение опроса Telegram пересоздано")

    async def do_request(self, *args, **kwargs) -> tuple[int, bytes]:
        try:
            status, body = await asyncio.wait_for(
                super().do_request(*args, **kwargs), timeout=self.REQUEST_TIMEOUT
            )
        except TimeoutError as error:
            await self.reconnect()
            raise TimedOut("Превышено общее время опроса Telegram") from error
        except NetworkError:
            await self.reconnect()
            raise
        if status == 409:
            await self.reconnect()
        elif status == 200:
            try:
                updates = json.loads(body).get("result")
            except (ValueError, AttributeError):
                updates = None
            if isinstance(updates, list):
                LOGGER.info("Опрос Telegram завершён: обновлений=%s", len(updates))
        return status, body


def telegram_request(*, polling: bool = False) -> HTTPXRequest:
    request_type = PollingRequest if polling else HTTPXRequest
    connections = 1 if polling else 16
    return request_type(
        connection_pool_size=connections,
        connect_timeout=3,
        read_timeout=3 if polling else 5,
        pool_timeout=1,
        httpx_kwargs={
            "limits": httpx.Limits(max_connections=connections, max_keepalive_connections=0)
        },
    )
