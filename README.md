# Aegis Quant

Sistema de análise quantitativa e execução automatizada para Binance
Futures. Preservação de capital primeiro, retorno depois.

- **Blueprint de arquitetura completo:** [`aegis-quant-blueprint.html`](aegis-quant-blueprint.html)
  (abra no navegador) — princípios, módulos, fontes de dados, risco,
  portfólio, telas de interface e roadmap de 12 fases.
- **Backend:** [`backend/`](backend/README.md) — implementação em Python.
  Fases 1–14 concluídas (coleta de dados, banco, Technical/Derivatives/
  Liquidation/Order Book/Macro/News/Risk Engines, Kill Switch global,
  Strategy Engine, Backtest Engine, Paper Trading, Shadow Trading,
  Dashboard, bucket Speculative e Momentum Engine); ver o README do
  backend para setup, testes e como rodar.

## Status

| Fase | Descrição | Status |
|---|---|---|
| 1 | Binance Market Collector | ✅ concluída |
| 2 | Banco de dados (PostgreSQL + TimescaleDB) | ✅ concluída |
| 3 | Technical Engine | ✅ concluída |
| 4 | Derivatives Engine (funding, OI, long/short ratio, basis) | ✅ concluída |
| 4b | Liquidation Engine | ✅ concluída* |
| 4c | Order Book Engine | ✅ concluída |
| 5 | Macro Engine (FRED + BLS + BEA) | ✅ concluída |
| 6 | News Engine (core + 6b fontes adicionais + NEWS_CONFLICT) | ✅ concluída |
| 7 | Risk Engine | ✅ concluída |
| 7b | Kill Switch global | ✅ concluída |
| 8 | Strategy Engine + Backtest Engine (com persistência) | ✅ concluída |
| 9 | Paper Trading | ✅ concluída** |
| 10 | Shadow Trading (ordens reais, testnet) | ✅ concluída*** |
| 11 | Dashboard (API + UI local) | ✅ concluída**** |
| 13 | Bucket Speculative (Core + Speculative, Paper/Shadow) | ✅ concluída |
| 14 | Momentum Engine ("moonshot" scanner, pares líquidos) | ✅ concluída***** |
| 12 | Live Trading (mainnet), ML | planejadas |

\* Liquidation Engine funciona (conexão, parsing testado, persistência) mas
não foi validado ao vivo contra um evento real — ver `backend/README.md`.

\*\* Paper Trading funciona ponta a ponta (testado + validado contra dado
real), mas uma abertura de posição real ainda não foi observada ao vivo
(nenhum sinal bateu durante a janela curta de teste) — ver `backend/README.md`.

\*\*\* Shadow Trading coloca ordens REAIS na Binance Futures Testnet (dinheiro
fictício, zero risco financeiro real) — validado ponta a ponta com um
bracket completo (entrada + stop + take-profit) aberto, verificado e
desfeito com sucesso. **Mainnet (`LIVE_TRADING=true` com dinheiro real)
ainda não foi tocado, de propósito** — ver `backend/README.md`.

\*\*\*\* Dashboard cobre a fração já construída (Overview, Posições, Trades,
Backtests, Data Health) — não as telas completas do blueprint original
(Scanner, Portfólio multi-bucket, Calendário Macro, etc.). Local-only, sem
autenticação, de propósito — ver `backend/README.md`.

\*\*\*\*\* Momentum Engine escaneia só pares já líquidos (piso real de volume,
nunca listagens novas/finas — escolha explícita do usuário) e sai via
trailing stop nativo da Binance, não um take-profit fixo — validado ao vivo,
incluindo um trade completo (entrada + saída) sem intervenção manual. Durante
o monitoramento pós-lançamento, um bug crítico foi encontrado e corrigido: a
leverage real nunca era setada explicitamente antes da entrada, então ordens
reais saíam na leverage padrão da conta/símbolo na Binance, não na leverage
configurada internamente — ver "Métricas da Fase 15" em `backend/README.md`.

\*\*\*\*\* Momentum Engine varre pares de cripto líquidos reais (piso de
volume 24h configurável) atrás de força de movimento e sai via trailing
stop nativo da Binance — deliberadamente restrito a pares já líquidos,
nunca listagens novas/finas, por decisão explícita do usuário dado o risco
mais alto dessa categoria de trading. Validado ao vivo (scan real +
bracket real com trailing stop aberto/verificado/desfeito) — ver
`backend/README.md`.

Nunca rode em `LIVE_TRADING=true` sem antes passar por backtest, paper e
shadow trading — ver seção 9 do blueprint (Modos & Controle).
