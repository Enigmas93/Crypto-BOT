"""Live-switchable BingX session (Fase 17): wraps a BingXFuturesRestClient +
BingXExecutionProvider bound to whichever mode ("demo" or "live") is
currently selected in `bingx_account_settings`, and knows how to rebuild
itself when that mode changes - so the long-running BingX Shadow/Momentum
scripts never need a process restart to follow a switch made from the
dashboard.

One `BingxSessionManager` per running script. Call `refresh()` once per
poll cycle (cheap - a single DB read when the mode hasn't changed); it only
tears down and rebuilds the REST client on an actual mode change or the
very first call. Callers are expected to re-check the returned session's
`mode` against whatever they built their trading-cycle state (engine,
per-symbol configs) from, and rebuild THAT when it no longer matches - this
manager only owns the exchange connection, not the caller's engine.
"""
from __future__ import annotations

from dataclasses import dataclass

from aegis.db.bingx_account_repository import BingxAccountRepository
from aegis.execution.bingx_provider import BingXExecutionProvider
from aegis.logging_utils import get_logger, log_event
from aegis.providers.bingx.rest_client import BingXFuturesRestClient


class BingxCredentialsNotConfigured(RuntimeError):
    """No BingX API key has ever been saved from the dashboard."""


@dataclass(slots=True)
class BingxSession:
    mode: str
    account_id_suffix: str
    rest: BingXFuturesRestClient
    execution: BingXExecutionProvider

    @property
    def account_id(self) -> str:
        return f"{self.account_id_suffix}_{self.mode}"


class BingxSessionManager:
    def __init__(self, account_repo: BingxAccountRepository, account_id_suffix: str, logger_name: str) -> None:
        self._account_repo = account_repo
        self._account_id_suffix = account_id_suffix
        self._log = get_logger(logger_name)
        self._session: BingxSession | None = None

    async def refresh(self) -> BingxSession:
        state = await self._account_repo.get_settings()
        if not state.credentials_configured:
            raise BingxCredentialsNotConfigured(
                "nenhuma chave da BingX configurada - salve uma pela aba Configurações do dashboard"
            )
        if self._session is not None and self._session.mode == state.mode:
            return self._session

        if self._session is not None:
            log_event(
                self._log, "bingx_mode_switch", level=30, account_suffix=self._account_id_suffix,
                previous_mode=self._session.mode, new_mode=state.mode,
            )
            await self._session.rest.aclose()

        credentials = await self._account_repo.get_decrypted_credentials()
        assert credentials is not None  # state.credentials_configured already checked this
        api_key, api_secret = credentials
        rest = BingXFuturesRestClient(testnet=(state.mode == "demo"), api_key=api_key, api_secret=api_secret)
        session = BingxSession(
            mode=state.mode, account_id_suffix=self._account_id_suffix,
            rest=rest, execution=BingXExecutionProvider(rest),
        )
        if state.mode == "live":
            log_event(
                self._log, "LIVE_MODE_ACTIVE", level=30, account_suffix=self._account_id_suffix,
                message="Operando com fundos REAIS na BingX - modo ativado pelo dashboard.",
            )
        self._session = session
        return session

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.rest.aclose()
            self._session = None
