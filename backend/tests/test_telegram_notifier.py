import httpx
import pytest

from aegis.notifications.telegram import TelegramNotifier


def _notifier_with_transport(handler, bot_token: str = "test-token", chat_id: str = "12345") -> TelegramNotifier:
    notifier = TelegramNotifier(bot_token=bot_token, chat_id=chat_id)
    notifier._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return notifier


def test_enabled_requires_both_token_and_chat_id():
    assert TelegramNotifier(bot_token="x", chat_id="y").enabled is True
    assert TelegramNotifier(bot_token="", chat_id="y").enabled is False
    assert TelegramNotifier(bot_token="x", chat_id="").enabled is False
    assert TelegramNotifier(bot_token="", chat_id="").enabled is False


@pytest.mark.asyncio
async def test_send_posts_to_the_real_endpoint_shape_with_the_token_in_the_path():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.path == "/bottest-token/sendMessage"
        body = request.content.decode()
        assert '"chat_id":"12345"' in body
        assert "hello" in body
        return httpx.Response(200, json={"ok": True})

    notifier = _notifier_with_transport(handler)
    await notifier.send("hello")
    assert len(calls) == 1
    await notifier.aclose()


@pytest.mark.asyncio
async def test_send_is_a_no_op_when_not_configured():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"ok": True})

    notifier = _notifier_with_transport(handler, bot_token="", chat_id="")
    await notifier.send("should never be sent")
    assert calls["n"] == 0
    await notifier.aclose()


@pytest.mark.asyncio
async def test_send_never_raises_on_a_non_200_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "description": "chat not found"})

    notifier = _notifier_with_transport(handler)
    await notifier.send("hello")  # must not raise
    await notifier.aclose()


@pytest.mark.asyncio
async def test_send_never_raises_on_a_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network is unreachable")

    notifier = _notifier_with_transport(handler)
    await notifier.send("hello")  # must not raise
    await notifier.aclose()
