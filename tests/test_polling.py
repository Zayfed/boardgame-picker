import asyncio
import json

import httpx
import pytest
from telegram import Bot
from telegram.error import Conflict, TimedOut
from telegram.request import HTTPXRequest

from boardgames.polling import PollingRequest, telegram_request


@pytest.mark.parametrize("failure", ["pool", "hung", "conflict"])
def test_polling_reconnects_and_receives_queued_update(monkeypatch, failure):
    async def scenario():
        async def broken(request):
            if failure == "pool":
                raise httpx.PoolTimeout("occupied", request=request)
            if failure == "hung":
                await asyncio.Event().wait()
            return httpx.Response(
                409,
                json={
                    "ok": False,
                    "error_code": 409,
                    "description": "Conflict: terminated by other getUpdates request",
                },
            )

        async def healthy(request):
            return httpx.Response(200, json={"ok": True, "result": [{"update_id": 10}]})

        async def identity(request):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "id": 42,
                        "is_bot": True,
                        "first_name": "Picker",
                        "username": "picker_bot",
                    },
                },
            )

        clients = [
            httpx.AsyncClient(transport=httpx.MockTransport(handler))
            for handler in (broken, healthy)
        ]
        original_client = clients[0]
        monkeypatch.setattr(PollingRequest, "_build_client", lambda self: clients.pop(0))
        if failure == "hung":
            monkeypatch.setattr(PollingRequest, "REQUEST_TIMEOUT", 0.05)
        polling = PollingRequest()
        regular = HTTPXRequest(httpx_kwargs={"transport": httpx.MockTransport(identity)})
        async with Bot("123456:offline-test", request=regular, get_updates_request=polling) as bot:
            expected = Conflict if failure == "conflict" else TimedOut
            with pytest.raises(expected):
                await asyncio.wait_for(bot.get_updates(offset=10, timeout=0), timeout=1)
            assert original_client.is_closed
            updates = await bot.get_updates(offset=10, timeout=0)
            assert [update.update_id for update in updates] == [10]

    asyncio.run(scenario())


@pytest.mark.parametrize("polling", [False, True])
def test_requests_survive_proxy_that_hangs_on_reused_connections(monkeypatch, polling):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")

    async def scenario():
        active = set()

        async def serve(reader, writer):
            active.add(asyncio.current_task())
            try:
                for index in range(2):
                    headers = await reader.readuntil(b"\r\n\r\n")
                    for header in headers.split(b"\r\n"):
                        if header.lower().startswith(b"content-length:"):
                            await reader.readexactly(int(header.split(b":", 1)[1]))
                    if index:
                        await reader.read()
                        return
                    body = json.dumps(
                        {
                            "ok": True,
                            "result": {
                                "id": 42,
                                "is_bot": True,
                                "first_name": "Picker",
                                "username": "picker_bot",
                            },
                        }
                    ).encode()
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        b"Connection: keep-alive\r\nContent-Length: "
                        + str(len(body)).encode()
                        + b"\r\n\r\n"
                        + body
                    )
                    await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                writer.close()
                active.discard(asyncio.current_task())

        async with await asyncio.start_server(serve, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with Bot(
                "123456:offline-test",
                base_url=f"http://127.0.0.1:{port}/bot",
                request=telegram_request(polling=polling),
            ) as bot:
                for _ in range(2):
                    identity = await asyncio.wait_for(bot.get_me(), timeout=1)
                    assert identity.id == 42
        await asyncio.gather(*active)

    asyncio.run(scenario())
