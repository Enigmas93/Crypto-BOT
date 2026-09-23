"""EventRiskEngine (Fase 15b) - polls CoinMarketCal for upcoming
crypto-specific events (listings, mainnet launches, hard forks) mentioning
any of the tracked symbols' underlying assets, and persists them.

Deliberately narrow: this is the crypto-native half of "Event Risk" only.
Macro events (FOMC, CPI, NFP) have no good free API - see this module's
sibling investigation documented in backend/README.md "Métricas da Fase
15b" - so they stay out of scope here, not silently assumed covered.

Does not feed any trading decision yet, same staged-rollout discipline as
EVENT_REACTION (aegis/strategy/strategies.py) - captures and persists the
data first; whether/how a strategy should react to an upcoming event is a
separate, deliberate decision for later, not bundled into this engine.
"""
from __future__ import annotations

from aegis.db.events_repository import EventsRepository
from aegis.events.models import CoinMarketCalEvent
from aegis.logging_utils import get_logger, log_event
from aegis.providers.coinmarketcal.client import CoinMarketCalProvider

_LOG = get_logger("events.service")


class EventRiskEngine:
    def __init__(self, provider: CoinMarketCalProvider, repo: EventsRepository) -> None:
        self.provider = provider
        self.repo = repo

    async def run_once(self, coin_slugs: list[str], limit: int = 50) -> list[CoinMarketCalEvent]:
        raw_events = await self.provider.get_events(coin_slugs, limit=limit)
        events = [CoinMarketCalEvent.from_api_payload(payload) for payload in raw_events]
        await self.repo.upsert_events(events)
        log_event(_LOG, "events_synced", count=len(events), coins=coin_slugs)
        return events
