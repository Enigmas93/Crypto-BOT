"""Initial schema - Phase 2: asset registry + the four market-data streams
collected in Phase 1 (candles, trades, funding_rates, book_ticker).

Every time-series table is a TimescaleDB hypertable, chunked by its time
column. Tables for macro/news/signals/orders/etc. are added by later phases,
each in its own migration - this one only covers what Phase 1 already
produces.

Revision ID: 0001
Revises:
Create Date: 2026-09-21
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")

    op.create_table(
        "assets",
        sa.Column("symbol", sa.Text, primary_key=True),
        sa.Column("status", sa.Text, nullable=False, server_default="ACTIVE"),
        sa.Column("tier", sa.Text, nullable=True),  # set later by AssetScanner (Phase 5+)
        sa.Column("first_seen_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )

    op.create_table(
        "candles",
        sa.Column("symbol", sa.Text, nullable=False),
        sa.Column("interval", sa.Text, nullable=False),
        sa.Column("open_time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("close_time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("open", sa.Float, nullable=False),
        sa.Column("high", sa.Float, nullable=False),
        sa.Column("low", sa.Float, nullable=False),
        sa.Column("close", sa.Float, nullable=False),
        sa.Column("volume", sa.Float, nullable=False),
        sa.Column("quote_volume", sa.Float, nullable=False),
        sa.Column("trades", sa.Integer, nullable=False),
        sa.Column("taker_buy_base_volume", sa.Float, nullable=False),
        sa.Column("taker_buy_quote_volume", sa.Float, nullable=False),
        sa.Column("is_closed", sa.Boolean, nullable=False),
        sa.Column("source", sa.Text, nullable=False, server_default="binance"),
        sa.Column("ingested_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("symbol", "interval", "open_time"),
    )
    op.execute("SELECT create_hypertable('candles', 'open_time')")
    op.create_index("ix_candles_symbol_interval_time", "candles", ["symbol", "interval", "open_time"])

    op.create_table(
        "trades",
        sa.Column("symbol", sa.Text, nullable=False),
        sa.Column("agg_trade_id", sa.BigInteger, nullable=False),
        sa.Column("price", sa.Float, nullable=False),
        sa.Column("quantity", sa.Float, nullable=False),
        sa.Column("trade_time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("is_buyer_maker", sa.Boolean, nullable=False),
        sa.Column("source", sa.Text, nullable=False, server_default="binance"),
        sa.Column("ingested_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        # trade_time (the partitioning column) must be part of any unique/PK
        # constraint on a hypertable - TimescaleDB requirement.
        sa.PrimaryKeyConstraint("symbol", "agg_trade_id", "trade_time"),
    )
    op.execute("SELECT create_hypertable('trades', 'trade_time')")
    op.create_index("ix_trades_symbol_time", "trades", ["symbol", "trade_time"])

    op.create_table(
        "funding_rates",
        sa.Column("symbol", sa.Text, nullable=False),
        sa.Column("event_time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("mark_price", sa.Float, nullable=False),
        sa.Column("index_price", sa.Float, nullable=False),
        sa.Column("estimated_settle_price", sa.Float, nullable=True),
        sa.Column("funding_rate", sa.Float, nullable=False),
        sa.Column("next_funding_time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("source", sa.Text, nullable=False, server_default="binance"),
        sa.Column("ingested_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("symbol", "event_time"),
    )
    op.execute("SELECT create_hypertable('funding_rates', 'event_time')")
    op.create_index("ix_funding_rates_symbol_time", "funding_rates", ["symbol", "event_time"])

    op.create_table(
        "book_ticker",
        sa.Column("symbol", sa.Text, nullable=False),
        sa.Column("event_time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("best_bid_price", sa.Float, nullable=False),
        sa.Column("best_bid_qty", sa.Float, nullable=False),
        sa.Column("best_ask_price", sa.Float, nullable=False),
        sa.Column("best_ask_qty", sa.Float, nullable=False),
        sa.Column("source", sa.Text, nullable=False, server_default="binance"),
        sa.Column("ingested_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("symbol", "event_time"),
    )
    op.execute("SELECT create_hypertable('book_ticker', 'event_time')")
    op.create_index("ix_book_ticker_symbol_time", "book_ticker", ["symbol", "event_time"])


def downgrade() -> None:
    op.drop_table("book_ticker")
    op.drop_table("funding_rates")
    op.drop_table("trades")
    op.drop_table("candles")
    op.drop_table("assets")
