"""Web Push (VAPID) trade notifications to the installed PWA (Fase 20).

Only two events: a position opened (asset + direction) and a position
closed (asset + direction + profit/loss in USDT). Sending never blocks a
trading loop: `notify_trade_result` schedules the delivery as a background
task, and every failure is logged and swallowed - a phone that is offline
must never stall order management.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass

import asyncpg
from py_vapid import Vapid02
from pywebpush import WebPushException, webpush

from aegis.logging_utils import get_logger, log_event

_LOG = get_logger("notifications.webpush")

ACCOUNT_LABELS = {
    "paper": "Paper", "shadow": "Shadow Binance",
    "shadow_bingx_demo": "Shadow BingX Demo", "shadow_bingx_live": "Shadow BingX REAL",
    "momentum": "Momentum Binance",
    "momentum_bingx_demo": "Momentum BingX Demo", "momentum_bingx_live": "Momentum BingX REAL",
}
EXIT_LABELS = {
    "STOP": "stop", "TAKE_PROFIT": "alvo", "TRAILING_STOP": "trailing stop", "END_OF_DATA": "fim dos dados",
}


def _brl_number(value: float, digits: int = 2) -> str:
    """12345.6 -> '12.345,60' (Brazilian separators)."""
    return f"{value:,.{digits}f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _price(value: float) -> str:
    if value >= 1000:
        return _brl_number(value, 2)
    if value >= 1:
        return _brl_number(value, 4).rstrip("0").rstrip(",")
    if value <= 0:
        return "0"
    decimals = min(12, -math.floor(math.log10(value)) + 3)  # ~4 significant digits, never scientific
    return f"{value:.{decimals}f}".rstrip("0").rstrip(".").replace(".", ",")


def _asset(symbol: str) -> str:
    return symbol.removesuffix("USDT")


@dataclass(slots=True)
class PushMessage:
    title: str
    body: str
    tag: str
    url: str

    def to_json(self) -> str:
        return json.dumps({"title": self.title, "body": self.body, "tag": self.tag, "url": self.url},
                          ensure_ascii=False)


def entry_message(account_id: str, symbol: str, side: str, entry_price: float | None) -> PushMessage:
    arrow = "🟢 LONG" if side == "LONG" else "🔴 SHORT"
    price = f" a {_price(entry_price)}" if entry_price else ""
    return PushMessage(
        title=f"{arrow} · {_asset(symbol)}",
        body=f"Entrada aberta{price} · {ACCOUNT_LABELS.get(account_id, account_id)}",
        tag=f"open-{account_id}-{symbol}-{int(time.time())}", url="/#positions",
    )


def close_message(account_id: str, symbol: str, side: str, net_pnl: float, r_multiple: float | None,
                  exit_reason: str | None) -> PushMessage:
    won = net_pnl >= 0
    result = f"{'Lucro' if won else 'Perda'} {'+' if won else '−'}${_brl_number(abs(net_pnl))}"
    r_part = f" ({'+' if (r_multiple or 0) >= 0 else '−'}{_brl_number(abs(r_multiple), 2)}R)" if r_multiple is not None else ""
    reason = EXIT_LABELS.get(exit_reason or "", exit_reason or "")
    return PushMessage(
        title=f"{'✅' if won else '❌'} {result} · {_asset(symbol)}",
        body=f"{side} encerrado{f' por {reason}' if reason else ''}{r_part} · {ACCOUNT_LABELS.get(account_id, account_id)}",
        tag=f"close-{account_id}-{symbol}-{int(time.time())}", url="/#orders",
    )


def unprotected_position_message(account_id: str, symbol: str) -> PushMessage:
    return PushMessage(
        title=f"🚨 POSIÇÃO SEM PROTEÇÃO · {_asset(symbol)}",
        body=f"Stop/alvo não foram criados e o fechamento de emergência falhou. Confira na corretora agora · "
             f"{ACCOUNT_LABELS.get(account_id, account_id)}",
        tag=f"unprotected-{account_id}-{symbol}-{int(time.time())}", url="/#positions",
    )


def closed_outside_message(account_id: str, symbol: str) -> PushMessage:
    return PushMessage(
        title=f"⚠️ Posição fechada fora do robô · {_asset(symbol)}",
        body=f"A corretora mostra a posição zerada, mas nem o stop nem o alvo executaram (liquidação ou "
             f"fechamento manual?) · {ACCOUNT_LABELS.get(account_id, account_id)}",
        tag=f"reconcile-{account_id}-{symbol}-{int(time.time())}", url="/#positions",
    )


KILL_SWITCH_REASONS = {
    "MAX_DRAWDOWN_BREACHED": "drawdown máximo atingido",
    "LOSS_STREAK_HALT_THRESHOLD": "limite de perdas seguidas",
}


def kill_switch_message(account_id: str, reasons: list[str]) -> PushMessage:
    why = ", ".join(KILL_SWITCH_REASONS.get(r, r) for r in reasons) or "motivo não informado"
    return PushMessage(
        title=f"⛔ Robô parado · {ACCOUNT_LABELS.get(account_id, account_id)}",
        body=f"Kill switch acionado ({why}). Nenhuma nova entrada até você reativar em Risco & Logs.",
        tag=f"killswitch-{account_id}-{int(time.time())}", url="/#risk",
    )


def process_down_message(script: str, failures: int) -> PushMessage:
    return PushMessage(
        title=f"📴 Processo caindo · {PROCESS_LABELS.get(script, script)}",
        body=f"Caiu {failures} vezes seguidas e está sendo reiniciado automaticamente. Verifique o PC.",
        tag=f"process-{script}", url="/#risk",
    )


def process_recovered_message(script: str) -> PushMessage:
    return PushMessage(
        title=f"✅ Processo normalizado · {PROCESS_LABELS.get(script, script)}",
        body="Voltou a rodar de forma estável.", tag=f"process-{script}", url="/#risk",
    )


PROCESS_LABELS = {
    "run_collector.py": "coletor de candles", "run_derivatives_engine.py": "derivativos",
    "run_liquidation_engine.py": "liquidações", "run_orderbook_engine.py": "orderbook",
    "run_macro_engine.py": "macro", "run_news_engine.py": "notícias", "run_event_risk_engine.py": "eventos",
    "run_paper_trading.py": "Paper", "run_shadow_trading.py": "Shadow Binance",
    "run_bingx_shadow_trading.py": "Shadow BingX", "run_momentum_trading.py": "Momentum Binance",
    "run_bingx_momentum_trading.py": "Momentum BingX", "run_daily_reset.py": "reset diário",
    "run_dashboard.py": "painel/API", "run_ai_engine.py": "IA", "run_tunnel.py": "túnel do celular",
}


def message_for_result(account_id: str, symbol: str, result: dict) -> PushMessage | None:
    """Maps an engine's run_once() result to a notification, or None for
    every routine action (no signal, blocked by risk, already open, ...)."""
    action = result.get("action")
    if action == "ENTRY_OPENED":
        return entry_message(account_id, symbol, result.get("side", ""), result.get("entry_price"))
    if action == "POSITION_CLOSED" and result.get("trade") is not None:
        t = result["trade"]
        return close_message(account_id, symbol, t.side, t.net_pnl, t.r_multiple, t.exit_reason)
    if action == "BRACKET_FAILED" and result.get("flattened") is False:
        return unprotected_position_message(account_id, symbol)
    if action == "RECONCILIATION_FAILED":
        return closed_outside_message(account_id, symbol)
    return None


def vapid_from_raw(private_key_b64url: str) -> Vapid02:
    """RFC 8292 ("vapid t=..., k=...") - the scheme Apple's push service
    requires; Chrome/Firefox accept it too. from_raw takes the base64url text."""
    return Vapid02.from_raw(private_key_b64url.strip().encode())


class PushSubscriptionRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def upsert(self, endpoint: str, p256dh: str, auth: str, user_agent: str = "") -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO push_subscriptions (endpoint, p256dh, auth, user_agent) VALUES ($1,$2,$3,$4)
                ON CONFLICT (endpoint) DO UPDATE SET p256dh = EXCLUDED.p256dh, auth = EXCLUDED.auth,
                    user_agent = EXCLUDED.user_agent, failure_count = 0
                """,
                endpoint, p256dh, auth, user_agent[:300],
            )

    async def delete(self, endpoint: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM push_subscriptions WHERE endpoint = $1", endpoint)

    async def all(self) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT endpoint, p256dh, auth FROM push_subscriptions")
        return [dict(r) for r in rows]

    async def count(self) -> int:
        async with self._pool.acquire() as conn:
            return await conn.fetchval("SELECT count(*) FROM push_subscriptions")

    async def mark(self, endpoint: str, ok: bool) -> None:
        async with self._pool.acquire() as conn:
            if ok:
                await conn.execute("UPDATE push_subscriptions SET last_success_at = now(), failure_count = 0 "
                                   "WHERE endpoint = $1", endpoint)
            else:
                await conn.execute("UPDATE push_subscriptions SET failure_count = failure_count + 1 "
                                   "WHERE endpoint = $1", endpoint)


class WebPushNotifier:
    def __init__(self, repo: PushSubscriptionRepository, private_key: str, subject: str) -> None:
        self.repo = repo
        self._vapid = vapid_from_raw(private_key) if private_key else None
        self._subject = subject
        self._tasks: set[asyncio.Task] = set()

    @property
    def enabled(self) -> bool:
        return self._vapid is not None

    async def send(self, message: PushMessage) -> dict:
        """Delivers to every subscription; returns counts. Expired ones
        (404/410 from the push service) are removed."""
        if not self.enabled:
            return {"sent": 0, "failed": 0, "removed": 0}
        sent = failed = removed = 0
        for sub in await self.repo.all():
            info = {"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}}
            try:
                await asyncio.to_thread(
                    webpush, subscription_info=info, data=message.to_json(), vapid_private_key=self._vapid,
                    vapid_claims={"sub": self._subject}, ttl=3600, headers={"Urgency": "high"}, timeout=10,
                )
            except WebPushException as exc:
                status = getattr(exc.response, "status_code", None)
                if status in (404, 410):
                    await self.repo.delete(sub["endpoint"])
                    removed += 1
                else:
                    await self.repo.mark(sub["endpoint"], ok=False)
                    failed += 1
                    log_event(_LOG, "push_failed", level=30, status=status, error=str(exc)[:200])
            except Exception as exc:  # noqa: BLE001 - network errors must never escape into a trading loop
                failed += 1
                log_event(_LOG, "push_failed", level=30, error=str(exc)[:200])
            else:
                await self.repo.mark(sub["endpoint"], ok=True)
                sent += 1
        return {"sent": sent, "failed": failed, "removed": removed}

    def notify_trade_result(self, account_id: str, symbol: str, result: dict) -> None:
        """Fire-and-forget: call with an engine result BEFORE popping its keys."""
        message = message_for_result(account_id, symbol, result) if self.enabled else None
        if message is not None:
            self.notify(message)

    def notify(self, message: PushMessage) -> None:
        """Fire-and-forget delivery of any message (kill switch, supervisor...)."""
        if not self.enabled:
            return
        task = asyncio.create_task(self._send_logged(message))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send_logged(self, message: PushMessage) -> None:
        try:
            counts = await self.send(message)
            log_event(_LOG, "push_delivered", title=message.title, **counts)
        except Exception as exc:  # noqa: BLE001
            log_event(_LOG, "push_failed", level=30, error=str(exc)[:200])

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


def build_webpush_notifier(pool: asyncpg.Pool, settings) -> WebPushNotifier:
    return WebPushNotifier(PushSubscriptionRepository(pool), settings.vapid_private_key, settings.vapid_subject)
