import asyncio
import json
from dataclasses import dataclass

import pytest
from pywebpush import WebPushException

from aegis.notifications import webpush as wp


@dataclass
class _Trade:
    side: str
    net_pnl: float
    r_multiple: float
    exit_reason: str


def test_entry_message_shows_asset_and_direction():
    m = wp.entry_message("shadow_bingx_demo", "BTCUSDT", "LONG", 84482.5)
    assert m.title == "🟢 LONG · BTC"
    assert m.body == "Entrada aberta a 84.482,50 · Shadow BingX Demo"
    assert m.url == "/#positions"


def test_close_message_profit_shows_value_in_dollars():
    m = wp.close_message("momentum_bingx_live", "1000PEPEUSDT", "SHORT", 12.345, 1.85, "TAKE_PROFIT")
    assert m.title == "✅ Lucro +$12,35 · 1000PEPE"
    assert m.body == "SHORT encerrado por alvo (+1,85R) · Momentum BingX REAL"


def test_close_message_loss():
    m = wp.close_message("shadow", "ETHUSDT", "LONG", -5.1, -1.02, "STOP")
    assert m.title == "❌ Perda −$5,10 · ETH"
    assert "LONG encerrado por stop (−1,02R)" in m.body


def test_only_entries_and_closes_produce_a_message():
    assert wp.message_for_result("paper", "SOLUSDT", {"action": "NO_SIGNAL"}) is None
    assert wp.message_for_result("paper", "SOLUSDT", {"action": "ENTRY_BLOCKED"}) is None
    opened = wp.message_for_result("paper", "SOLUSDT", {"action": "ENTRY_OPENED", "side": "SHORT", "entry_price": 119.1})
    assert opened.title == "🔴 SHORT · SOL"
    closed = wp.message_for_result("paper", "SOLUSDT", {"action": "POSITION_CLOSED",
                                                        "trade": _Trade("SHORT", 3.0, 0.6, "STOP")})
    assert closed.title.startswith("✅ Lucro +$3,00")


def test_small_prices_keep_significant_digits():
    assert wp.entry_message("paper", "XUSDT", "LONG", 0.00001234).body.startswith("Entrada aberta a 0,00001234")


class _Repo:
    def __init__(self, subs):
        self.subs = subs
        self.deleted, self.marks = [], []

    async def all(self):
        return self.subs

    async def delete(self, endpoint):
        self.deleted.append(endpoint)

    async def mark(self, endpoint, ok):
        self.marks.append((endpoint, ok))


class _Resp:
    def __init__(self, status):
        self.status_code = status


_TEST_KEY = "l3YxW4PS6d_8k0mV0jH8M4jKX2P2PmJxOQ9eaxS8xTw"  # throwaway key, test only


@pytest.mark.asyncio
async def test_send_delivers_and_drops_expired_subscriptions(monkeypatch):
    calls = []

    def fake_webpush(subscription_info, data, **kwargs):
        calls.append((subscription_info["endpoint"], json.loads(data), kwargs["vapid_claims"]))
        if subscription_info["endpoint"].endswith("gone"):
            raise WebPushException("gone", response=_Resp(410))
        if subscription_info["endpoint"].endswith("flaky"):
            raise WebPushException("server error", response=_Resp(500))

    monkeypatch.setattr(wp, "webpush", fake_webpush)
    subs = [{"endpoint": f"https://push.example/{n}", "p256dh": "k", "auth": "a"} for n in ("ok", "gone", "flaky")]
    repo = _Repo(subs)
    notifier = wp.WebPushNotifier(repo, _TEST_KEY, "mailto:x@example.com")
    counts = await notifier.send(wp.entry_message("paper", "BTCUSDT", "LONG", 1.0))

    assert counts == {"sent": 1, "failed": 1, "removed": 1}
    assert repo.deleted == ["https://push.example/gone"]
    assert ("https://push.example/ok", True) in repo.marks and ("https://push.example/flaky", False) in repo.marks
    assert calls[0][1]["title"] == "🟢 LONG · BTC" and calls[0][2] == {"sub": "mailto:x@example.com"}


@pytest.mark.asyncio
async def test_notify_trade_result_is_fire_and_forget(monkeypatch):
    sent = []
    monkeypatch.setattr(wp, "webpush", lambda subscription_info, data, **kw: sent.append(json.loads(data)["title"]))
    notifier = wp.WebPushNotifier(_Repo([{"endpoint": "https://p/1", "p256dh": "k", "auth": "a"}]), _TEST_KEY, "mailto:x")
    notifier.notify_trade_result("paper", "BTCUSDT", {"action": "NO_SIGNAL"})
    notifier.notify_trade_result("paper", "BTCUSDT", {"action": "ENTRY_OPENED", "side": "LONG", "entry_price": 1.0})
    await notifier.drain()
    await asyncio.sleep(0)
    assert sent == ["🟢 LONG · BTC"]


def test_notifier_disabled_without_private_key():
    notifier = wp.WebPushNotifier(_Repo([]), "", "mailto:x")
    assert not notifier.enabled
    notifier.notify_trade_result("paper", "BTCUSDT", {"action": "ENTRY_OPENED", "side": "LONG"})  # no-op, no loop needed


def test_unprotected_position_alerts_only_when_flatten_also_failed():
    assert wp.message_for_result("shadow_bingx_live", "BTCUSDT", {"action": "BRACKET_FAILED", "flattened": True}) is None
    m = wp.message_for_result("shadow_bingx_live", "BTCUSDT", {"action": "BRACKET_FAILED", "flattened": False})
    assert m.title == "🚨 POSIÇÃO SEM PROTEÇÃO · BTC" and "Shadow BingX REAL" in m.body


def test_position_closed_outside_the_robot():
    m = wp.message_for_result("momentum_bingx_demo", "HBARUSDT", {"action": "RECONCILIATION_FAILED"})
    assert m.title == "⚠️ Posição fechada fora do robô · HBAR"


def test_kill_switch_message_translates_reasons():
    m = wp.kill_switch_message("momentum_bingx_demo", ["LOSS_STREAK_HALT_THRESHOLD"])
    assert m.title == "⛔ Robô parado · Momentum BingX Demo"
    assert "limite de perdas seguidas" in m.body and m.url == "/#risk"


def test_process_messages_use_friendly_names():
    assert wp.process_down_message("run_bingx_shadow_trading.py", 3).title == "📴 Processo caindo · Shadow BingX"
    assert wp.process_recovered_message("run_tunnel.py").title == "✅ Processo normalizado · túnel do celular"


class _FakePush:
    def __init__(self):
        self.messages = []

    def notify(self, message):
        self.messages.append(message)


@pytest.mark.asyncio
async def test_kill_switch_trigger_sends_push(pool):
    import uuid

    from aegis.config import Settings
    from aegis.db.kill_switch_repository import KillSwitchRepository

    push = _FakePush()
    repo = KillSwitchRepository(pool, push=push)
    account = f"test-push-{uuid.uuid4().hex[:8]}"
    settings = Settings(_env_file=None, max_drawdown=0.10, loss_streak_halt_threshold=8)
    await repo.check_and_maybe_trigger(account, 100.0, 100.0, 2, settings)  # healthy - no alert
    assert push.messages == []
    await repo.check_and_maybe_trigger(account, 100.0, 100.0, 8, settings)  # 8 losses in a row
    assert len(push.messages) == 1 and push.messages[0].title.startswith("⛔ Robô parado")
    await repo.check_and_maybe_trigger(account, 100.0, 100.0, 9, settings)  # already halted - no repeat
    assert len(push.messages) == 1
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM kill_switch_events WHERE account_id = $1", account)
        await conn.execute("DELETE FROM kill_switch_state WHERE account_id = $1", account)
