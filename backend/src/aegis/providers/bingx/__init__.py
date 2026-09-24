"""BingX Perpetual Swap provider (Fase 16) - execution only.

Market data collection stays on Binance (public, free, already deeply
tested); BingX is used exclusively for real order execution, behind the
same `ExecutionProvider`-shaped interface `BinanceExecutionProvider`
implements - see `aegis.execution.bingx_provider`.
"""
