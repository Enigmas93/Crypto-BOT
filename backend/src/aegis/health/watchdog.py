"""System watchdog (Fase 22): answers "is the robot really analyzing every
closed candle right now?" - not just "are the processes alive?".

Every trading engine advances a per-(account, symbol, interval) cursor each
time it evaluates a closed candle, whatever the outcome (no signal,
filtered, blocked by risk, entry). So a cursor that hasn't reached the last
closed bar some minutes after it closed means that engine did not analyze
it - whether the cause is a dead process, an exchange outage, a stuck data
feed or a bug. That single check covers every failure mode that would make
the bot silently miss trades.

`evaluate_health` is pure (all inputs passed in) so each rule is unit
tested; `collect_inputs` does the DB reads.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

_INTERVAL_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240}
ENGINE_LABELS = {
    "paper": "Paper", "shadow": "Shadow Binance",
    "shadow_bingx_demo": "Shadow BingX Demo", "shadow_bingx_live": "Shadow BingX REAL",
    "momentum": "Momentum Binance",
    "momentum_bingx_demo": "Momentum BingX Demo", "momentum_bingx_live": "Momentum BingX REAL",
    "trend_paper": "Tendência BTC+ETH",
}


@dataclass(slots=True)
class HealthIssue:
    key: str             # stable id, used to dedupe alerts
    severity: str        # CRITICAL | WARNING
    title: str
    detail: str


@dataclass(slots=True)
class HealthInputs:
    now: datetime
    candle_latest_close: dict[str, datetime]                      # symbol -> latest closed 1m bar close_time (Binance DB)
    cursors: dict[tuple[str, str, str], datetime]                  # (account, symbol, interval) -> last evaluated close
    expected_fixed: list[tuple[str, str, str]]                     # (account, symbol, interval) that must advance
    open_positions: set[tuple[str, str]] = field(default_factory=set)  # (account, symbol) - cursor frozen while open
    momentum_accounts: list[str] = field(default_factory=list)
    kill_switched: list[str] = field(default_factory=list)
    binance_db_accounts: tuple[str, ...] = ("paper", "shadow")
    trend_updated_at: datetime | None = None        # None = trend engine never started
    trend_last_bar: datetime | None = None          # open time of the daily bar last rebalanced on


def last_closed_bar_close(now: datetime, interval: str) -> datetime:
    """close_time (…:59.999) of the most recent fully closed bar."""
    minutes = _INTERVAL_MINUTES[interval]
    epoch_min = int(now.timestamp() // 60)
    bar_open = datetime.fromtimestamp((epoch_min - epoch_min % minutes) * 60, tz=UTC)
    return bar_open - timedelta(milliseconds=1)


def evaluate_health(inp: HealthInputs, candle_stale_minutes: int = 5, engine_grace_minutes: int = 10) -> list[HealthIssue]:
    issues: list[HealthIssue] = []

    # 1. Binance market data feed (Paper/Shadow Binance read it from the DB).
    stale = sorted(sym for sym, t in inp.candle_latest_close.items()
                   if inp.now - t > timedelta(minutes=candle_stale_minutes))
    if stale:
        worst = max(inp.now - inp.candle_latest_close[s] for s in stale)
        issues.append(HealthIssue(
            "data:binance_candles", "CRITICAL", "Dados de mercado atrasados",
            f"Candles da Binance sem atualizar há {int(worst.total_seconds() // 60)} min ({', '.join(stale)}). "
            "Paper e Shadow Binance não conseguem analisar.",
        ))
    data_down = bool(stale)

    # 2. Fixed-universe engines: every symbol's last closed bar must be evaluated.
    lagging: dict[str, list[str]] = {}
    for account, symbol, interval in inp.expected_fixed:
        if (account, symbol) in inp.open_positions:
            continue  # managing an open position - cursor intentionally paused
        if data_down and account in inp.binance_db_accounts:
            continue  # root cause already reported as a data issue
        due = last_closed_bar_close(inp.now, interval)
        if inp.now - due < timedelta(minutes=engine_grace_minutes):
            due = last_closed_bar_close(due - timedelta(minutes=1), interval)  # still inside the grace window
        cursor = inp.cursors.get((account, symbol, interval))
        if cursor is None or cursor < due:
            lagging.setdefault(account, []).append(symbol)
    for account, symbols in sorted(lagging.items()):
        issues.append(HealthIssue(
            f"engine:{account}", "CRITICAL", f"{ENGINE_LABELS.get(account, account)} parou de analisar",
            f"Último candle fechado não foi avaliado em {', '.join(sorted(symbols))}. "
            "Possível queda do processo, da corretora ou dos dados.",
        ))

    # 3. Momentum engines: dynamic universe, so the freshest cursor must be recent.
    for account in inp.momentum_accounts:
        latest = max((t for (a, _, _), t in inp.cursors.items() if a == account), default=None)
        due = last_closed_bar_close(inp.now, "1h")
        if inp.now - due < timedelta(minutes=engine_grace_minutes):
            due = last_closed_bar_close(due - timedelta(minutes=1), "1h")
        if latest is None or latest < due:
            issues.append(HealthIssue(
                f"engine:{account}", "CRITICAL", f"{ENGINE_LABELS.get(account, account)} parou de analisar",
                "Nenhum candidato do scanner foi avaliado no último candle de 1h.",
            ))

    # 4. Trend engine (daily): alive every minute, and today's rebalance done.
    if inp.trend_updated_at is not None:
        label = ENGINE_LABELS["trend_paper"]
        if inp.now - inp.trend_updated_at > timedelta(minutes=engine_grace_minutes):
            issues.append(HealthIssue(
                "engine:trend_paper", "CRITICAL", f"{label} parou",
                f"Sem atualizar há {int((inp.now - inp.trend_updated_at).total_seconds() // 60)} min.",
            ))
        else:
            today = inp.now.replace(hour=0, minute=0, second=0, microsecond=0)
            if inp.now - today >= timedelta(minutes=30) and (inp.trend_last_bar is None
                                                             or inp.trend_last_bar < today - timedelta(days=1)):
                issues.append(HealthIssue(
                    "engine:trend_paper_rebalance", "CRITICAL", f"{label} não rebalanceou hoje",
                    "O candle diário de 00:00 UTC fechou há mais de 30 min e o sinal não foi recalculado.",
                ))

    # 5. Accounts halted by the kill switch (alerted when it fired; listed here for the panel).
    for account in inp.kill_switched:
        issues.append(HealthIssue(
            f"killswitch:{account}", "WARNING", f"{ENGINE_LABELS.get(account, account)} parado pelo kill switch",
            "Nenhuma nova entrada até o reset em Risco & Logs.",
        ))
    return issues


# -- DB side --------------------------------------------------------------------
async def collect_inputs(pool, settings, now: datetime | None = None) -> HealthInputs:
    now = now or datetime.now(UTC)
    async with pool.acquire() as conn:
        mode = await conn.fetchval("SELECT mode FROM bingx_account_settings LIMIT 1") or "demo"
        candle_rows = await conn.fetch(
            "SELECT symbol, max(close_time) AS t FROM candles WHERE interval = '1m' AND is_closed "
            "AND symbol = ANY($1::text[]) GROUP BY symbol", settings.symbols,
        )
        cursor_rows = []
        for table in ("paper_trading_cursor", "shadow_trading_cursor", "momentum_trading_cursor"):
            cursor_rows += await conn.fetch(
                f"SELECT account_id, symbol, interval, last_processed_close_time AS t FROM {table}")
        open_rows = []
        for table in ("paper_positions", "shadow_positions"):
            open_rows += await conn.fetch(f"SELECT account_id, symbol FROM {table}")
        killed = await conn.fetch("SELECT account_id FROM kill_switch_state WHERE is_triggered")
        trend = await conn.fetchrow(
            "SELECT updated_at, last_rebalance_bar FROM trend_account WHERE account_id = 'trend_paper'")

    shadow_bingx, momentum_bingx = f"shadow_bingx_{mode}", f"momentum_bingx_{mode}"
    expected = []
    for account in ("paper", "shadow", shadow_bingx):
        expected += [(account, s, "1h") for s in settings.core_symbols]
        expected += [(account, s, settings.speculative_interval) for s in settings.speculative_symbol_list]
    return HealthInputs(
        now=now,
        candle_latest_close={r["symbol"]: r["t"] for r in candle_rows}
        | {s: datetime(1970, 1, 1, tzinfo=UTC) for s in settings.symbols if s not in {r["symbol"] for r in candle_rows}},
        cursors={(r["account_id"], r["symbol"], r["interval"]): r["t"] for r in cursor_rows},
        expected_fixed=expected,
        open_positions={(r["account_id"], r["symbol"]) for r in open_rows},
        momentum_accounts=["momentum", momentum_bingx],
        kill_switched=[r["account_id"] for r in killed],
        trend_updated_at=trend["updated_at"] if trend else None,
        trend_last_bar=trend["last_rebalance_bar"] if trend else None,
    )


class AlertTracker:
    """Turns successive health snapshots into notifications: alert once an
    issue has been seen on `confirm_checks` consecutive checks (no alarm for
    a one-minute blip, e.g. while the supervisor restarts an engine), remind
    every `remind_after` while it persists, and announce recovery."""

    def __init__(self, confirm_checks: int = 2, remind_after: timedelta = timedelta(hours=3)) -> None:
        self.confirm_checks = confirm_checks
        self.remind_after = remind_after
        self._seen: dict[str, int] = {}
        self._alerted_at: dict[str, datetime] = {}
        self._titles: dict[str, str] = {}

    def update(self, issues: list[HealthIssue], now: datetime) -> tuple[list[HealthIssue], list[str]]:
        """Returns (issues to alert now, titles of issues that recovered)."""
        current = {i.key: i for i in issues if i.severity == "CRITICAL"}
        to_alert: list[HealthIssue] = []
        for key, issue in current.items():
            self._seen[key] = self._seen.get(key, 0) + 1
            self._titles[key] = issue.title
            last = self._alerted_at.get(key)
            if self._seen[key] >= self.confirm_checks and (last is None or now - last >= self.remind_after):
                self._alerted_at[key] = now
                to_alert.append(issue)
        recovered = []
        for key in list(self._seen):
            if key not in current:
                if key in self._alerted_at:
                    recovered.append(self._titles.get(key, key))
                    del self._alerted_at[key]
                del self._seen[key]
        return to_alert, recovered


def status_payload(issues: list[HealthIssue], now: datetime) -> dict:
    return {
        "checked_at": now.isoformat(),
        "status": "CRITICAL" if any(i.severity == "CRITICAL" for i in issues)
        else ("WARNING" if issues else "OK"),
        "issues": [asdict(i) for i in issues],
    }
