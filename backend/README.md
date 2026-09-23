# Aegis Quant — Backend

**Fase 1 — Binance Market Collector:** coleta de dados de mercado públicos
(klines, agg trades, mark price/funding, book ticker) via WebSocket, com
bootstrap e reconciliação via REST. Nenhuma chave de API é necessária —
tudo aqui são endpoints públicos.

**Fase 2 — Banco de dados:** PostgreSQL + TimescaleDB, migrations via
Alembic, e um `PersistenceWriter` que liga o coletor ao banco com batching
assíncrono.

**Fase 3 — Technical Engine:** indicadores multi-timeframe (EMA, SMA, RSI,
ATR, ADX, VWAP, Bollinger, MACD, ROC, momentum, volume z-score, percentil
de volatilidade) e estrutura de mercado (HH/HL/LH/LL, breakout/breakdown)
calculados a partir dos candles persistidos, gravados no feature store
compartilhado `market_features`.

**Fase 4 — Derivatives Engine (core):** funding rate/basis, open interest,
long/short ratio (global + top trader por conta + top trader por posição),
z-scores e padrões PRICE×OI, também gravados no `market_features`.

**Fase 4b — Liquidation Engine:** stream de liquidações de todo o mercado
(`!forceOrder@arstream`), estatísticas por janela (notional por lado,
imbalance, z-score, aceleração) e um `squeeze_score` heurístico.

**Fase 4c — Order Book Engine:** profundidade parcial via WS
(`<symbol>@depth20@100ms`), spread, microprice, imbalance, depth
imbalance e book pressure — só o snapshot derivado é persistido, nunca o
livro bruto (mesma lição do `book_ticker` na Fase 2, aplicada de propósito).

Fase 4 (core + 4b + 4c) está completa.

**Fase 5 — Macro Engine (FRED):** integração com FRED (Federal Reserve
Economic Data) — registro de séries via `macro_series.yaml`, observações
históricas, e um snapshot por série (variação, YoY, z-score vs. histórico
recente).

**Fase 5b — Macro Engine (BLS):** segunda fonte independente (Bureau of
Labor Statistics) para desemprego/CPI, mais o Nonfarm Payroll (NFP), que o
FRED não publica como série própria. Mesmo `MacroEngine`, agora roteando
por `source` — um mesmo pipeline, múltiplos providers.

**Fase 5c — Macro Engine (BEA):** terceira fonte (Bureau of Economic
Analysis) — PIB real trimestral. Diferente de FRED/BLS, o BEA não tem um
"series ID" plano; uma série só existe como (dataset, tabela, código
dentro da tabela) — `MacroSeriesConfig` ganhou campos opcionais
(`dataset`/`table`/`bea_frequency`) só pra isso.

**Fase 5 está completa (core + 5b + 5c).** O calendário de eventos macro
(`EVENT_RISK`) ficou fora do escopo — ver Próximos Passos.

**Fase 6 — News Engine (core):** feeds RSS/Atom oficiais (Fed, SEC, CFTC —
todos verificados ao vivo antes de entrar no `news_sources.yaml`),
classificação por regras (menção de ativo, sentimento, magnitude,
`SourceQualityScore` exatamente conforme os 5 níveis do spec).

**Fase 6b — News Engine (fontes adicionais + NEWS_CONFLICT):** mais 6
fontes reais (MarketWatch, CNBC — `TIER_1_FINANCIAL_MEDIA`; CoinDesk,
Cointelegraph, Decrypt, The Block — `SPECIALIZED_CRYPTO_MEDIA`), todas
verificadas ao vivo do mesmo jeito que as 3 da Fase 6. Mais a lógica de
conflito entre fontes do spec §41: `WAIT_FOR_CONFIRMATION` com uma fonte
só, `NEWS_CONFLICT` quando duas ou mais discordam sem uma fonte primária
pra desempatar, `CONFIRMED` quando concordam ou uma fonte oficial resolve
o empate — um veredito por ativo, recalculado a cada ciclo.

**Fase 6 está completa (core + 6b).** Classificação por IA (`event_type`,
`time_sensitivity`) ficou fora, por decisão explícita — ver Próximos Passos.

**Fase 7 — Risk Engine:** a primeira fase com autoridade de bloqueio de
verdade. Position sizing (spec §19), drawdown guard
(NORMAL/CAUTION/REDUCED_RISK/HALTED), loss-streak guard
(NONE/COOLDOWN/REDUCE_RISK/HALT), limite de perda diária, checagem de
distância de liquidação, cap de alavancagem, risco/retorno líquido de
custos, e trilha de auditoria (`risk_events`). Diferente dos outros
engines, não é um poller contínuo — é um portão síncrono que um futuro
Strategy/Execution Engine vai chamar antes de cada ordem. Validado contra
regras reais de mercado da Binance (tick/step/minNotional) e uma simulação
completa da máquina de estados com persistência real no banco.

**Fase 7b — Kill Switch global:** um disjuntor persistido e "pegajoso"
(spec §23) — diferente das guardas de drawdown/loss-streak da Fase 7, que
recalculam a cada `evaluate()` e podem voltar a NORMAL sozinhas assim que
a equity recupera, o Kill Switch, uma vez acionado (drawdown máximo ou
sequência de perdas realmente severa), fica travado até um reset manual
explícito e auditado (`kill_switch_events`). Reaproveita a mesma função
pura de gatilho tanto ao vivo (persistida via `KillSwitchRepository`)
quanto dentro do `BacktestEngine` (simulado só em memória, nunca tocando o
banco real).

**Fase 8 — Strategy Engine + Backtest Engine:** a primeira fase com sinais
de trade de verdade. Três estratégias puras sobre o `TechnicalSnapshot` da
Fase 3 (`TREND_PULLBACK`, `BREAKOUT`, `MEAN_REVERSION`), combinadas por
votação ponderada (`confluence.py`) em uma decisão LONG/SHORT/NO_TRADE. O
`BacktestEngine` reproduz o histórico de candles já coletado barra a barra,
reaproveitando o `RiskEngine` real da Fase 7 pra sizing e gate — a única
diferença entre backtest e live deve ser o `ExecutionProvider` (spec §120).
Validado contra dados reais da Binance (BTCUSDT/ETHUSDT, 1h) e contra um
teste de não-lookahead que prova que um choque de preço no fim da série
nunca muda uma decisão tomada antes dele. Cada rodada é persistida
(`backtest_runs`/`backtest_trades`, spec §86), incluindo se o Kill Switch
simulado chegou a disparar.

**Fase 9 — Paper Trading:** o primeiro engine que age sobre o "agora" em
vez de repetir histórico. `PaperTradingEngine.run_once()` roda como um
poller (mesmo formato do `MacroEngine`/`NewsEngine`): a cada ciclo, checa
uma posição aberta contra o candle mais recente ou avalia o Strategy
Engine pra uma entrada nova — passando primeiro pelo Kill Switch (Fase 7b,
bloqueia antes mesmo de calcular qualquer sinal) e depois pelo `RiskEngine`
real (Fase 7). Reaproveita literalmente a mesma matemática de fill/saída
do `BacktestEngine`, agora extraída pra um módulo compartilhado
(`aegis.execution.fills`) — spec §120 na prática, não só como princípio.
Nenhuma ordem real é enviada e nenhum dinheiro real está em risco; o
estado da conta (`risk_account_state`) e as posições/trades simulados
(`paper_positions`/`paper_trades`) são reais e persistidos, prontos pra um
futuro Live Trading só trocar o `ExecutionProvider`.

**Fase 10 — Shadow Trading (execução real, testnet):** o mesmo pipeline de
decisão da Fase 9 (Kill Switch → Strategy Engine → RiskEngine), mas agora
`ShadowTradingEngine` envia ordens REAIS pra Binance Futures (testnet por
padrão — `BINANCE_TESTNET=true`/`LIVE_TRADING=false`, com um gate de
segurança que se recusa a rodar fora disso). `BinanceExecutionProvider`
abre um bracket completo (entrada a mercado + stop-loss + take-profit) e,
se qualquer uma das duas pernas protetoras falhar ao ser colocada, fecha a
posição imediatamente (nunca deixa uma posição aberta sem proteção — spec
§110 aplicado no ponto de execução real, não só como precondição do
RiskEngine). Validado ao vivo contra a testnet: um bracket real foi
aberto, verificado contra o que a própria exchange reportou, e desfeito
com sucesso — inclusive um problema real descoberto nessa validação (a
Binance migrou ordens condicionais pra um endpoint novo, `/fapi/v1/algoOrder`,
em 09/12/2025; e o relógio local estava ~4,7s atrasado do servidor,
estourando o `recvWindow` padrão) e corrigido antes de qualquer execução
subsequente.

**Fase 11 — Dashboard:** a primeira interface visual do sistema — uma API
FastAPI local (sem autenticação, de propósito: ferramenta de um operador
só, na própria máquina, mesmo limite de confiança que todo script já
assume) mais um frontend HTML/JS estático servido junto, sem build step.
Desvio deliberado do blueprint original (que especificava Next.js +
TypeScript + Tailwind): introduzir um toolchain Node/npm inteiro pra um
painel de monitoramento local não se pagou nesta fase — documentado como
decisão consciente, não um atalho escondido. Mostra equity/drawdown/PnL
de Paper e Shadow lado a lado, posições abertas, trades recentes,
backtests recentes e saúde dos dados (candle mais recente por símbolo/
intervalo, com um limiar de "atrasado" real). Único endpoint de escrita:
reset do Kill Switch (exige nota, auditado exatamente como o caminho já
existente via `KillSwitchRepository`).

**Fase 13 — Bucket Speculative:** Paper e Shadow Trading passaram a
avaliar dois grupos de símbolos com o mesmo pipeline — Core (BTCUSDT/
ETHUSDT, 1h) e Speculative (SOLUSDT/XRPUSDT/DOGEUSDT/1000PEPEUSDT/
NEARUSDT, 15m — intervalo mais curto pra reagir mais rápido a
"oportunidades de retorno rápido"). Símbolos escolhidos verificando volume
real de 24h na Binance no momento, não uma lista arbitrária. Durante a
implantação ao vivo, achou e corrigiu um bug real: o preço de stop/take-
profit calculado via ATR nunca era arredondado pro `tick_size` real do
símbolo antes de mandar a ordem — só não tinha aparecido antes porque o
teste manual anterior (BTCUSDT) tinha arredondado à mão sem eu perceber
que o código de produção não fazia isso. Corrigido nos três motores
(Shadow, Paper, Backtest) com teste de regressão.

**Fase 14 — Momentum Engine ("moonshot" scanner):** depois de uma conversa
explícita sobre risco (perseguir criptos que "surgem com muita força"
é uma categoria de risco bem mais alta que tudo construído até aqui —
manipulação, gap, delisting), o usuário escolheu explicitamente escopar
isso só a pares já líquidos, nunca listagens novas/finas. `aegis.scanner`
varre as estatísticas reais de 24h da Binance e rankeia por força de
movimento entre símbolos acima de um piso real de volume. `MomentumTradingEngine`
reaproveita o mesmo pipeline de decisão (Kill Switch → Strategy Engine →
RiskEngine → ordens reais), mas com duas diferenças: candles vêm direto
via REST a cada ciclo (o universo é dinâmico, não uma lista fixa do
coletor) e a saída não é um alvo fixo — é um stop de segurança mais um
`TRAILING_STOP_MARKET` nativo da Binance, que acompanha o preço a favor e
nunca contra, deixando um movimento genuíno correr em vez de travar o
lucro num R fixo. Validado ao vivo: scanner rankeando símbolos reais,
bracket com trailing stop aberto/verificado/desfeito com sucesso.

Ver o blueprint de arquitetura completo para o desenho do sistema inteiro.

## Setup

```bash
# 1. Banco de dados (na raiz do repo)
cp .env.example .env          # credenciais locais de dev do Postgres
docker compose up -d          # sobe o TimescaleDB
docker compose ps             # espere "healthy"

# 2. Backend
cd backend
python -m venv .venv
source .venv/Scripts/activate       # Windows Git Bash
# PowerShell: .venv\Scripts\Activate.ps1  |  Linux/Mac: source .venv/bin/activate

pip install -r requirements-dev.txt
pip install -e .

cp .env.example .env          # DATABASE_URL já aponta pro compose acima
alembic upgrade head          # cria todas as tabelas, até macro_series/macro_observations/macro_snapshots

# Opcional, só necessário pra rodar o Macro Engine com dados reais:
# FRED: registre-se de graça em https://fred.stlouisfed.org/docs/api/api_key.html
# BEA:  registre-se de graça em https://apps.bea.gov/API/signup/index.cfm
# Defina FRED_API_KEY / BEA_API_KEY no backend/.env (não no .env da raiz —
# esse é só do docker-compose). BLS não precisa de chave nenhuma pra volume baixo.
```

## Rodar tudo de uma vez (supervisor, recomendado)

```bash
python scripts/supervisor.py
```

Lança os 10 processos de longa duração (Coletor, Derivatives, Liquidation,
Order Book, Macro, News, Paper, Shadow, Momentum, Dashboard) de uma vez e
reinicia sozinho qualquer um que caia por qualquer motivo, com backoff
exponencial (ver "Métricas da Fase 15b", item 8, pra detalhes e validação
ao vivo). Essa é a forma recomendada de deixar o sistema rodando sem
supervisão humana constante - as seções abaixo (`Rodar o Coletor`, `Rodar
o Shadow Trading`, etc.) continuam valendo pra rodar UM processo isolado
(debug, desenvolvimento de uma feature nova), mas em operação normal use
o supervisor, não os scripts um a um. Ctrl+C encerra o supervisor e todo
processo filho.

## Rodar os testes

```bash
python -m pytest -q
```

285 testes. 248 são offline (backoff, models, REST/WS/RSS clients,
PersistenceWriter, indicadores técnicos, estrutura de mercado,
TechnicalAnalysisService, analytics/service dos engines de Derivatives,
Liquidation, Order Book, Macro, News e Risk (regras, sizing, máquina de
estados) — todos com dados sintéticos hand-verificados, FRED/BLS/BEA/RSS
inclusos via `httpx.MockTransport`). 37 são de **integração real** contra
o Postgres — pulados automaticamente (não falham) se o banco não estiver
de pé.

## Rodar o coletor (dados reais)

```bash
python scripts/run_collector.py
```

Conecta no **testnet** da Binance Futures (`BINANCE_TESTNET=true` por
padrão). Se `DATABASE_URL` estiver configurado (é, por padrão), cada evento
é persistido via `PersistenceWriter`; sem banco, cai de volta pro
comportamento da Fase 1 (só loga). A cada 10s imprime `writer_stats`
(eventos escritos/descartados). `Ctrl+C` para parar — o writer drena o
buffer antes de sair.

## Rodar o Technical Engine (dados reais)

```bash
python scripts/run_technical_snapshot.py
```

Calcula um snapshot multi-timeframe para cada símbolo configurado a partir
dos candles já no banco (rode o coletor primeiro — precisa de histórico
real), imprime cada um como log JSON e grava no `market_features`. Só faz
sentido depois que houver candles suficientes persistidos.

## Rodar o Derivatives Engine (dados reais)

```bash
python scripts/run_derivatives_engine.py
```

Roda para sempre: a cada `DERIVATIVES_POLL_INTERVAL_SECONDS` (60s por
padrão), busca open interest e as 3 variantes de long/short ratio via REST
para cada (símbolo, período), persiste a série bruta, calcula o snapshot
derivado e faz merge no `market_features`. `Ctrl+C` para parar. Funding
rate/basis não precisam de polling — já vêm do stream `markPrice` da Fase 1
(tabela `funding_rates`), só são lidos de volta aqui.

## Rodar o Liquidation Engine (dados reais)

```bash
python scripts/run_liquidation_engine.py
```

Roda para sempre: conecta no stream de liquidações de todo o mercado,
filtra para os símbolos configurados, persiste os eventos brutos, e a cada
`LIQUIDATION_SNAPSHOT_INTERVAL_SECONDS` (60s por padrão) calcula as
estatísticas por janela e grava no `market_features`. `Ctrl+C` para parar.

## Rodar o Order Book Engine (dados reais)

```bash
python scripts/run_orderbook_engine.py
```

Roda para sempre: assina a profundidade parcial de cada símbolo, mantém só
o livro mais recente em memória, e a cada
`ORDERBOOK_SNAPSHOT_INTERVAL_SECONDS` (15s por padrão) calcula e grava o
snapshot derivado. Nenhum tick bruto de profundidade é persistido.

## Rodar o Macro Engine (dados reais)

```bash
python scripts/run_macro_engine.py
```

Roda para sempre: a cada `MACRO_POLL_INTERVAL_SECONDS` (6h por padrão —
dado macro muda devagar), busca cada série de `macro_series.yaml` na fonte
certa (FRED, BLS ou BEA, por `source`), grava as observações (upsert —
dado macro é revisado depois da divulgação inicial) e o snapshot calculado
(`macro_snapshots`). Sem `FRED_API_KEY`/`BEA_API_KEY`, as séries dessa
fonte são só puladas (logado) — BLS funciona sem chave nenhuma, então
nunca trava o resto.

## Rodar o News Engine (dados reais)

```bash
python scripts/run_news_engine.py
```

Sem chave nenhuma — todas as fontes seed são feeds RSS públicos. Roda para
sempre: a cada `NEWS_POLL_INTERVAL_SECONDS` (15min por padrão), busca cada
feed de `news_sources.yaml`, classifica cada item (ativos mencionados,
sentimento, magnitude, score de qualidade da fonte), persiste — reclassifica
em cima se o mesmo item aparecer de novo — e recalcula o veredito de
conflito (`NEWS_CONFLICT`/`WAIT_FOR_CONFIRMATION`/`CONFIRMED`) pra cada
ativo rastreado, sobre uma janela de `NEWS_CONFLICT_WINDOW_HOURS` (24h
por padrão).

## Rodar o Event Risk Engine (CoinMarketCal, dados reais)

```bash
python scripts/run_event_risk_engine.py
```

Exige `COINMARKETCAL_API_KEY` (cadastro gratuito em coinmarketcal.com/
developer — sem ela, o script recusa rodar e loga o motivo, mesma política
de FRED/BEA). Roda para sempre: a cada `EVENT_RISK_POLL_INTERVAL_SECONDS`
(1h por padrão — o plano gratuito dá 3.000 requisições/mês, sobra folga),
busca eventos cripto-específicos (listagens, mainnet, forks) pra cada
símbolo de `EVENT_RISK_COIN_SLUGS` numa única chamada, e persiste
(atualiza em cima se o evento já existia — datas estimadas se confirmam
com o tempo). Só a metade cripto-nativa do "Event Risk" — eventos macro
(FOMC/CPI/NFP) não têm fonte gratuita boa, ver "Métricas da Fase 15b",
item 18.

## Rodar a demonstração do Risk Engine (dados reais)

```bash
python scripts/demo_risk_engine.py
```

Diferente dos outros `run_*_engine.py`, não roda pra sempre — RiskEngine é
um portão síncrono, não um poller. O script busca as regras reais do
BTCUSDT (tick/step/minNotional) e o preço atual na Binance, roda 4 cenários
de proposta de trade (saudável, sem stop, alavancagem excessiva,
liquidação perto demais) e depois simula 10 "dias" de uma perda cada,
resetando o estado diário entre eles, pra mostrar as duas máquinas de
estado (drawdown e loss-streak) escalando juntas contra um estado de conta
real no Postgres. Cada avaliação é logada e gravada em `risk_events`.

## Rodar o Momentum Engine (ordens reais, testnet)

```bash
python scripts/verify_momentum_trading.py    # validação manual: scan + um bracket real
python scripts/run_momentum_trading.py       # poller contínuo
```

Mesmo gate de segurança do Shadow Trading (`BINANCE_TESTNET=true`/
`LIVE_TRADING=false`, recusa rodar fora disso). Não depende do coletor —
busca candles direto via REST a cada ciclo, porque o universo de símbolos
é escolhido dinamicamente pelo scanner, não uma lista fixa. A cada
`MOMENTUM_SCAN_INTERVAL_SECONDS` (15min por padrão) rerankeia os
candidatos; a cada `MOMENTUM_POLL_INTERVAL_SECONDS` (30s) reconcilia
posições abertas (mesmo se o símbolo saiu do ranking atual) e avalia
entrada nos candidatos correntes.

## Rodar o Dashboard

```bash
python scripts/run_dashboard.py
```

Abre em `http://localhost:8000` (só localhost — nunca exposto na rede por
padrão). A documentação automática da API (Swagger) fica em
`http://localhost:8000/docs`. A página atualiza sozinha a cada 10s via
`fetch()`; nenhum passo de build é necessário (HTML/CSS/JS puro, servido
como arquivo estático pelo próprio FastAPI).

## Rodar o Shadow Trading (ordens reais, testnet)

```bash
python scripts/verify_execution_setup.py     # 1. confirma que as credenciais funcionam (só leitura)
python scripts/verify_shadow_trading.py      # 2. abre/verifica/desfaz UM bracket pequeno de verdade
python scripts/run_collector.py              # 3. em um terminal - mantém os candles atualizados
python scripts/run_shadow_trading.py         # 4. em outro - o poller contínuo
```

Rode os quatro NESSA ordem antes de deixar o poller sem supervisão. Os
dois primeiros são scripts únicos (não pollers) — `verify_execution_setup.py`
só lê saldo/posições (não escreve nada), `verify_shadow_trading.py` abre
um bracket real minúsculo (pouco acima do `min_notional`), confirma cada
peça contra o que a Binance reporta, e desfaz tudo antes de terminar.
`run_shadow_trading.py` é o poller de verdade — se recusa a rodar a menos
que `BINANCE_TESTNET=true` e `LIVE_TRADING=false` (o script para
imediatamente, sem tentar nada, se essas condições não baterem). Requer
`BINANCE_API_KEY`/`BINANCE_API_SECRET` de uma chave gerada especificamente
em testnet.binancefuture.com — chaves da conta Binance normal NÃO
funcionam contra a testnet (são sistemas de autenticação completamente
separados).

## Rodar o Paper Trading (dados reais)

```bash
python scripts/run_collector.py       # em um terminal - mantém os candles atualizados
python scripts/run_paper_trading.py   # em outro
```

Esse sim é um poller de verdade (roda pra sempre, `Ctrl+C` pra parar).
Inicializa a conta "paper" (idempotente — não reseta uma conta que já
existe), busca as regras reais de cada símbolo configurado e, a cada
`PAPER_TRADING_POLL_INTERVAL_SECONDS` (30s por padrão), roda um ciclo por
símbolo: se há posição aberta, checa saída contra o candle mais recente;
senão, checa o Kill Switch primeiro e, se livre, avalia o Strategy Engine.
`NO_NEW_CANDLE` (nenhum candle novo fechou desde o último ciclo) não é
logado a cada vez — só ações que de fato mudam alguma coisa.

## Rodar a demonstração do Kill Switch (dados reais)

```bash
python scripts/demo_kill_switch.py
```

Também não é um poller. Busca o preço real do BTCUSDT, dirige uma conta de
demonstração real no Postgres pra uma quebra de drawdown (dispara o Kill
Switch), mostra que ele continua travado mesmo depois de uma recuperação
de equity (a característica central de um kill switch — nunca destravar
sozinho), mostra a composição correta com o Risk Engine (uma proposta
saudável é bloqueada só pelo Kill Switch, sem nem chegar a rodar
`RiskEngine.evaluate()`), demonstra o reset manual (com nota obrigatória,
auditado) e um novo disparo depois do reset — agora por sequência de
perdas, não por drawdown. Cada transição de estado é logada e gravada em
`kill_switch_events`.

## Rodar o Backtest Engine (dados reais)

```bash
python scripts/run_backtest.py
```

Também não é um poller — um backtest é uma repetição única sobre dados que
já existem. O script busca as regras reais de BTCUSDT/ETHUSDT na Binance,
lê os 500 candles de 1h já persistidos pelo coletor (Fase 1/2/3) e roda
`BacktestEngine.run()` pra cada símbolo com as três estratégias em
confluência, logando as métricas agregadas e cada trade individual (side,
entrada/saída, motivo de saída, PnL líquido, R múltiplo) — e persiste cada
rodada via `BacktestRepository.save_run()` (`backtest_runs` +
`backtest_trades`, spec §86), retornando o `run_id` de cada símbolo.

## Rodar a simulação Monte Carlo (dados reais)

```bash
python scripts/run_monte_carlo.py
```

Roda o mesmo backtest real acima e, em seguida, reamostra a sequência de
trades resultante (bootstrap, 2000 simulações) pra reportar uma
distribuição de resultados possíveis, não só a única trajetória que
aconteceu — ver "Métricas da Fase 15b", item 11, e `aegis/backtest/
monte_carlo.py`. Não persiste o resultado do Monte Carlo em si (só o
backtest, como sempre); é uma ferramenta de análise sob demanda.

## O que foi construído (Fase 14 — Momentum Engine)

```
src/aegis/scanner/ranking.py
  rank_by_momentum() — função pura: dado uma lista de TickerStats (24h
  reais da Binance), filtra por USDT, por piso real de liquidez
  (min_quote_volume) e por um conjunto de exclusão, ordena por
  abs(price_change_pct), corta em top_n. Métrica de momentum
  deliberadamente simples (não um z-score de volume vs. baseline histórico
  — inventar isso sem evidência de que é melhor violaria a regra 151)
src/aegis/momentum/
  candles.py    klines_to_closed_dataframe() — converte klines de REST pro
                mesmo formato que CandleRepository.fetch_ohlcv devolve, mas
                com uma checagem que a leitura do banco nunca precisou:
                descarta a barra ainda em formação (comparando close_time
                contra agora), porque o endpoint de klines da Binance
                inclui a vela corrente incompleta como última linha se não
                for limitado por end time — tratá-la como fechada seria um
                bug de lookahead de verdade
  models.py     MomentumConfig, MomentumPosition (com trailing_order_id em
                vez de take_profit_order_id), MomentumTrade (carrega
                momentum_score pra análise futura)
  engine.py     MomentumTradingEngine — mesmo pipeline de decisão do Shadow
                Trading, com 3 diferenças: universo dinâmico (scan() a cada
                ciclo), candles via REST direto (não a tabela persistida do
                coletor), saída via stop de segurança + TRAILING_STOP_MARKET
                nativo em vez de take-profit fixo (RiskEngine.evaluate()
                roda com take_profit_price=None de propósito — o R múltiplo
                final de uma saída por trailing é inerentemente desconhecido
                na entrada, não tem sentido fingir um valor). Reconcilia
                posições abertas mesmo se o símbolo saiu do ranking atual
src/aegis/execution/binance_provider.py (estendido)
  open_trailing_bracket_position() — mesma política de falha do bracket
  fixo (Fase 10): se a perna de stop OU a de trailing falhar ao ser
  colocada, fecha a posição imediatamente, nunca deixa rodando sem
  proteção completa
src/aegis/providers/binance/rest_client.py (estendido)
  get_24h_tickers() — endpoint público, sem autenticação
  place_trailing_stop_order() — Algo Order tipo TRAILING_STOP_MARKET,
  valida callback_rate_pct no intervalo que a Binance aceita (0.1-10)
  antes de gastar uma chamada de rede
migrations/versions/0014_momentum.py   momentum_positions + momentum_trades
  + momentum_trading_cursor — mesmo formato de shadow_*, tabelas próprias
  (ver docstring da migration pro motivo)
src/aegis/db/momentum_repository.py    mesmo formato de ShadowRepository +
  get_open_symbols() (necessário pra reconciliar posições cujo símbolo
  saiu do ranking atual)
scripts/
  verify_momentum_trading.py   Scan real (só leitura) + um bracket real
                                pequeno com trailing stop, aberto/
                                verificado/desfeito — pegou um bug real
                                (ver Métricas)
  run_momentum_trading.py      Poller contínuo — mesmo gate de segurança
                                testnet-only do Shadow Trading
tests/
  test_scanner_ranking.py               7 testes puros
  test_momentum_candles.py              5 testes puros (incluindo o caso
                                         crítico: descartar a vela em
                                         formação)
  test_binance_execution_provider.py    +3 testes do trailing bracket
                                         (caminho feliz, falha da perna de
                                         trailing, falha da perna de stop —
                                         cada uma flattening corretamente)
  test_binance_signed_requests.py       +2 testes (parsing de TickerStats,
                                         validação de callback_rate_pct)
  test_momentum_engine.py               12 testes com repositórios/REST/
                                         execution provider falsos — mesmo
                                         padrão de cobertura do Shadow
                                         Trading, incluindo um teste de
                                         regressão específico pro
                                         arredondamento de tick_size
  test_momentum_repository_integration.py  7 testes contra Postgres real
```

## O que foi construído (Fase 11 — Dashboard)

```
src/aegis/api/
  app.py            create_app() — lifespan cria o pool uma vez na
                     inicialização (app.state.pool), fecha no shutdown.
                     Sem autenticação — documentado explicitamente como
                     decisão consciente (ferramenta local, um operador só)
  dependencies.py   Uma função de DI por repositório — todas reaproveitam
                     o MESMO pool via app.state, nenhuma lógica nova aqui
  routes.py         Todos os endpoints (um arquivo só — a contagem de
                     rotas não justifica separar em vários módulos ainda):
                       GET  /overview                    equity/drawdown/
                                                          PnL/Kill Switch
                                                          de paper+shadow
                       GET  /positions                    posições abertas
                                                          dos dois
                       GET  /trades?account=paper|shadow  trades fechados
                       GET  /backtests                    rodadas recentes
                       GET  /kill-switch/{id}/events       auditoria
                       POST /kill-switch/{id}/reset        ÚNICO endpoint
                                                          de escrita — exige
                                                          nota, nunca
                                                          silencioso
                       GET  /system/health                 último candle
                                                          por símbolo/
                                                          intervalo +
                                                          limiar de atraso
                                                          (2x o próprio
                                                          intervalo)
  static/index.html  Frontend puro (sem framework, sem build) — fetch()
                     pra cada endpoint a cada 10s, renderização direta em
                     DOM. Botão de reset do Kill Switch pede a nota via
                     prompt() antes de chamar a API
scripts/run_dashboard.py   uvicorn, bind só em 127.0.0.1
tests/test_api_routes.py   10 testes de integração contra Postgres real —
  como "paper"/"shadow" são as DUAS contas reais do sistema (não IDs
  aleatórios como os outros testes de repositório), as asserções checam
  formato/status HTTP em vez de valores exatos (o estado real muda
  conforme o resto do sistema roda), e o teste do reset do Kill Switch
  primeiro CONSULTA o estado real antes de tentar resetar — nunca arrisca
  limpar um disparo genuíno da conta real só porque um teste rodou
```

## O que foi construído (Fase 10 — Shadow Trading)

```
src/aegis/providers/binance/rest_client.py (estendido)
  Infraestrutura de assinatura HMAC-SHA256 (_sign, _signed_get,
  _signed_write) — colocar uma ordem NUNCA é re-tentado automaticamente
  (diferente de todo endpoint de leitura já existente): se a resposta de um
  POST /order se perde por erro de rede, não há como saber se a Binance
  recebeu e executou mesmo assim — reenviar às cegas poderia duplicar uma
  ordem real. recvWindow=10000 (não os 5000 "padrão de tutorial") depois de
  medir ao vivo que o relógio desta máquina estava ~4,7s atrasado do
  servidor da Binance — um problema real, não hipotético.
  place_market_order/place_stop_market_order/place_take_profit_market_order/
  cancel_order/cancel_algo_order/get_order/get_algo_order/get_account_balance/
  get_position_risk — stop/take-profit passam pelo endpoint de Algo Order
  (/fapi/v1/algoOrder), não o /fapi/v1/order antigo: a Binance migrou esses
  tipos de ordem condicional em 09/12/2025 (erro -4120 confirmado ao vivo
  contra a testnet antes de qualquer suposição da documentação)
src/aegis/providers/binance/models.py (estendido)
  OrderResult (ordens MARKET/LIMIT normais) + AlgoOrderResult (ordens
  condicionais — campos da Binance diferentes por baixo: algoId não
  orderId, algoStatus não status, actualPrice/actualQty não avgPrice/
  executedQty — normalizados pros MESMOS nomes de atributo de OrderResult,
  então quem chama trata as duas como intercambiáveis) + PositionRisk
src/aegis/execution/binance_provider.py
  BinanceExecutionProvider.open_bracket_position() — entrada a mercado,
  depois stop-loss e take-profit. Se QUALQUER uma das duas pernas
  protetoras falhar ao ser colocada, fecha a posição recém-aberta
  imediatamente (spec §110 — nunca uma posição sem stop — aplicado aqui no
  ponto de execução real, não só como precondição do RiskEngine). Política
  deliberadamente conservadora: uma posição parcialmente protegida (stop
  sem take-profit, por exemplo) também é fechada, não mantida rodando —
  preservação de capital antes de deixar um setup parcial correr
src/aegis/shadow/
  models.py     ShadowTradingConfig, ShadowPosition (com stop_order_id/
                take_profit_order_id — os IDs reais das pernas do bracket,
                necessários pra reconciliar depois), ShadowTrade
  engine.py     ShadowTradingEngine.run_once() — mesmo pipeline de decisão
                da Fase 9, mas a detecção de saída é fundamentalmente
                diferente: Paper Trading checa o high/low de um candle uma
                vez por ciclo (parecido com backtest); aqui o stop/TP são
                ordens reais que disparam sozinhas a qualquer momento — o
                trabalho do engine é perguntar pra exchange "ainda está
                aberto?" e, se não, descobrir qual das duas pernas
                executou (nunca inventa um preço de saída: se nenhuma das
                duas mostra FILLED mas a posição está flat, marca
                RECONCILIATION_FAILED e para de bloquear novas entradas,
                em vez de fabricar um resultado)
src/aegis/db/shadow_repository.py
  Mesma forma de PaperRepository (Fase 9) — no máximo uma posição aberta
  por conta+símbolo, log de trades fechados, cursor de candle — tabela
  própria, deliberadamente não compartilhada com paper_positions/
  paper_trades (campos genuinamente diferentes: aqui precisa dos IDs reais
  das ordens)
migrations/versions/0013_shadow_trading.py   shadow_positions +
  shadow_trades + shadow_trading_cursor
scripts/
  verify_execution_setup.py    Só leitura — confirma que as credenciais
                                funcionam antes de qualquer ordem
  verify_shadow_trading.py     Um bracket real pequeno, aberto/verificado/
                                desfeito manualmente — a validação ao vivo
                                que pegou os dois bugs reais desta fase
                                (ver Métricas)
  run_shadow_trading.py        O poller contínuo — recusa rodar fora de
                                BINANCE_TESTNET=true/LIVE_TRADING=false
tests/
  test_binance_signed_requests.py       14 testes offline — assinatura
                                HMAC bate com cálculo feito à mão, credencial
                                ausente levanta erro, parsing de
                                OrderResult/AlgoOrderResult/PositionRisk
                                (incluindo a normalização FINISHED->FILLED)
  test_binance_execution_provider.py    9 testes — bracket feliz (LONG e
                                SHORT), falha da perna de stop flatten+
                                levanta erro, falha da perna de take-profit
                                idem, flatten TAMBÉM falhando levanta erro
                                dizendo "MANUAL INTERVENTION", cancelamento
                                engole "unknown order" mas propaga outros
                                erros
  test_shadow_engine.py                 12 testes com repositórios e
                                execution provider falsos — histórico
                                insuficiente, conta não inicializada,
                                cursor evita reprocessamento, posição ainda
                                aberta segundo a exchange, fechamento via
                                stop E via take-profit (cada um cancelando
                                a perna oposta), RECONCILIATION_FAILED
                                quando nenhuma perna mostra FILLED, Kill
                                Switch bloqueia antes de qualquer sinal,
                                equity minúscula bloqueada pelo RiskEngine,
                                entrada válida persiste com os IDs reais
                                das ordens, falha de bracket não persiste
                                posição nenhuma
  test_shadow_repository_integration.py  6 testes contra Postgres real
```

## O que foi construído (Fase 9 — Paper Trading)

```
src/aegis/execution/   (extraído da Fase 8 durante esta fase)
  models.py    OpenPosition (movido de aegis.backtest.models, reexportado
               de lá pra nada quebrar) + FillOutcome (novo — resultado
               neutro de fechar uma posição, sem saber se quem chamou foi
               Backtest ou Paper Trading)
  fills.py     apply_slippage/compute_stop/compute_take_profit/check_exit/
               close_position — a MESMA matemática que já era da Fase 8,
               só que agora vive num lugar neutro que os dois engines
               importam, em vez de Paper Trading duplicar (ou pior,
               reimplementar ligeiramente diferente) a lógica de fill.
               `backtest/engine.py` virou um fino repassador dos mesmos
               nomes privados de antes (`_apply_slippage` etc.) — os 371
               testes que já existiam antes desta fase continuaram
               passando sem alteração nenhuma, confirmando que o refactor
               não mudou comportamento
src/aegis/paper/
  models.py    PaperTradingConfig (deliberadamente um dataclass próprio,
               não uma classe-base compartilhada com BacktestConfig — os
               dois têm ciclos de vida genuinamente diferentes; só a
               lógica que precisa ser idêntica de verdade — o fill — é
               compartilhada) + PaperTrade
  engine.py    PaperTradingEngine.run_once() — uma chamada, uma ação: ou
               checa saída de uma posição aberta, ou (se nenhuma aberta)
               checa o Kill Switch e avalia uma entrada nova. Nunca as
               duas coisas na mesma chamada — depois de fechar um trade, a
               próxima entrada só é avaliada no PRÓXIMO candle fechado,
               nunca no mesmo (comportamento conservador documentado, não
               acidental)
src/aegis/db/paper_repository.py
  get/open/close_position — no máximo uma posição aberta por (conta,
    símbolo); open_position levanta erro se já existe uma (nunca deveria
    acontecer dado como o engine chama, então um erro aqui pega um bug de
    verdade, não vira um "no-op silencioso")
  record/fetch_trades — log de trades fechados, mesma forma de
    backtest_trades mas tabela própria (tempo real decorrido, nunca
    comparável a uma rodada de backtest reprodutível)
  get/set_cursor — o único conceito genuinamente novo: lembra o
    close_time do último candle já processado por (conta, símbolo,
    interval), pra um poller chamando run_once() a cada 30s nunca agir
    duas vezes sobre o mesmo candle fechado
migrations/versions/0012_paper_trading.py   paper_positions (PK composta
  conta+símbolo) + paper_trades (append-only, indexada por conta+símbolo+
  tempo) + paper_trading_cursor (PK conta+símbolo+interval)
scripts/run_paper_trading.py   O primeiro poller de verdade desta fase
  (`while True` + `asyncio.sleep`, mesmo formato do run_macro_engine.py) —
  ver seção "Rodar" acima
tests/
  test_execution_fills.py           16 testes diretos do módulo
                                     compartilhado (não só via reexport do
                                     backtest) — prova que ele funciona
                                     sozinho, sem depender de nenhum engine
  test_paper_engine.py              9 testes com repositórios falsos em
                                     memória (mesmo padrão de
                                     test_backtest_engine.py) — histórico
                                     insuficiente, conta não inicializada
                                     levanta erro, cursor evita
                                     reprocessamento, posição aberta que
                                     não bate stop/TP continua aberta,
                                     stop batido fecha e grava o trade e
                                     atualiza a conta, Kill Switch bloqueia
                                     ANTES de calcular qualquer sinal,
                                     sem sinal não abre nada, equity
                                     minúscula é bloqueada pelo RiskEngine,
                                     entrada válida persiste a posição
  test_paper_repository_integration.py  9 testes contra Postgres real —
                                     round-trip de posição, erro ao tentar
                                     abrir uma posição em cima de outra já
                                     aberta, close é no-op seguro quando
                                     não há nada aberto, posições
                                     independentes por símbolo, trades
                                     gravados e filtrados por símbolo,
                                     cursor default None e round-trip
```

## O que foi construído (Fase 7b — Kill Switch global)

```
src/aegis/risk/kill_switch.py
  evaluate_kill_switch_triggers() — função pura, sem I/O: recebe
  equity/peak_equity/consecutive_losses + os dois thresholds já existentes
  em Settings (max_drawdown, loss_streak_halt_threshold) e devolve a lista
  de gatilhos aplicáveis (nunca para no primeiro — mesma disciplina de
  auditoria do RiskEngine.evaluate()). Reaproveitada em DOIS lugares:
  KillSwitchRepository (ao vivo, persistido) e BacktestEngine (em memória,
  simulado) — mesma lógica de detecção, I/O diferente, o mesmo princípio
  que já rege RiskEngine em paper/live/backtest
src/aegis/db/kill_switch_repository.py
  get_state() — nunca retorna None; conta sem linha ainda = não disparado
  check_and_maybe_trigger() — pegajoso de propósito: se já disparado, é
    uma leitura barata que não reavalia nem duplica evento; senão avalia
    os gatilhos e, se algum bater, grava kill_switch_state + UMA linha em
    kill_switch_events (transação única)
  reset() — a única saída; exige uma nota (auditoria) e levanta erro se a
    conta não estava disparada (resetar algo que não disparou é quase
    certamente um bug de quem chama, não um no-op inofensivo)
  fetch_events() — histórico completo de disparos/resets por conta
migrations/versions/0010_kill_switch.py   kill_switch_state (uma linha por
  conta, "current state") + kill_switch_events (log append-only) — tabelas
  planas, não hypertables (um kill switch deve disparar raramente, ao
  contrário de risk_events que cresce a cada avaliação de risco)
migrations/versions/0011_backtest_kill_switch.py   3 colunas novas em
  backtest_runs (kill_switch_triggered/reasons/tripped_at) — um bug real
  de contagem de parâmetros SQL (`INSERT has more target columns than
  expressions`) foi pego pelos testes de integração antes de ir pra
  produção, não descoberto em produção
src/aegis/backtest/engine.py (alterado)   depois de cada trade fechar (ou
  no fechamento forçado de fim de dado), roda evaluate_kill_switch_triggers
  sobre o AccountState em memória do backtest; se disparar, nenhuma barra
  seguinte abre posição nova — mas posições já abertas continuam saindo
  normalmente. Deliberadamente NUNCA toca KillSwitchRepository/Postgres
  (mesmo princípio que já protege _apply_trade_outcome: "um backtest nunca
  deve tocar o estado de conta real")
scripts/demo_kill_switch.py   Demonstração/validação contra dados reais
                               (ver seção "Rodar" acima)
tests/
  test_kill_switch_rules.py   7 testes puros — sem gatilho no normal,
                               limites inclusivos de cada gatilho
                               (drawdown e loss-streak), os dois juntos
                               acumulando, divisão por zero de peak_equity
                               nunca dispara por acidente
  test_kill_switch_repository_integration.py   7 testes contra Postgres
                               real — conta desconhecida não disparada,
                               disparo grava exatamente 1 evento, checagem
                               repetida NÃO duplica evento nem destrava
                               sozinha após recuperação, reset limpa
                               estado e grava nota, reset sem disparo
                               levanta erro, um novo disparo depois de um
                               reset funciona e o log fica correto
  test_backtest_engine_kill_switch.py   2 testes de integração — disparo
                               no primeiro loss bloqueia toda entrada
                               posterior (nenhum trade entra depois do
                               tripped_at), e o caso "nunca dispara" com
                               thresholds altos o bastante pra não serem
                               alcançados em 320 barras
```

## O que foi construído (Fase 8 — Strategy Engine + Backtest Engine)

```
src/aegis/strategy/
  models.py       StrategySignal (dataclass) — strategy_id, symbol, interval,
                  as_of, signal (LONG/SHORT/NO_TRADE), strength (0-100), reasons
  strategies.py   Três funções puras, cada uma recebendo um TechnicalSnapshot
                  (Fase 3) e retornando um StrategySignal:
                    evaluate_trend_pullback   EMA50>EMA200 + ADX>=20 +
                                              RSI em [40,60] + estrutura
                                              UPTREND (e o espelho pra SHORT)
                    evaluate_breakout         snapshot.breakout/breakdown +
                                              volume_zscore_20 acima de um
                                              limiar
                    evaluate_mean_reversion   ADX<=20 (mercado em range) +
                                              RSI extremo + distância do VWAP
                  Deliberadamente NÃO inclui LIQUIDATION_SQUEEZE nem
                  EVENT_REACTION — precisam de histórico de derivativos/
                  macro/news real e contínuo que hoje só foi smoke-testado,
                  não rodado de forma contínua (ver Riscos)
                  [Estado na época da Fase 8. EVENT_REACTION foi
                  implementada depois, na Fase 15b (item 12) - existe e é
                  testada, mas ainda não ligada a nenhum motor real.]
  confluence.py   combine_signals() — votação ponderada (Σ direção×força×peso
                  / Σpeso), decisão LONG/SHORT se |net_score| >= threshold
                  (30 por padrão), senão NO_TRADE. Pesos default (1.0 pra
                  cada estratégia) documentados como hipótese não validada
                  (spec §54), não uma calibração
src/aegis/backtest/
  models.py       BacktestConfig, BacktestTrade, OpenPosition, BacktestResult
                  (com data_start/data_end/total_bars — o range de candle
                  que a rodada de fato consumiu, incluindo warmup)
  metrics.py      compute_metrics() — win rate, profit factor, expectancy,
                  max drawdown, sharpe/sortino/calmar sobre a série de R
                  múltiplo por trade (não retornos anualizados — simplificação
                  documentada explicitamente no docstring, não escondida
                  como se fosse a fórmula clássica)
  engine.py       BacktestEngine.run() — o loop de replay. Regra dura: uma
                  decisão na barra i só enxerga df.iloc[:i+1] (barras 0..i,
                  já fechadas) — nunca a barra i+1 em diante. Um sinal gerado
                  a partir dessa janela preenche na ABERTURA da barra i+1,
                  não no fechamento da própria barra i (spec §71: "nunca
                  assumir execução perfeita") — um bar de latência realista.
                  Reaproveita o RiskEngine real da Fase 7 (spec §120: "a
                  única diferença deve ser o ExecutionProvider") — sizing e
                  gate são código idêntico ao que paper/live vão usar, não
                  uma reimplementação
src/aegis/db/backtest_repository.py
  save_run() — persiste backtest_runs + backtest_trades numa única
  transação (uma rodada sem seus trades, ou vice-versa, seria um registro
  enganoso). Métricas ficam em colunas normais, não JSONB — BacktestMetrics
  é uma estrutura fixa escrita por um produtor só, então não precisa da
  flexibilidade de merge que market_features usa. strategy_weights é o
  único campo JSONB (dict opcional de tamanho variável) — decodificado de
  volta pra dict em fetch_run/fetch_recent_runs, já que asyncpg devolve
  jsonb como texto cru por padrão, sem um codec registrado
migrations/versions/0009_backtest.py   backtest_runs (uma linha por
                                        rodada) + backtest_trades (FK
                                        run_id, ON DELETE CASCADE) — tabelas
                                        planas, não hypertables (uma rodada
                                        escreve tudo de uma vez só; leitura
                                        é sempre "essa rodada" ou "rodadas
                                        recentes", nunca uma varredura por
                                        intervalo de tempo)
scripts/run_backtest.py   Demonstração/validação contra dados reais, agora
                          persistindo cada rodada (ver seção "Rodar" acima)
tests/
  test_strategies.py             18 testes — cada estratégia, cada lado
                                  (LONG/SHORT), campos obrigatórios ausentes
                                  -> INSUFFICIENT_DATA/NO_TRADE
  test_confluence.py             10 testes — votação ponderada, threshold,
                                  lista vazia levanta ValueError
  test_backtest_metrics.py       14 testes — cada métrica contra valor
                                  calculado à mão, casos indefinidos
                                  (profit_factor sem perdas, sharpe com
                                  variância zero) retornam None, não 0/inf
  test_backtest_engine_helpers.py  17 testes — slippage, cálculo de stop/TP,
                                    checagem de saída, fechamento de trade,
                                    transição de AccountState, tudo puro
  test_backtest_engine.py        4 testes de integração — ValueError com
                                  histórico insuficiente; ciclo completo
                                  abrindo/fechando trades sobre uma
                                  consolidação->breakout sintética; equity
                                  minúsculo bloqueado pelo RiskEngine (não
                                  ignorado silenciosamente); e o teste de
                                  não-lookahead (ver "Como a Fase 8 cumpre
                                  a especificação")
  test_backtest_repository_integration.py  7 testes contra Postgres real —
                                  round-trip completo de run+trades,
                                  rodada sem trades, strategy_weights
                                  decodificado como dict (não string crua),
                                  ordenação/filtro de fetch_recent_runs,
                                  round-trip de um disparo de Kill Switch
                                  (colunas adicionadas na Fase 7b)
```

## O que foi construído (Fase 7 — Risk Engine)

```
src/aegis/risk/
  rules.py     Funções puras: compute_drawdown_state (spec §21),
               loss_streak_action (spec §22), compute_net_r_multiple
               (spec §56, líquido de fees/slippage), estimate_liquidation_
               distance_pct + check_liquidation_distance (spec §112 —
               aproximação documentada como tal, não a tabela real de
               margem de manutenção em camadas da Binance)
  sizing.py    calculate_position_size (spec §19), reaproveitando
               SymbolRules da Fase 1 — nunca hard-codeia tick/step/
               minNotional. round_down_to_step usa Decimal, não float
               puro, pra evitar drift binário no que é literalmente
               "quanto dinheiro está em risco"
  service.py    TradeProposal / AccountState / RiskDecision (dataclasses)
               + RiskEngine.evaluate() — acumula TODOS os motivos de
               bloqueio aplicáveis em vez de parar no primeiro, e reduz
               risk_per_trade pelo fator mais conservador entre drawdown
               e loss-streak quando os dois se aplicam ao mesmo tempo
src/aegis/db/risk_repository.py
  get/initialize_account_state, record_trade_outcome (um UPDATE...RETURNING
  atômico só — nunca lê-modifica-escreve em Python), reset_daily,
  set_exposure, insert_risk_event/fetch_recent_events (auditoria, spec §85)
migrations/versions/0008_risk.py   risk_account_state (uma linha por
                                    conta) + risk_events (hypertable,
                                    cresce a cada avaliação futura)
scripts/demo_risk_engine.py        Demonstração/validação (não é um loop
                                    de produção — ver seção "Rodar" acima)
tests/
  test_risk_rules.py         cada regra contra valor calculado à mão,
                              incluindo limites inclusivos dos estados
  test_risk_sizing.py        sizing + arredondamento por Decimal
                              (incluindo o clássico drift de float 0.1+0.2)
  test_risk_service.py       RiskEngine.evaluate() cenário por cenário —
                              16 testes, um por motivo de bloqueio e
                              combinações (múltiplos motivos ao mesmo tempo,
                              fator de redução mais conservador vencendo)
  test_risk_integration.py   RiskRepository contra Postgres real, incluindo
                              atomicidade do record_trade_outcome e reset diário
```

## O que foi construído (Fase 6 — News Engine, core + 6b)

```
news_sources.yaml   Registro de fontes rastreadas (spec §38) — Federal
                     Reserve, SEC, CFTC (PRIMARY_OFFICIAL) + MarketWatch,
                     CNBC (TIER_1_FINANCIAL_MEDIA) + CoinDesk, Cointelegraph,
                     Decrypt, The Block (SPECIALIZED_CRYPTO_MEDIA) — os 9
                     feed_url confirmados ao vivo (HTTP 200, XML válido,
                     itens reais e recentes) antes de entrar no arquivo;
                     um décimo candidato (Reuters, domínio antigo) foi
                     testado e descartado por não resolver mais
src/aegis/providers/rss/client.py
  RssFeedProvider (implementa NewsProvider) — parser único que detecta
  RSS 2.0 vs. Atom pela raiz do XML (`<rss>` vs `<feed>` com namespace),
  sem biblioteca externa (xml.etree.ElementTree da stdlib), sem scraping —
  só o que o próprio feed publica
src/aegis/news/
  classifier.py   Funções puras e determinísticas (não é ML/LLM, documentado
                   como tal no docstring): source_quality_score (os 5 níveis
                   exatos do spec §40), extract_assets (palavra-inteira,
                   case-insensitive), classify_sentiment (léxico positivo/
                   negativo), classify_magnitude (HIGH/MEDIUM/LOW por
                   palavra-chave) — event_type e time_sensitivity ficaram
                   de fora de propósito (spec §83 reserva classificação real
                   pra um componente de IA futuro; um léxico não faz esse
                   trabalho bem)
  conflict.py     analyze_asset_conflict() (pura, spec §41 aplicado ao pé
                   da letra): 1 fonte só -> WAIT_FOR_CONFIRMATION; 2+
                   fontes concordando (ou uma neutra) -> CONFIRMED; 2+
                   discordando sem fonte PRIMARY_OFFICIAL -> NEWS_CONFLICT;
                   discordando mas com uma fonte primária -> CONFIRMED com
                   o veredito da primária. Usa só o item mais recente por
                   fonte, não todo o histórico.
  source_config.py   load_news_sources() — carrega o YAML, nunca assume
                      feed_url inline no código
  service.py          ClassifiedNewsItem (dataclass) + classify_news_item()
                      (pura) + NewsEngine (poll -> classifica cada entrada
                      -> persiste -> update_conflict_statuses() sobre o
                      universo de ativos que o classificador reconhece;
                      uma fonte ou um ativo com falha não derruba o ciclo)
src/aegis/db/news_repository.py
  upsert_source_registry, insert_news_items (reclassificação sobrescreve em
  lugar — igual ao dado macro revisado — grava news + news_entities numa
  transação só), fetch_recent, fetch_recent_by_asset (join com news_entities
  + news_sources, filtrado por janela), upsert_asset_status (uma linha por
  ativo, NO_NEWS nunca é persistido)
migrations/versions/0006_news.py   news_sources, news, news_entities —
                                    tabelas normais, não hypertables (poucos
                                    itens por dia, mesmo raciocínio do 0005)
migrations/versions/0007_news_conflict.py   news_asset_status — uma linha
                                    por ativo, sem JSONB (produtor único,
                                    mesmo raciocínio do macro_snapshots)
scripts/run_news_engine.py         Loop de produção — poll de todas as
                                    fontes seguido de update_conflict_statuses()
tests/
  test_rss_client.py           parsing RSS e Atom, XML malformado, root
                                não reconhecido, retry/erro
  test_news_classifier.py      cada heurística contra caso conhecido
                                (incluindo "ETH" não casar dentro de "method")
  test_news_conflict.py        analyze_asset_conflict contra os 4 veredictos
                                possíveis, incluindo desempate por fonte
                                primária e "usa só o item mais recente por fonte"
  test_news_source_config.py   loader + valida o news_sources.yaml real do
                                repo, incluindo cobertura dos 3 níveis de
                                qualidade usados
  test_news_service.py         classify_news_item + NewsEngine com fakes,
                                incluindo update_conflict_statuses
  test_news_integration.py     NewsRepository contra Postgres real, incluindo
                                dedup, reclassificação, menções multi-ativo,
                                fetch_recent_by_asset e upsert_asset_status
```

## O que foi construído (Fase 5 — Macro Engine, FRED + BLS + BEA)

```
macro_series.yaml   Registro de séries rastreadas (spec §33) — FEDFUNDS,
                     DGS2, DGS10, CPIAUCSL, UNRATE, NFCI (fred) +
                     LNS14000000, CES0000000001, CUSR0000SA0 (bls) +
                     A191RL / Real GDP (bea)
src/aegis/providers/fred/client.py
  FredProvider (implementa MacroDataProvider) — retry/backoff igual ao
  cliente REST da Binance, chave obrigatória, janela de histórico
  (history_days) é responsabilidade do próprio provider, não de quem chama
src/aegis/providers/bls/client.py
  BlsProvider (implementa MacroDataProvider) — POST com corpo JSON
  (diferente do GET da FRED), chave opcional (funciona sem, limites
  menores), intervalo de anos (history_years) também é interno ao provider
src/aegis/providers/bea/client.py
  BeaProvider — GET com UserID/DataSetName/TableName/Frequency/Year, chave
  obrigatória. BEA não tem "series ID" plano: a resposta traz TODAS as
  linhas da tabela, então `_extract_rows` filtra por `SeriesCode` antes de
  devolver — é o único provider cuja `get_series()` recebe mais contexto
  do que só o id (dataset/tabela/frequência vêm do MacroSeriesConfig)
src/aegis/macro/
  models.py            MacroObservation compartilhado, com três construtores
                        (from_fred_payload / from_bls_payload / from_bea_payload)
                        — mesmo padrão de Kline.from_rest_row/from_ws_payload.
                        FRED marca ausência com "."; BLS tem a linha M13
                        (média anual) descartada; BEA usa "(NA)" e formata
                        números com vírgula de milhar ("23,542.7")
  series_config.py      load_macro_series() — carrega o YAML; ganhou campos
                        opcionais dataset/table/bea_frequency só pro BEA
  service.py             MacroSnapshot (dataclass) + compute_snapshot() (pura,
                         reaproveita aegis.stats) + MacroEngine, que recebe um
                         dict {source: provider} e roteia cada série pelo seu
                         MacroSeriesConfig.source — `_fetch_raw()` é o único
                         ponto que sabe que o BEA precisa de mais argumentos
src/aegis/db/macro_repository.py
  upsert_series_registry, insert_observations (upsert, não idempotente-
  descarta — dado macro é revisado), fetch_observations, upsert_snapshot
  (uma linha por série, substituída a cada ciclo — não é JSONB mergeável
  como market_features, porque só há um produtor)
migrations/versions/0005_macro.py   macro_series, macro_observations,
                                     macro_snapshots — tabelas normais, não
                                     hypertables (volume baixo demais para
                                     justificar particionamento). Mesmas
                                     tabelas servem as três fontes.
scripts/run_macro_engine.py         Loop de produção — monta os três
                                     providers; BLS sempre, FRED/BEA se
                                     houver chave
tests/
  test_fred_client.py           retry/erro/chave ausente, httpx.MockTransport
  test_bls_client.py            retry/erro/chave opcional, range de anos
  test_bea_client.py            retry/erro/chave obrigatória, filtro por
                                 SeriesCode, erro em dois formatos diferentes
                                 (BEAAPI.Error vs Results.Error)
  test_macro_models.py          parsing FRED ("." ausente), BLS (M13,
                                 período não-mensal) e BEA (trimestre/mês/
                                 ano, vírgula de milhar, marcador "(NA)")
  test_macro_series_config.py   loader + campos opcionais do BEA + valida
                                 o macro_series.yaml real do repo
  test_macro_service.py         compute_snapshot + MacroEngine com fakes,
                                 incluindo roteamento fred/bls misturado,
                                 o caminho especial do BEA (_fetch_raw) e
                                 fonte sem provider registrado
  test_macro_integration.py     MacroRepository contra Postgres real,
                                 incluindo upsert de valor revisado
```

## O que foi construído (Fase 4b — Liquidation Engine)

```
src/aegis/liquidation/service.py
  LiquidationSnapshot (dataclass) + compute_snapshot() (pura, reaproveita
  aegis.derivatives.analytics para pct_change/z-score/aceleração — nenhuma
  estatística nova foi escrita) + _squeeze_score() (heurística documentada,
  pesos 0.4/0.3/0.3 como hipótese, não validados por backtest ainda) +
  LiquidationEngine (WS -> buffer -> flush periódico -> janela via
  time_bucket() -> snapshot -> merge no market_features)
src/aegis/db/liquidation_repository.py
  insert_events (idempotente) + fetch_windowed_stats (agrega no Postgres
  via time_bucket(), devolve DataFrame ascendente pronto para as funções
  de analytics)
migrations/versions/0004_liquidations.py   hypertable de eventos brutos
scripts/run_liquidation_engine.py          Loop de produção
tests/
  test_liquidation_service.py       compute_snapshot, squeeze_score,
                                     LiquidationEngine com repositórios fake
  test_liquidation_integration.py   LiquidationRepository contra Postgres
                                     real (dedup, windowing por time_bucket)
```

## O que foi construído (Fase 4c — Order Book Engine)

```
src/aegis/orderbook/
  analytics.py   Funções puras: microprice, top_of_book_imbalance,
                 depth_notional, depth_imbalance, book_pressure
                 (book_pressure pondera níveis mais próximos do topo)
  service.py     OrderBookFeatureSnapshot (dataclass) + compute_snapshot()
                 (pura) + OrderBookEngine (mantém só o último livro por
                 símbolo em memória; nunca persiste tick bruto)
scripts/run_orderbook_engine.py   Loop de produção
tests/
  test_orderbook_analytics.py   cada função contra valor calculado à mão
  test_orderbook_service.py     compute_snapshot + OrderBookEngine com
                                 repositório fake
```

## O que foi construído (Fase 4)

```
src/aegis/derivatives/
  analytics.py           Funções puras: latest_pct_change, latest_zscore,
                          acceleration — usadas por OI, long/short ratio e funding
  service.py              DerivativesSnapshot (dataclass) + compute_snapshot()
                          (pura, testável sem I/O) + DerivativesEngine (polling
                          REST -> persistência bruta -> cálculo -> merge no
                          market_features; uma falha por symbol/period não
                          derruba o ciclo inteiro)
src/aegis/db/derivatives_repository.py
                          DerivativesRepository: insert_open_interest,
                          insert_long_short_ratios (idempotentes, ON CONFLICT
                          DO NOTHING) + fetch_* (leem de volta como DataFrame
                          ascendente, igual ao CandleRepository)
migrations/versions/0003_derivatives.py   open_interest e long_short_ratios
                                           (hypertables, dados brutos)
scripts/run_derivatives_engine.py         Loop de produção (polling contínuo)
tests/
  test_derivatives_analytics.py     cada função contra valor calculado à mão
  test_derivatives_service.py       compute_snapshot (todos os 4 padrões
                                     PRICE×OI) + DerivativesEngine com REST/
                                     repositórios fake
  test_derivatives_integration.py   DerivativesRepository contra Postgres
                                     real + merge real no market_features
                                     compartilhado com TechnicalSnapshot
```

## O que foi construído (Fase 3)

```
src/aegis/technical/
  indicators.py         Funções puras: sma, ema, rsi, atr, adx, bollinger_bands,
                         macd, roc, momentum, daily_anchored_vwap, volume_zscore,
                         percentile_rank, pct_distance — todas com min_periods
                         (NaN até ter histórico suficiente, nunca um valor forjado)
  market_structure.py   Detecção de swing points por fractal (left/right bars),
                         classificação HH/HL/LH/LL -> UPTREND/DOWNTREND/RANGING,
                         breakout/breakdown contra o último swing confirmado
  service.py             TechnicalSnapshot (dataclass) + compute_snapshot()
                         (função pura, testável sem banco) + TechnicalAnalysisService
                         (multi-timeframe: um snapshot por intervalo configurado)
src/aegis/db/
  candle_repository.py   CandleRepository.fetch_ohlcv() -> DataFrame ascendente,
                         só candles fechadas por padrão
  feature_repository.py  FeatureRepository.upsert_snapshot() -> merge em
                         market_features (JSONB `||`, nunca substitui a linha)
migrations/versions/0002_market_features.py   Tabela única de feature store
                                               (JSONB + GIN index), hypertable
scripts/run_technical_snapshot.py   Smoke test manual contra dados reais
tests/
  test_indicators.py            cada indicador contra valor calculado à mão
  test_market_structure.py      sequência de preços desenhada à mão com
                                 swings/tendência conhecidos
  test_technical_service.py     compute_snapshot + TechnicalAnalysisService,
                                 com fonte de candles fake
  test_technical_integration.py CandleRepository + FeatureRepository
                                 contra Postgres real (skip se indisponível)
```

## O que foi construído (Fase 2)

```
backend/
  docker-compose.yml            (raiz do repo) TimescaleDB local
  alembic.ini, migrations/
    env.py                       lê DATABASE_URL do Settings, roda sync via psycopg2
    versions/0001_initial_schema.py   assets, candles, trades, funding_rates, book_ticker
                                       (todas hypertables, exceto assets)
  src/aegis/db/
    engine.py                    pool asyncpg (create_pool/close_pool)
    market_repository.py         MarketRepository: upsert_assets, upsert_candles,
                                  insert_trades, insert_funding_rates, insert_book_ticker
                                  — tudo batched via executemany, idempotente (ON CONFLICT)
    persistence_writer.py        PersistenceWriter: on_event() enfileira (não bloqueia o
                                  dispatch do WS); task de fundo drena por tamanho de lote
                                  ou intervalo de tempo e escreve via MarketRepository
scripts/run_collector.py        Agora liga PersistenceWriter quando DATABASE_URL existe
tests/
  test_persistence_writer.py            batching/flush/drop, com repositório fake
  test_market_repository_integration.py CRUD real contra Postgres (skip se indisponível)
```

## Como a Fase 2 cumpre a especificação

- **Nunca um round-trip de banco por evento** (implícito em toda a spec de
  performance/resiliência): `on_event` é síncrono e só enfileira;
  `PersistenceWriter` drena em lotes de até `DB_WRITER_BATCH_SIZE` ou a
  cada `DB_WRITER_FLUSH_INTERVAL` segundos, o que vier primeiro.
- **Nunca perde dado silenciosamente, mas também nunca trava o coletor**
  (espírito do spec §100, resiliência): fila com tamanho máximo
  (`DB_WRITER_QUEUE_MAX_SIZE`); se o banco cair/atrasar além da capacidade
  da fila, eventos novos são contados em `events_dropped` e logados — nunca
  bloqueiam o WebSocket.
- **Idempotência** (spec §100): candles usam `ON CONFLICT ... DO UPDATE`
  (a mesma vela é atualizada enquanto forma, até fechar); trades,
  funding_rates e book_ticker usam `ON CONFLICT ... DO NOTHING` (eventos
  imutáveis, redelivery do WS ou do gap-fill REST não duplica linhas).
- **Auto-registro de ativos**: `assets` é populada sozinha na primeira vez
  que um símbolo aparece — semente para o `AssetScanner` de uma fase
  futura, sem trabalho manual agora.
- **Migrations com rollback** (spec §139): cada tabela tem `downgrade()`
  simétrico; testado (`alembic upgrade head` já rodou verde neste ambiente).
- **TimescaleDB hypertables**: candles, trades, funding_rates e book_ticker
  são particionadas por tempo (`create_hypertable`) — `assets` fica como
  tabela normal (não é série temporal).

## Como a Fase 3 cumpre a especificação

- **Nunca um indicador isolado decide nada** (spec §2): `TechnicalSnapshot`
  é só o insumo técnico — uma dimensão entre várias que o futuro
  `ConfluenceEngine` (Fase 7+) vai combinar com derivativos/macro/notícias
  antes de qualquer sinal.
- **Nunca inventa um valor com histórico insuficiente** (spec §46,
  anti-leakage): todo `rolling`/`ewm` usa `min_periods` — `ema_200` fica
  `None` até existirem 200 candles fechadas de verdade, nunca uma
  aproximação silenciosa. `quality` no snapshot (`NO_DATA` /
  `PARTIAL_HISTORY` / `OK`) deixa isso explícito para quem consome.
- **Sem look-ahead** (spec §69): swing points usam um fractal simétrico
  (`left`/`right` barras) — os últimos `right` candles nunca podem virar um
  "swing recente" ainda, então breakout/breakdown sempre comparam contra um
  nível que já existia *antes* da barra atual fechar.
- **Multi-timeframe de verdade** (spec §26): `TechnicalAnalysisService`
  calcula um snapshot independente por intervalo configurado
  (1m/5m/15m/1h/4h/1d) — o smoke test real mostrou tendências diferentes
  entre timeframes do mesmo ativo, exatamente o tipo de divergência que
  confluência multi-timeframe existe para capturar.
- **Feature store único e mergeável** (spec §45): `market_features` é uma
  tabela por (symbol, interval, as_of) com um payload JSONB; escritas usam
  `features || EXCLUDED.features`, então a Fase 4 (Derivatives) e as
  seguintes só adicionam chaves na mesma linha, nunca criam tabelas
  paralelas nem apagam o que a Fase 3 já calculou.

## Como a Fase 4 cumpre a especificação

- **Padrões, não sinais** (spec §28, textual): "Utilizar esses padrões como
  features. Não interpretar automaticamente como BUY ou SELL." —
  `price_oi_pattern` é só uma string classificatória no feature store;
  nada aqui decide nada.
- **Reaproveita, não duplica, o funding já coletado** (evita trabalho e
  superfície de bug desnecessários): `funding_rate`/`basis`/`basis_pct` são
  lidos de volta da tabela `funding_rates` (populada pelo stream
  `markPrice` desde a Fase 1) — nenhum polling novo para isso.
- **REST só onde a Binance não tem stream** (spec §5): confirmado ao vivo
  nesta fase — os 4 endpoints `/futures/data/*` (OI histórico, 3 variantes
  de long/short ratio) genuinamente não existem no testnet (redirecionam
  para uma página de marketing); só produção os serve. Como são só-leitura,
  públicos e sem risco de trading, o cliente REST os chama sempre contra
  produção, independente de `BINANCE_TESTNET` — documentado em
  `constants.STATS_BASE_URL`.
- **Uma falha não derruba o ciclo** (spec §100, resiliência): `run_once()`
  captura exceção por (symbol, period) individualmente e segue para o
  próximo — testado explicitamente (`test_run_once_survives_one_symbol_failing`).
- **Feature store realmente compartilhado** (spec §45, validado nesta fase):
  `DerivativesSnapshot` e `TechnicalSnapshot` escrevem na mesma linha de
  `market_features` para o mesmo (symbol, timeframe, as_of) — confirmado
  por teste de integração e pelo smoke test ao vivo.

## Como a Fase 4b/4c cumprem a especificação

- **Padrão de snapshot puro + engine com I/O, repetido pela terceira vez**
  (Technical -> Derivatives -> Liquidation/OrderBook): cada engine tem uma
  função `compute_snapshot()` que só recebe dados já carregados e devolve
  um dataclass — zero rede, zero banco, 100% testável com dados sintéticos.
  A classe `*Engine` é a única parte que fala com WS/REST/Postgres.
- **Reuso de estatística, não reinvenção** (evita bugs de fórmula
  duplicada): `LiquidationEngine` importa `aegis.derivatives.analytics`
  diretamente — `latest_pct_change`, `latest_zscore` e `acceleration` já
  validados na Fase 4 servem sem alteração para notional de liquidação.
- **SqueezeScore é uma hipótese documentada, não um modelo validado**
  (spec §54: "Não fixar pesos sem backtest"): os pesos 0.4/0.3/0.3 do
  `squeeze_score` estão no docstring do módulo como o que são — um ponto
  de partida a validar, nunca uma fórmula com autoridade.
- **`book_ticker` ensinou a lição, `orderbook` aplicou** (spec §100,
  eficiência de storage): profundidade parcial chega a 10x/segundo por
  símbolo — só o snapshot periódico derivado é gravado, nunca o tick bruto,
  decisão tomada *antes* de qualquer problema de volume aparecer, não
  depois.
- **Um símbolo ruim não derruba o ciclo** (spec §100, resiliência): tanto
  `LiquidationEngine.run_snapshot_cycle()` quanto
  `OrderBookEngine.run_snapshot_cycle()` isolam falha por símbolo, mesmo
  padrão já testado no `DerivativesEngine`.

## Como a Fase 5/5b/5c cumprem a especificação

- **Nunca inventa um dado ausente** (spec §43, aplicado agora a dado
  macro): FRED marca observação ausente com o literal `"."`, BLS omite o
  valor ou usa `"-"`, BEA usa `"(NA)"` — os três parsers
  (`from_fred_payload`, `from_bls_payload`, `from_bea_payload`) devolvem
  `None` nesses casos, nunca `0.0`. `MacroEngine.poll_and_store_one`
  filtra esses `None` antes de persistir.
- **Um engine, três fontes, sem gambiarra** (spec §101, abstração de
  provider): `MacroEngine` recebe `providers: dict[str, provider]` e
  `_PARSERS: dict[str, callable]` — adicionar BLS e depois BEA não tocou
  em `compute_snapshot`, `MacroRepository` nem em nenhuma tabela; só
  registrou mais um provider e mais um parser. O BEA, que precisa de mais
  contexto que só o `series_id`, ficou isolado num único método
  (`_fetch_raw`) em vez de vazar parâmetros específicos pro resto do engine.
- **Duas fontes independentes convergindo é a própria validação** (não é
  redundância — é o ponto): `UNRATE` (FRED) e `LNS14000000` (BLS) mediram
  4.1% no mesmo smoke test ao vivo; `CPIAUCSL` (FRED) e `CUSR0000SA0` (BLS)
  bateram em 334.131 com YoY 3.71%. Duas fontes primárias chegando ao
  mesmo número é exatamente o tipo de confluência que o blueprint pede
  entre dimensões independentes (spec §2) — aqui, entre fontes de dados.
- **`macro_series.yaml` é a fonte única de verdade** (spec §33, textual):
  nenhum `series_id` aparece hard-coded em código Python — tudo vem do
  YAML, carregado por `load_macro_series()`.
- **Honesto sobre o que "surpresa macro" significa aqui** (espírito do
  spec §105 — não fabricar o que a fonte gratuita não oferece): nenhuma
  das três fontes (FRED, BLS, BEA) publica consensos de previsão, só
  valores realizados (consenso é produto pago, tipo Bloomberg/Reuters).
  Em vez de inventar um campo "forecast", o
  `MacroSnapshot` calcula `zscore_vs_trailing` — o quão fora do padrão
  recente o valor está — documentado no docstring do módulo como uma
  substituição honesta, não como o `MacroSurpriseEngine` literal do
  blueprint (que exigiria uma fonte de consenso paga).
- **Observações são upsert, não idempotência-por-descarte** (diferença
  deliberada dos outros engines): candles/trades/liquidações são
  imutáveis uma vez criados; dado macro é revisado (PIB e CPI são
  restated depois da divulgação inicial) — `insert_observations` sempre
  sobrescreve o valor no `ON CONFLICT`, nunca ignora.
- **Tabela sem JSONB por design, não por esquecimento**: diferente de
  `market_features`, `macro_snapshots` tem colunas fixas — só existe um
  produtor (`MacroEngine`), então a flexibilidade de merge que
  `market_features` precisa (vários engines escrevendo na mesma linha)
  não se aplica aqui.

## Como a Fase 6 cumpre a especificação

- **Sem scraping agressivo** (spec §38, textual): `RssFeedProvider` só lê
  o XML que a própria fonte publica no feed oficial — nada de raspar HTML,
  nada de headless browser.
- **`SourceQualityScore` exatamente como o spec define** (spec §40): os 5
  níveis e seus 5 valores numéricos (1.00/0.90/0.75/0.30/0.00) estão
  literalmente copiados do texto da especificação para
  `SOURCE_QUALITY_SCORES` — nenhum valor inventado ou "ajustado".
- **Heurística assumida como heurística, não como IA** (spec §83, limite
  explícito do que IA generativa pode e não pode decidir): `classifier.py`
  é 100% determinístico, baseado em contagem de palavras-chave — o
  docstring do módulo diz isso abertamente e explica por que
  `event_type`/`time_sensitivity` ficaram de fora (uma lista de
  palavras-chave faria esse trabalho mal, então não faz).
- **Uma fonte ruim não derruba o ciclo** (spec §100, resiliência, mesmo
  padrão testado em todo engine desde a Fase 4): `NewsEngine.run_once()`
  isola falha por fonte.
- **Todo `feed_url` foi confirmado ao vivo antes de virar configuração**
  (spec §151: nunca inventar endpoint): os 9 candidatos que entraram foram
  testados com `curl` primeiro — dois descartados nesse processo (SEC
  litigation releases por HTTP 403 de rate limiting na Fase 6; o antigo
  domínio de RSS da Reuters na Fase 6b, que simplesmente não resolve mais)
  nunca chegaram a virar código.
- **§41 aplicado ao pé da letra, incluindo o "pode" da última frase**
  (spec §41: "Se for fonte primária: pode receber maior peso"): uma fonte
  `PRIMARY_OFFICIAL` desempata em vez de virar `NEWS_CONFLICT` — mas só
  quando existe desacordo de verdade; duas fontes concordando não passam
  por essa regra, e uma única fonte primária sozinha ainda cai em
  `WAIT_FOR_CONFIRMATION` como qualquer fonte única.
- **Conflito usa o item mais recente por fonte, não o histórico inteiro**
  (evita que uma opinião antiga e já superada continue contando): se a
  mesma fonte publicou "negativo" ontem e "positivo" hoje sobre o mesmo
  ativo, só o "positivo" de hoje entra na conta — testado explicitamente
  (`test_uses_the_latest_item_per_source_not_every_item`).

## Como a Fase 14 cumpre a especificação

- **Preservação de capital antes de retorno prometido, aplicado numa
  decisão real de escopo, não só como slogan** (o princípio central do
  spec, citado desde a Fase 0): o piso de liquidez (`min_quote_volume`)
  não é um detalhe de configuração — é a diferença entre "buscar força de
  mercado real" e "apostar em token fino sujeito a manipulação/gap/
  delisting". O usuário foi consultado explicitamente sobre essa troca
  antes de eu escrever uma linha de código, e escolheu a opção mais
  conservadora.
- **Nunca inventa um R múltiplo que não existe** (regra 151): com saída
  por trailing stop, o resultado final é desconhecido na entrada —
  `TradeProposal.take_profit_price=None` faz `RiskEngine` pular o check de
  BAD_RISK_REWARD de propósito, documentado no código como uma
  consequência do desenho, não uma lacuna esquecida.
- **Nunca trata a barra em formação como fechada** (spec §69, a mesma
  disciplina de não-lookahead do Backtest Engine, agora aplicada a dados
  vindos direto de REST em vez de uma tabela já filtrada):
  `klines_to_closed_dataframe` descarta explicitamente qualquer barra cujo
  `close_time` ainda esteja no futuro — testado (`test_drops_the_still_
  forming_last_candle`), não assumido.
- **Nunca uma posição sem proteção completa** (mesmo princípio da Fase 10,
  reaplicado ao bracket com trailing): `open_trailing_bracket_position`
  fecha a posição imediatamente se a perna de stop OU a de trailing falhar
  — testado para os dois casos.
- **Reaproveita o RiskEngine real, não uma versão simplificada pra
  "trades especiais"** (spec §120): mesmo `RiskEngine.evaluate()` das
  Fases 7-10, mesmo Kill Switch, mesma composição "bloqueia antes de
  calcular qualquer sinal" — a única coisa que muda entre Shadow e
  Momentum é como os candles chegam e como a saída é estruturada.

## Como a Fase 11 cumpre a especificação

- **Nenhuma lógica de negócio nova vive na API** (mesmo princípio de
  reaproveitar em vez de duplicar já usado em toda a Fase 10): todo
  endpoint só chama repositórios já existentes e testados (Fases 7-10) —
  a API é uma camada de apresentação, não uma segunda fonte de verdade.
  Se um número aparece errado no dashboard, o bug está no repositório
  (já coberto por testes de integração), não numa lógica duplicada aqui.
  compute_drawdown_pct também é o MESMO import de `aegis.risk.rules`
  usado pelo RiskEngine — o dashboard nunca recalcula drawdown com uma
  fórmula própria que poderia divergir silenciosamente.
- **O único endpoint de escrita exige o mesmo nível de auditoria que o
  caminho por script já exigia** (spec §85): `POST /kill-switch/{id}/reset`
  chama `KillSwitchRepository.reset()` direto — mesma exigência de nota
  não-vazia, mesmo erro 409 (não um "sucesso" silencioso) quando a conta
  não está disparada, mesma linha gravada em `kill_switch_events`. A API
  não abre um atalho mais fraco que o script.
- **"Atrasado" tem um limiar real, não um número inventado** (regra 151):
  `_stale_threshold_seconds` usa 2x a duração do próprio intervalo — um
  candle de 1h não fica "atrasado" 5 minutos depois da virada da hora, mas
  um coletor genuinamente parado É pego. Validado ao vivo: com o
  `run_collector.py` desligado no momento do teste, `/api/system/health`
  corretamente marcou `stale:true` pra quase todos os intervalos e
  `stale:false` só pro candle diário (que ainda estava dentro da janela de
  48h) — o sinal fez exatamente o que deveria, não foi só testado
  offline.
- **Nenhuma credencial ou segredo passa pela API ou pelo frontend**
  (mesmo princípio já seguido em toda config do projeto): as rotas nunca
  expõem `BINANCE_API_KEY`/`BINANCE_API_SECRET` nem qualquer valor de
  `.env` — só estado de conta/posições/trades, que já não é sensível no
  sentido de credencial.

## Como a Fase 10 cumpre a especificação

- **Nunca uma posição sem proteção completa, nem por um segundo** (spec
  §110, aplicado agora no ponto de execução real, não só como precondição
  do RiskEngine): `open_bracket_position` fecha a posição recém-aberta
  imediatamente se QUALQUER perna protetora (stop OU take-profit) falhar
  — testado explicitamente para os dois casos (`test_stop_leg_failure_...`,
  `test_take_profit_leg_failure_...`) e para o caso "e se o flatten também
  falhar" (`test_when_flatten_also_fails_...`, que exige uma mensagem de
  erro dizendo "MANUAL INTERVENTION" — não um erro genérico).
- **Nunca coloca uma ordem duas vezes por causa de um erro de rede**
  (consequência direta de spec §110/120 — uma ordem duplicada é
  exatamente o tipo de erro que preservação de capital deveria evitar):
  `_signed_write` (usado por toda colocação/cancelamento de ordem) nunca
  re-tenta automaticamente — se a resposta se perde, o erro sobe pra quem
  chamou decidir, em vez de reenviar às cegas. Documentado explicitamente
  no docstring do módulo, não um comportamento acidental.
- **Nunca inventa um preço de saída** (regra 151, mesmo princípio já usado
  em Technical/Macro pra "None em vez de um valor fabricado"): se a
  exchange diz que a posição está flat mas nenhuma das duas pernas mostra
  FILLED, `ShadowTradingEngine` marca `RECONCILIATION_FAILED` e para
  (`test_reconciliation_failed_when_neither_leg_shows_filled` prova que
  nenhum trade é gravado nesse caso) — em vez de estimar um preço
  qualquer e fingir que sabe o que aconteceu.
- **A mesma lógica de decisão roda em paper e em shadow, só o
  `ExecutionProvider` muda** (spec §120, textual): `ShadowTradingEngine`
  reusa literalmente a mesma ordem de checagens do `PaperTradingEngine`
  (Kill Switch antes de qualquer sinal, depois Strategy Engine, depois
  RiskEngine) — a única coisa que muda é o que acontece DEPOIS da decisão.
- **Nunca confia no próprio estado local sobre o que a exchange diz**
  (novo nesta fase, mas o mesmo espírito de "nunca fabricar dado" das
  fases anteriores): `_handle_open_position` sempre pergunta
  `get_position` primeiro — o registro local (`shadow_positions`) é
  tratado como um cache a ser reconciliado, nunca como a fonte de verdade.
- **Nunca assume que um endpoint documentado continua sendo o certo**
  (regra 151, na prática mais dura desta sessão inteira): a suíte de testes
  offline não detectou o problema (não tinha como — a assinatura/parsing
  estavam corretos, só o ENDPOINT tinha mudado do lado da Binance). Só o
  smoke test ao vivo contra testnet pegou o erro -4120 real, o que é
  exatamente por que este projeto nunca declara uma fase "pronta" sem
  validação contra dado/API real, não só testes offline.

## Como a Fase 9 cumpre a especificação

- **A única diferença entre backtest e paper é mesmo só o "ExecutionProvider"**
  (spec §120, testado na prática, não só afirmado): `PaperTradingEngine`
  importa `apply_slippage`/`compute_stop`/`compute_take_profit`/`check_exit`/
  `close_position` do MESMO módulo (`aegis.execution.fills`) que
  `BacktestEngine` usa — não uma cópia, não uma reimplementação parecida.
  Qualquer correção futura nessa matemática vale pros dois automaticamente.
- **Kill Switch é checado ANTES de qualquer sinal ser calculado, nunca
  depois** (mesma composição estabelecida no smoke test da Fase 7b):
  `_handle_no_open_position` chama `kill_switch_repo.get_state()` como a
  primeira coisa, antes de `compute_snapshot`/`evaluate_all` — uma conta
  travada nunca gasta ciclo de CPU calculando um sinal que não pode agir
  de qualquer forma, e mais importante, nunca corre o risco de um bug
  futuro inverter a ordem e deixar o sinal "vazar" pra decisão de entrada.
- **Uma posição aberta sempre pode sair, mesmo com o Kill Switch
  travado** (mesmo princípio já validado em BacktestEngine): a checagem de
  saída em `_handle_open_position` roda incondicionalmente, sem checar o
  Kill Switch primeiro — travar o switch impede entradas novas, nunca
  impede fechar o que já está aberto.
- **Nunca processa o mesmo candle fechado duas vezes** (não é regra do
  spec letra por letra, mas é a mesma disciplina de idempotência já usada
  em `record_trade_outcome`/dedup de candles desde a Fase 1/2): o cursor
  em `paper_trading_cursor` é comparado ANTES de qualquer trabalho real
  acontecer — `test_no_new_candle_when_cursor_already_at_latest_close`
  prova isso.
- **Nunca abre uma segunda posição em cima de uma já aberta**
  (spec §110, limite de exposição, aplicado aqui no nível mais básico
  possível): `PaperRepository.open_position` levanta erro em vez de
  sobrescrever silenciosamente — `test_open_position_raises_when_one_already_open`
  prova isso contra Postgres real.
- **Fill de entrada é uma simplificação documentada, não escondida**
  (regra 151): usa o close do último candle fechado + slippage, não um
  preço de ticker ao vivo recém-buscado — ver o docstring de
  `paper/engine.py` e "Riscos" abaixo.

## Como a Fase 7b cumpre a especificação

- **Uma vez disparado, fica disparado até um humano olhar** (spec §23,
  regra textual do "kill switch"): `check_and_maybe_trigger` retorna cedo
  sem reavaliar nada se `is_triggered` já é verdadeiro — a stickiness não é
  incidental, é a primeira coisa que a função checa. Provado, não só
  afirmado: `test_check_and_maybe_trigger_is_sticky_and_does_not_re_log_once_triggered`
  dispara o switch, "recupera" a equity de propósito, reavalia, e confirma
  que continua disparado E que `kill_switch_events` não ganhou uma segunda
  linha — e o smoke test ao vivo reproduziu exatamente isso contra Postgres
  real (`step_3_recovery_does_not_clear_it`).
- **Reset nunca é silencioso nem opcional** (spec §85, auditoria): `reset()`
  exige uma `note` (texto livre, mas obrigatório) e grava um evento RESET —
  não existe um caminho de código que destrava a conta sem deixar rastro de
  quem/por quê. `reset()` também recusa resetar uma conta que não estava
  disparada, ao invés de tratar como no-op — evita mascarar um bug de quem
  chama (ex.: resetar a conta errada) como se fosse um comportamento normal.
- **`DAILY_LOSS_LIMIT` foi deliberadamente excluído dos gatilhos do Kill
  Switch** (ver docstring de `kill_switch.py`): é um mecanismo que já se
  autorregenera todo dia via `reset_daily()` (spec §20) — incluí-lo faria o
  disjuntor disparar em qualquer dia ruim comum, o oposto do que um
  circuit-breaker raro e sério deveria fazer.
- **O mesmo cálculo de gatilho roda ao vivo e em backtest, só o I/O
  muda** (spec §120, o mesmo princípio já usado pelo RiskEngine em
  BacktestEngine): `evaluate_kill_switch_triggers` é chamada tanto por
  `KillSwitchRepository` (persistida) quanto por `BacktestEngine`
  (em memória) — nenhuma das duas reimplementa a lógica de detecção.
- **Um backtest nunca escreve no estado de conta real** (mesmo princípio
  já documentado em `_apply_trade_outcome`): a simulação de Kill Switch
  dentro do `BacktestEngine` é inteiramente em memória — nunca chama
  `KillSwitchRepository`, nunca toca `kill_switch_state`/`kill_switch_events`.

## Como a Fase 8 cumpre a especificação

- **Nenhuma decisão pode enxergar o futuro** (spec §69, a propriedade de
  correção mais fundamental de um backtest): `engine.py` só constrói
  `window = df.iloc[:i+1]` antes de calcular qualquer sinal, e o fill
  acontece na abertura de `i+1`, nunca no fechamento de `i`. Provado, não só
  afirmado — `test_no_lookahead_a_future_price_shock_never_changes_earlier_decisions`
  roda o mesmo engine duas vezes sobre datasets que compartilham um prefixo
  idêntico e divergem violentamente depois (choque de preço 3x + volume
  10x), e verifica que todo trade cujo ciclo de vida inteiro (entrada E
  saída) cabe dentro do prefixo compartilhado sai byte-idêntico entre as
  duas rodadas. Um trade que abre perto da borda e cuja saída legitimamente
  cai na região que diverge é excluído da comparação de propósito — ficar
  exposto ao futuro depois que a posição já está aberta é comportamento
  correto, não vazamento de informação; só a *decisão* de entrada precisa
  ser cega ao futuro.
- **Risco tem a mesma autoridade que vai ter em produção** (spec §120: "a
  única diferença entre paper/live/backtest deve ser o ExecutionProvider"):
  `BacktestEngine` chama `RiskEngine.evaluate()` de verdade (Fase 7), não
  uma versão simplificada — `test_tiny_equity_is_blocked_by_the_risk_engine_not_silently_ignored`
  prova isso explicitamente: com equity de $0.01, todo `TradeProposal`
  nunca atinge `min_notional`, então o backtest termina sem nenhum trade —
  bloqueado pelo mesmo portão que bloquearia uma conta real, não um `if`
  qualquer no loop do backtest.
- **Fees, slippage e latência de execução nunca são ignorados** (spec §71:
  "nunca assumir execução perfeita"): toda entrada/saída passa por
  `_apply_slippage` (pior preço pro lado que está sendo executado, nunca o
  melhor) e `_close_trade` cobra `fees_pct` sobre o notional de entrada E
  saída — um trade nunca fica "de graça" só porque bateu o take-profit.
- **Stop é checado antes de take-profit quando os dois cabem na mesma
  barra** (spec §82: risco vence lucro em caso de conflito, aplicado aqui
  porque dados OHLC não revelam a ordem real dos ticks dentro da barra):
  `_check_exit` sempre testa o stop primeiro — a suposição conservadora,
  documentada como tal no docstring, não escondida.
- **Pesos de confluência são uma hipótese, não uma calibração** (spec §54,
  regra 151 — nunca inventar precisão que não existe): `DEFAULT_WEIGHTS`
  é pesos iguais (1.0) pra cada estratégia, documentado explicitamente no
  módulo como ponto de partida não validado, não como resultado de algum
  processo de otimização que nunca rodou.
- **`sharpe_r`/`sortino_r`/`calmar_r` são variantes documentadas, não a
  fórmula clássica disfarçada** (regra 151 de novo): calculadas sobre a
  série de R múltiplo por trade, não retornos anualizados — o docstring de
  `metrics.py` é explícito sobre a diferença, porque anualizar de verdade
  exigiria assumir uma frequência de barras e um calendário de trading que
  este módulo não tem informação suficiente pra adivinhar direito.
- **Todo resultado de backtest fica auditável, não só logado e
  esquecido** (spec §86 e o princípio geral de rastreabilidade do spec):
  `BacktestRepository.save_run()` grava o snapshot completo da config
  (inclusive `strategy_weights`, se usado) junto com cada trade, numa
  transação só — uma rodada nunca fica meio-persistida (runs sem trades,
  ou vice-versa) se algo falhar no meio.

## Como a Fase 7 cumpre a especificação

- **Risco tem autoridade de veto de verdade, não só de nome** (spec §17,
  §82: "Quando houver conflito: AI vs. RISK ENGINE — RISK ENGINE vence"):
  `RiskEngine.evaluate()` não recebe um "sinal aprovado" pra só carimbar —
  ele calcula tudo do zero (drawdown, streak, sizing, R múltiplo,
  liquidação) e qualquer motivo de bloqueio é suficiente, nenhum outro
  componente pode contornar.
- **Nunca uma posição sem stop** (spec §110, textual): `NO_STOP` é a
  primeira checagem em `evaluate()`, antes de qualquer cálculo de
  tamanho — e `calculate_position_size` também recusa uma distância de
  stop igual a zero (`NO_STOP_DISTANCE`), redundância deliberada.
- **`round_down_to_step` nunca arredonda pra cima** (spec §19: ajustar
  pro step size sem exceder o risco pretendido): usa `Decimal`, não
  `float`, especificamente porque `math.floor(x/step)*step` tem drift
  binário conhecido — testado explicitamente contra o caso clássico
  (`0.1 + 0.2`).
- **Motivos de bloqueio nunca escondem uns aos outros** (facilita
  auditoria de verdade — spec §85): `evaluate()` acumula toda checagem
  aplicável numa lista só, em vez de retornar no primeiro `BLOCK` — o
  smoke test ao vivo mostrou isso na prática (dia 8 da simulação saiu com
  `['DRAWDOWN_LIMIT', 'LOSS_STREAK_HALT']` juntos, não um escondendo o outro).
- **Redução de risco pega o fator mais conservador, nunca soma nem
  multiplica reduções** (evita punir a conta duas vezes pelo mesmo
  problema): se CAUTION (0.75x) e REDUCE_RISK (0.50x) valem ao mesmo
  tempo, o resultado é 0.50x, não 0.375x — testado explicitamente
  (`test_reduced_risk_and_loss_streak_take_the_more_conservative_factor`).
- **Distância de liquidação é uma aproximação documentada como
  aproximação** (spec rule 151: nunca inventar precisão que não existe):
  o docstring de `estimate_liquidation_distance_pct` explica que a
  fórmula real da Binance depende de uma tabela de margem de manutenção
  em camadas (endpoint autenticado `/fapi/v1/leverageBracket`, ainda não
  implementado) — o que existe agora é uma aproximação conservadora para
  margem isolada, não uma cópia do cálculo real da exchange.

## Riscos conhecidos e limitações desta fase

- **Ainda é uma categoria de risco mais alta que Core/Speculative, mesmo
  escopado a pares líquidos.** O piso de liquidez reduz manipulação óbvia
  e risco de delisting, mas não elimina: um movimento de +80% em 24h
  (como o observado ao vivo, `MUBARAKUSDT`) ainda pode reverter
  violentamente. O stop de segurança e o trailing ajudam, mas não é o
  mesmo perfil de risco de operar BTCUSDT.
- **Métrica de momentum é deliberadamente simples** (`abs(price_change_pct)`
  só) — não distingue "subiu 80% de forma sustentada" de "subiu 80% e já
  está caindo". Um z-score de volume vs. baseline histórico seria mais
  sofisticado, mas exigiria dado que este endpoint sozinho não dá —
  documentado como ponto de partida, não uma alegação de sofisticação
  (regra 151).
- **`callback_rate_pct` (2% por padrão) nunca foi calibrado** — é o mesmo
  problema já documentado pros parâmetros de stop/TP das Fases 8-10:
  um valor razoável, não um valor validado contra dado real.
- **Nenhuma entrada real de Momentum foi observada organicamente ao vivo**
  — só a validação manual controlada (`verify_momentum_trading.py`) e o
  poller rodando sem nenhum sinal bater ainda. Mesma limitação honesta já
  documentada pra Paper/Shadow Trading.
- **Reconciliação de posição assume no máximo uma posição aberta por
  símbolo**, mesmo modelo de Backtest/Paper/Shadow — consistente, mas vale
  revisar se/quando o sistema precisar de posições simultâneas.
- **Fees estimadas, não buscadas do endpoint real de comissão** — mesma
  simplificação documentada em Shadow/Paper Trading.

- **Sem autenticação, de propósito — mas isso significa que NUNCA deve
  rodar em nada além de `127.0.0.1`.** `run_dashboard.py` já faz bind só
  em localhost por padrão, mas se alguém mudar isso pra `0.0.0.0` (pra
  acessar de outro dispositivo na rede, por exemplo) o endpoint de reset
  do Kill Switch ficaria exposto sem nenhuma proteção. Documentado
  explicitamente no docstring de `app.py` — antes de expor esse endpoint
  além de localhost, autenticação precisa entrar primeiro.
- **Cobre só uma fração pequena das telas do blueprint original.**
  Overview, Posições, Trades, Backtests e Data Health existem; Market
  Scanner, Alocação de Portfólio multi-bucket, Control Center (fila de
  aprovação — não existe SEMI_AUTO ainda), Calendário Macro, Feed de
  Notícias, Trading Journal e Configurações ficaram de fora — a primeira
  fatia é deliberadamente o que já existe nas Fases 1-10 tornado visível,
  não o mapa de telas completo do blueprint.
- **Sem WebSocket/push — o frontend faz polling a cada 10s.** Simples e
  suficiente pro volume atual (poucos trades, ciclos de 1h), mas não
  escala bem se o volume de dados/atualizações crescer muito; um upgrade
  futuro natural seria Server-Sent Events ou WebSocket, não uma reescrita.
- **`/api/backtests` não filtra por símbolo nem oferece paginação de
  verdade** — só `limit`. Suficiente enquanto o número de rodadas de
  backtest é pequeno; um filtro por símbolo é uma extensão trivial de
  `BacktestRepository.fetch_recent_runs`, que já aceita esse parâmetro.

- **`LIVE_TRADING=true` contra mainnet real ainda não foi testado nem
  habilitado — de propósito.** Toda a Fase 10 foi construída e validada
  exclusivamente contra `BINANCE_TESTNET=true`, com um gate de segurança
  (`run_shadow_trading.py`/`verify_shadow_trading.py` se recusam a rodar
  fora disso). Ligar mainnet é uma decisão separada e deliberada — envolve
  dinheiro real — não uma consequência automática de "a testnet funcionou".
- **Stop/TP são dimensionados a partir do close do último candle fechado,
  não do preço real de fill da entrada.** A Binance exige o `triggerPrice`
  das ordens protetoras junto com (ou logo depois) a entrada, antes do
  preço real de fill ser conhecido. Pra BTCUSDT/ETHUSDT (os pares mais
  líquidos) a diferença tende a ser pequena, mas é uma simplificação real,
  documentada no docstring de `shadow/engine.py`, não uma alegação de
  precisão que não existe (regra 151).
- **Fees são estimadas (`fees_pct` do config), não buscadas do endpoint
  real de comissão da Binance** (`/fapi/v1/userTrades`). Mesma abordagem
  já usada em Backtest/Paper — documentado, não escondido.
- **Nenhuma entrada real de Shadow Trading foi observada organicamente ao
  vivo** — só o teste manual controlado (`verify_shadow_trading.py`, que
  força uma entrada pra validar o pipeline) e o poller rodando ~45s sem
  nenhum sinal real bater. Mesma limitação honesta já documentada pra
  Paper Trading (Fase 9) e liquidações reais (Fase 4b) — rodar por mais
  tempo resolveria isso.
- **`ShadowRepository`/posição são por (conta, símbolo) — uma posição por
  vez, mesmo modelo do Backtest/Paper.** Consistente com as fases
  anteriores, mas vale revisar se/quando o sistema precisar de posições
  simultâneas no mesmo símbolo.
- **`workingType=MARK_PRICE` foi escolhido sem comparação empírica contra
  `CONTRACT_PRICE`.** MARK_PRICE é geralmente a escolha mais segura contra
  manipulação de preço de curto prazo (usado pelo cálculo de liquidação da
  própria Binance), mas essa escolha não foi validada contra as duas
  opções lado a lado — uma calibração futura, não uma alegação de que é
  comprovadamente a melhor.

- **Fill de entrada usa o close do último candle fechado, não um preço de
  ticker ao vivo.** Pra BTCUSDT/ETHUSDT em 1h a diferença tende a ser
  pequena, mas é uma simplificação real (documentada no docstring de
  `paper/engine.py`), não uma cópia do que uma ordem de mercado real
  pagaria. Um futuro Live Trading que envie ordens de verdade vai
  precisar buscar um preço atual de verdade, não usar o close de um candle.
- **Nenhuma entrada real de Paper Trading foi observada ao vivo nesta
  sessão.** O smoke test (`run_paper_trading.py`) rodou contra dado real
  (BTCUSDT/ETHUSDT, 500 candles de 1h reais) e completou um ciclo completo
  de decisão sem erro — mas o resultado foi `NO_SIGNAL` pros dois símbolos,
  porque nenhuma condição de entrada bateu no candle mais atual no momento
  do teste. O caminho de abertura de posição está coberto por 9 testes com
  repositórios falsos + 9 testes de integração real do repositório, mas
  "o RiskEngine aprovou e uma posição foi persistida" ainda não foi visto
  acontecer organicamente contra dado ao vivo — mesma limitação honesta já
  documentada pra liquidações reais na Fase 4b. Rodar o poller por mais
  tempo (horas/dias) resolveria isso.
- **Um único símbolo/intervalo por instância de config, uma posição por
  vez (mesmo modelo do BacktestEngine).** `scripts/run_paper_trading.py`
  contorna isso rodando um `PaperTradingConfig` por símbolo dentro do
  mesmo loop, mas cada símbolo ainda só pode ter uma posição aberta por
  vez — consistente com a Fase 8, mas vale revisar se/quando o sistema
  precisar de posições simultâneas no mesmo símbolo.
- **(Resolvido nesta fase.)** A limitação da Fase 7b "Kill Switch não é
  chamado automaticamente por nada" não é mais totalmente verdade — ver o
  próximo item.
- **O Kill Switch ainda não é chamado por nenhum fluxo de execução com
  dinheiro real (Live Trading).** `check_and_maybe_trigger`/`get_state`
  funcionam de ponta a ponta contra Postgres real e agora também via Paper
  Trading (o primeiro processo real, não script de demonstração, chamando
  isso) — mas a integração com um futuro Live Trading ainda não existe
  porque esse fluxo em si ainda não existe.
- **O Kill Switch é por conta (`account_id`), não um interruptor
  verdadeiramente global entre símbolos/estratégias.** Se o sistema um dia
  operar múltiplas contas/sub-estratégias simultâneas, cada uma tem seu
  próprio Kill Switch independente — não há hoje um "parar tudo em toda a
  operação" de um clique só. Suficiente pro estado atual (uma conta), mas
  vale revisar se/quando isso mudar.
- **Os dois gatilhos (drawdown máximo e loss-streak halt) reusam os
  mesmos thresholds do RiskEngine (`max_drawdown`, `loss_streak_halt_threshold`),
  não têm limites próprios e mais conservadores.** Isso é uma decisão
  deliberada (o Kill Switch deveria disparar exatamente quando o RiskEngine
  já passou a bloquear tudo mesmo, só que de forma permanente em vez de
  reavaliada a cada chamada) — mas significa que não existe hoje uma
  margem extra entre "RiskEngine bloqueia esta proposta" e "a conta inteira
  fica travada". Se isso se provar cedo demais na prática, o ajuste é
  simples (thresholds próprios em `Settings`), mas não foi feito
  preventivamente sem evidência de que é necessário (regra 151).

- **Quatro das cinco estratégias planejadas existem; a quarta agora pode
  ser backtestada, mas nenhum motor real de dinheiro (nem testnet ainda)
  a usa.** `LIQUIDATION_SQUEEZE` segue de fora — não por falta de tempo
  rodando, mas porque a testnet nunca gerou um evento real de liquidação
  forçada pros símbolos que operamos (ver "Métricas da Fase 15b", item
  10). `EVENT_REACTION` foi implementada, testada e — desde o item 13 —
  ligada ao `BacktestEngine` com uma tabela de histórico por
  ponto-no-tempo (`news_asset_status_history`) que impede vazar
  informação do presente pra uma decisão do passado. O que falta agora é
  só a decisão deliberada de ligá-la a Shadow/Paper/Momentum — ainda não
  feita, porque isso exige rodar o backtest de verdade por um tempo com
  dado real acumulando na tabela nova (criada 2026-09-23, ainda sem
  histórico suficiente pra um backtest ter poder estatístico) antes de
  decidir se o sinal vale a pena.
- **Os pesos de confluência (`DEFAULT_WEIGHTS`) nunca foram calibrados
  contra performance real ou historical.** São todos 1.0 — um ponto de
  partida razoável, não uma conclusão. Calibração de verdade precisaria de
  um histórico de backtests rodados em várias janelas de tempo/regimes de
  mercado, que é exatamente o próximo passo natural depois desta fase.
- **`stop_atr_multiple`/`take_profit_r_multiple`/`confluence_threshold`
  (2.0/2.0/30.0 por padrão) são escolhas razoáveis, não validadas.** O
  backtest real contra BTCUSDT/ETHUSDT mostrou uma taxa de acerto abaixo
  de 50% em ambos (esperado pra um R:R ~1:1.6 nominal com custos reais
  descontados) — isso não é "a estratégia não funciona", é exatamente o
  tipo de sinal que a Fase 8 existe pra produzir, pra decisões futuras
  sobre otimização de parâmetros (fora de escopo desta fase).
- **(Resolvido.)** `BacktestResult` agora é persistido em
  `backtest_runs`/`backtest_trades` (spec §86) via `BacktestRepository` —
  mantido aqui como registro histórico da limitação como ela era
  originalmente. O que ainda não existe é persistência da `equity_curve`
  bar-a-bar (só o resumo em `max_drawdown_pct`/`final_equity` fica salvo) —
  reconstruir a curva completa hoje exigiria rodar o backtest de novo, já
  que a configuração é determinística. Fora de escopo por ora: só passa a
  importar de verdade quando existir um dashboard (Fase 10) que precise
  plotá-la sem re-simular.
- **Fills assumem liquidez infinita ao preço de abertura da próxima
  barra (mais slippage fixo).** Não há simulação de profundidade de livro
  nem de fills parciais (spec menciona ambos) — pra um backtest sobre
  candles de 1h em BTCUSDT/ETHUSDT (os pares mais líquidos da Binance
  Futures) isso é uma aproximação razoável, mas ficaria pior em símbolos
  menos líquidos ou timeframes muito curtos.
- **(Resolvido na Fase 15b.)** Simulação Monte Carlo (spec §73) implementada
  em `aegis/backtest/monte_carlo.py` — ver "Métricas da Fase 15b", item 11.

- **(Resolvido na Fase 8.)** Na época da Fase 7 não existia Strategy/Signal
  Engine, então `TradeProposal` só podia ser construído manualmente — o
  smoke test validava `evaluate()` contra preço/precisão real, mas não
  contra um sinal de estratégia real. A Fase 8 construiu o Strategy Engine
  e o `BacktestEngine` chama `RiskEngine.evaluate()` a partir de propostas
  geradas por sinais reais de verdade; mantido aqui como registro
  histórico da limitação como ela era nesta fase.
- **`AccountState` é gerenciado manualmente por enquanto** (via
  `record_trade_outcome`/`reset_daily`/`set_exposure`) — não há nenhum
  processo automático chamando isso ainda, porque não existe Execution
  Engine nem Paper Trading (fases futuras) que gerariam esses eventos
  organicamente. `risk_account_state` e `risk_events` já existem e
  funcionam de ponta a ponta contra Postgres real; só falta algo que os
  alimente sozinho.
- **`estimate_liquidation_distance_pct` é isolated-margin-only e ignora
  a tabela de camadas de margem de manutenção real da Binance** (que
  varia por notional — posições maiores têm margem de manutenção maior).
  Documentado como aproximação conservadora no próprio código, não uma
  lacuna escondida — ver "Como a Fase 7 cumpre a especificação".
- **`COOLDOWN` (3 perdas seguidas) é puramente informacional — não reduz
  risco nem bloqueia.** O spec (§22) só diz "cooldown" sem especificar o
  que isso significa operacionalmente; interpretei como um sinal pra
  observar, não uma ação automática, já que uma pausa por tempo (em vez
  de por perdas) exigiria rastrear timestamps de trade que não existem
  nesta fase. Documentado, não escondido.
- **`update_conflict_statuses` custa uma query por ativo rastreado a cada
  ciclo.** Hoje são 7 ativos (`DEFAULT_ASSET_ALIASES`), então é trivial,
  mas escala linear com o tamanho do universo — se crescer muito, vale
  trocar por uma única query agregada em vez de uma por ativo.
- **Janela de conflito fixa (24h) não distingue "duas fontes
  publicaram simultaneamente" de "uma fonte mudou de opinião 20h depois
  da outra".** Ambos os casos entram na mesma janela — uma versão futura
  poderia pesar recência de forma mais fina, mas isso é otimização, não
  uma lacuna que impede o §41 de funcionar como especificado.
- **Nenhum `NEWS_CONFLICT` real apareceu no smoke test ao vivo.** BTC/ETH/
  SOL saíram todos `CONFIRMED` — a mídia cripto estava, no momento do
  teste, majoritariamente alinhada em tom (positivo/neutro). Isso valida
  o caminho feliz (concordância → `CONFIRMED`) com dado real, mas não
  prova ao vivo o caminho de desempate por fonte primária nem o de
  conflito genuíno — esses dois **foram** validados, só que com dados
  sintéticos em `test_news_conflict.py`, não observados organicamente.
- **Classificador de magnitude quase sempre dizia HIGH — pego no smoke
  test ao vivo, corrigido antes de fechar a fase.** A primeira versão de
  `_HIGH_IMPACT_KEYWORDS` incluía os nomes das próprias agências ("sec",
  "cftc", "federal reserve", "fomc"). Como cada feed É da agência, quase
  toda manchete continha o próprio nome trivialmente — rodando ao vivo
  contra os 3 feeds reais, 100% dos itens do Fed/SEC/CFTC saíam `HIGH`,
  o que não discrimina nada. Corrigido removendo os termos autorreferentes;
  revalidado com uma distribuição real de 25 HIGH / 10 MEDIUM / 20 LOW em
  55 itens — bem mais útil pra triagem.
- **`NEWS_CONFLICT`/`WAIT_FOR_CONFIRMATION` (spec §41) ficaram fora do
  escopo.** Correlacionar sentimento entre fontes diferentes sobre o mesmo
  ativo numa janela de tempo é a próxima peça natural (Fase 6b), mas
  exige dado real de múltiplas fontes fluindo por um tempo pra fazer
  sentido testar direito.
- **Só 3 fontes, todas `PRIMARY_OFFICIAL`, todas em inglês, nenhuma
  cripto-específica.** `TIER_1_FINANCIAL_MEDIA` e `SPECIALIZED_CRYPTO_MEDIA`
  (Reuters, CoinDesk, The Block etc.) são o próximo passo óbvio — não
  entraram agora porque eu não tinha certeza suficiente das URLs exatas
  dos feeds pra verificar ao vivo como fiz com Fed/SEC/CFTC (spec §151).
  Consequência direta observada no smoke test: todo item classificado
  veio com `assets=[]`, porque comunicado de banco central/regulador
  financeiro raramente cita um ticker de cripto pelo nome.
- **`extract_assets` é keyword literal, com falsos positivos conhecidos
  e aceitos.** Tickers curtos que também são palavras comuns em inglês
  (`ADA` dentro de "Canada", por exemplo) podem gerar menção falsa — a
  lista `DEFAULT_ASSET_ALIASES` é pequena e deliberadamente fácil de
  editar por isso mesmo, não uma tentativa de cobertura completa.
- **`confidence` do sentimento mede quantas palavras do léxico bateram,
  não confiança estatística real.** Documentado assim no próprio
  docstring de `SentimentResult` — é um proxy grosseiro ("achei 3+
  palavras-sinal, confio mais nessa classificação"), não uma medida
  calibrada.

- **O calendário de eventos (`EVENT_RISK`, spec §37) ficou fora do
  escopo.** É trabalho futuro que faz mais sentido numa fase própria,
  provavelmente reaproveitando as datas de divulgação que FRED/BLS/BEA já
  carregam implicitamente — ver Próximos Passos.
- **BEA só cobre uma série (PIB real).** Renda pessoal e PCE (spec §35)
  exigiriam confirmar `SeriesCode`/`TableName` novos contra o navegador de
  tabelas do BEA antes de adicionar — não fiz isso ainda por não ter
  certeza suficiente dos códigos de cabeça (mesma régua aplicada ao BLS
  abaixo). `A191RL`/`T10101` é a série mais citada do BEA, verificada
  antes de escrever código.
- **BEA devolve a tabela inteira; filtragem client-side, não server-side.**
  Cada chamada a `GetData` traz todas as linhas de `TableName` (no caso do
  GDP, GDP nominal, PCE, investimento, etc. — dezenas de linhas), e
  `_extract_rows` descarta tudo que não bate com o `SeriesCode` pedido.
  Funciona bem pra 1 série por tabela; se várias séries da mesma tabela
  forem adicionadas, vale reconsiderar buscar a tabela uma vez só e
  distribuir as linhas entre as séries, em vez de uma chamada HTTP por série.
- **Só séries mensais do BLS são suportadas.** `from_bls_payload` só
  reconhece `period` no formato `"M01"`.."M12"` — séries trimestrais
  (`"Q01"`..) ou anuais (`"A01"`) retornam `None` silenciosamente. Nenhuma
  das 3 séries do BLS configuradas é trimestral/anual, então isso não
  afeta nada hoje, mas é um limite real a lembrar antes de adicionar uma.
- **IDs de série do BLS foram escolhidos por alta confiança, não
  verificados um a um contra o buscador oficial do BLS** antes de
  escrever o código (diferente do FRED, onde cada série foi checada em
  fred.stlouisfed.org/series/<id> — spec §151). `LNS14000000`,
  `CES0000000001` e `CUSR0000SA0` são séries extremamente estáveis e
  citadas há décadas, e a validação ao vivo confirmou valores plausíveis
  e coerentes com o FRED — mas o processo ficou mais fraco que o
  recomendado, registrado aqui por honestidade.
- **FRED e BLS concordarem no smoke test não é garantia estrutural.**
  `UNRATE`/`LNS14000000` e `CPIAUCSL`/`CUSR0000SA0` bateram nesta
  validação porque a metodologia de ajuste sazonal coincide para essas
  séries específicas — não há checagem automática de que duas séries
  "equivalentes" vão sempre concordar; é um sinal de saúde a observar,
  não uma regra que o código impõe.
- **Sem `observation_start`/`start_year`, cada provider devolveria a série
  inteira — corrigido antes de virar hábito.** A primeira validação real
  do FRED mostrou `DGS10` trazendo 16.163 observações (histórico desde os
  anos 60) por ciclo, quando `compute_snapshot` só usa as últimas
  `MACRO_HISTORY_LIMIT` (260 por padrão). Corrigido e revalidado (`DGS10`
  caiu para 265 linhas/ciclo) — e a correção virou parte do design do
  `FredProvider`/`BlsProvider` (cada um sabe sua própria janela padrão,
  `history_days`/`history_years`), não uma conta feita no `MacroEngine`.
- **Lookback de YoY é aproximado por frequência**
  (`YOY_LOOKBACK_PERIODS`), não por data de calendário exata — uma série
  diária usa 252 observações como proxy de "1 ano" (dias úteis), não 365
  dias corridos, porque a própria série do FRED já tem buracos
  (feriados/fins de semana). Documentado no código, não escondido.
- **`FRED_API_KEY` e depois `BEA_API_KEY` bloquearam a validação ao vivo
  até o usuário fornecer as chaves** — não é algo que o agente possa
  contornar sozinho (registro exige e-mail/aceite de termos em ambos).
  Depois de fornecidas, os dois smoke tests rodaram limpos — mas isso
  ilustra uma dependência real desta fase que as anteriores não tinham.
  BLS, por outro lado, nunca precisou de nada do usuário.
- **Liquidation Engine não foi validado com um evento real.** Tentei duas
  vezes ao vivo (testnet por ~65s, produção por ~150s incluindo uma
  reconexão por inatividade) e não vi nenhuma liquidação passar pelo
  stream `!forceOrder@arstream` em nenhuma das duas. O que *foi* validado
  ao vivo: a conexão WS abre sem erro em ambos os ambientes, o watchdog de
  staleness dispara e reconecta corretamente quando não chega nada, e o
  parsing (`LiquidationEvent.from_ws_payload`) está testado unitariamente
  contra um payload que segue exatamente o schema documentado da Binance.
  O que **não** foi confirmado ao vivo: se o parsing sobrevive a um evento
  real (nomes de campo, tipos, casos extremos que só aparecem em produção).
  Reavaliar na próxima sessão de trabalho, idealmente numa janela mais
  longa ou observando o mercado num momento de maior volatilidade.
- **Teste de integração inicialmente flaky por proximidade de virada de
  minuto.** `test_fetch_windowed_stats_buckets_and_separates_by_side`
  usava `datetime.now()` puro para os 3 eventos de teste; se a execução
  caísse perto de virar o minuto, `time_bucket()` podia jogar um dos
  eventos pro bucket seguinte e quebrar a asserção de forma não-determinística.
  Pego na segunda rodada completa da suíte (a primeira passou por sorte de
  timing) — corrigido ancorando os timestamps no início do minuto corrente
  em vez de `now()` bruto.
- **`FeatureRepository` só sabia ler `.interval`.** `DerivativesSnapshot`
  usa `.period` (o vocabulário da própria Binance para esses endpoints) —
  a primeira tentativa de gravar um snapshot de derivativos quebrou com
  `AttributeError`. Corrigido com um pequeno resolvedor
  (`_timeframe_of`) que aceita `.interval` ou `.period`. Pego no smoke
  test ao vivo, não em produção — o fake usado nos testes unitários do
  `DerivativesEngine` não exercitava o repositório real.
- **`as_of` de derivativos raramente bate exatamente com o `as_of` técnico
  do mesmo período.** OI/long-short são "polled" a cada
  `DERIVATIVES_POLL_INTERVAL_SECONDS`, enquanto o technical snapshot é por
  fechamento de candle — os timestamps de ambos não são garantidamente
  idênticos, então nem sempre caem na mesma linha de `market_features`
  mesmo tendo o mesmo (symbol, period). Um consumidor futuro
  (ConfluenceEngine) deve fazer um "as-of join" (última linha com
  `as_of <= T`) em vez de esperar uma correspondência exata.
- **`derivatives_hist_limit=30` também limita o lookback do z-score.**
  `funding_zscore` usa uma janela maior (`FUNDING_ZSCORE_LOOKBACK=100`,
  porque `funding_rates` já tem muito histórico desde a Fase 1), mas os
  z-scores de long/short ratio ficam limitados a 30 pontos — aceitável
  agora, mas um `derivatives_hist_limit` maior custaria mais chamadas REST
  por ciclo.
- **Sem circuit breaker de rate limit ainda.** Cada ciclo faz ~4 chamadas
  REST por (symbol, period); com poucos símbolos isso é trivial dentro do
  limite de peso da Binance, mas não há tratamento explícito de 429/418
  além do retry genérico já existente no `BinanceFuturesRestClient` desde
  a Fase 1 — reavaliar se o universo de símbolos crescer muito (Asset
  Scanner, fases futuras).
- **Merge de JSONB nunca remove chave antiga.** Se o nome de um campo
  calculado mudar (ex.: renomear `rsi_14`) ou um campo for removido do
  código, linhas antigas de `market_features` mantêm a chave obsoleta para
  sempre até serem reescritas ou explicitamente limpas — não há
  migração automática de payload JSONB. Descoberto durante a validação
  desta fase (veja Métricas).
- **`book_ticker` pode crescer rápido.** É o stream de maior frequência
  (top-of-book a cada mudança); nesta fase não há retenção/downsampling —
  fica para quando o volume real de símbolos monitorados justificar uma
  política de retenção do Timescale (`add_retention_policy`).
- **Open Interest / Long-Short Ratio ainda não persistem.** O cliente REST
  já sabe buscá-los (Fase 1), mas não há tabela nem loop de polling ainda —
  chega com o Derivatives Engine (Fase 4).
- **Fila em memória, não durável.** Se o processo do coletor morrer com
  itens na fila (não flushados), esses eventos são perdidos — não há
  write-ahead log local. Aceitável nesta fase (dado de mercado é
  re-obtível via REST no próximo bootstrap); reconsiderar se isso vale a
  pena antes da fase de Execução, onde perder um evento é mais sério.
- **Migrations rodam via psycopg2 (sync)**, separado do runtime (asyncpg,
  async) — é o padrão do Alembic; dois drivers no projeto é intencional,
  não acidental.
- **Credenciais de dev no `.env.example`** (`aegis_dev_password`) — só para
  desenvolvimento local; nunca usar em qualquer ambiente exposto.
- **Bootstrap REST só persistia via WS antes desta fase.** Corrigido nesta
  fase: `MarketCollector.bootstrap()` agora também chama `on_event` para
  cada candle histórica vinda do REST (antes, só populava o estado de
  dedup e alimentava o cache em memória do backtester futuro — o banco só
  via candles depois que o WebSocket as transmitia ao vivo). Sem isso, o
  Technical Engine não teria as 200+ candles fechadas que `ema_200`
  precisa por dias após cada reinício. Pego durante a validação desta
  fase, não em produção.
- **VWAP diário, não por sessão de pregão.** Não existe "abertura de
  pregão" em cripto 24/7; o reset é à meia-noite UTC, que é uma convenção,
  não uma verdade de mercado — outros anchors (semanal, desde o último
  funding) podem fazer mais sentido para certas estratégias futuras.
- **Percentil de volatilidade (`volatility_percentile_100`) exige 100
  valores de ATR** (portanto ~114 candles no mínimo) antes de deixar de
  ser `None` — mais uma barreira de qualidade de dado, não um bug.

## Métricas da Fase 3

- 62/62 testes passando (54 offline + 8 integração real).
- Migration `0002` aplicada com sucesso; `market_features` confirmada como
  hypertable via `timescaledb_information.hypertables`.
- Smoke test ao vivo: coletor rodado até acumular 500 candles fechadas por
  (symbol, interval) via REST bootstrap (BTCUSDT/ETHUSDT × 6 intervalos);
  `run_technical_snapshot.py` computou os 12 snapshots sem erro, com RSI
  sempre em [0,100], `ema_200` populado só onde havia histórico suficiente,
  e classificações de estrutura de mercado plausíveis e divergentes entre
  timeframes do mesmo ativo (ex.: 1h em UPTREND com 4h em RANGING).
- Um bug real de merge foi pego pelo teste de integração
  (`test_feature_repository_merges_features_on_conflict`): `to_features_dict()`
  enviava `None` para campos não computados, e o `jsonb ||` interpretava
  isso como "apagar o valor real que outro engine já tinha escrito" —
  corrigido filtrando `None` antes de serializar.

## Métricas da Fase 4

- 90/90 testes passando (74 offline + 16 integração real).
- Migration `0003` aplicada com sucesso; `open_interest` e
  `long_short_ratios` confirmadas como hypertables.
- Smoke test ao vivo: `run_derivatives_engine.py` rodado ~30s contra
  produção da Binance (testnet não serve esses endpoints — ver Riscos);
  0 erros, 10/10 combinações (symbol×period) com `quality=OK`, 30 pontos
  de histórico por série, snapshot completo com todos os campos
  populados (`oi_change_pct`, `oi_acceleration`, `price_oi_pattern`,
  3 long/short ratios + z-scores, `funding_zscore`, `basis`/`basis_pct`)
  e classificação PRICE×OI internamente consistente com os sinais de
  `price_change_pct`/`oi_change_pct` observados.
- Um bug real de integração entre Fase 3 e Fase 4 foi pego pelo smoke
  test ao vivo (`FeatureRepository` não reconhecia `.period`) — corrigido
  e coberto por um novo teste de integração antes de revalidar.

## Métricas da Fase 4b/4c

- 125/125 testes passando (106 offline + 19 integração real).
- Migration `0004` aplicada com sucesso; `liquidations` confirmada como
  hypertable.
- Smoke test ao vivo do Order Book Engine: ~25s no testnet, 0 erros,
  2/2 símbolos com `quality=OK`, spread/microprice/book_pressure
  plausíveis e confirmados via query direta no `market_features`
  (`interval='orderbook'`).
- Smoke test ao vivo do Liquidation Engine: ~65s no testnet + ~150s em
  produção (com reconexão automática por inatividade no meio), 0 erros de
  conexão/protocolo em nenhum dos dois — mas nenhum evento de liquidação
  real chegou a ser observado em nenhuma das tentativas (ver Riscos).
  `quality=NO_DATA` em todos os ciclos é o comportamento correto para
  "sem liquidações na janela", não uma falha.

## Métricas da Fase 5/5b/5c

- 179/179 testes passando (155 offline + 24 integração real).
- Migration `0005` aplicada com sucesso; `macro_series`, `macro_observations`
  e `macro_snapshots` confirmadas — as mesmas três tabelas servem FRED,
  BLS e BEA sem nenhuma alteração de schema entre uma fonte e outra.
- Smoke test ao vivo contra a API real do FRED (chave fornecida pelo
  usuário): 0 erros, 6/6 séries com `quality=OK`, valores plausíveis
  conferidos manualmente (Fed Funds 3.63%, CPI YoY 3.71%, desemprego 4.1%,
  NFCI levemente negativo). Um problema real de volume de dados foi pego
  nessa mesma validação e corrigido antes de fechar a fase (ver Riscos).
- Smoke test ao vivo contra a API real do BLS (sem chave, limites
  padrão): 0 erros, 3/3 séries com `quality=OK` — `LNS14000000`
  (desemprego) = 4.1%, batendo exatamente com o `UNRATE` do FRED;
  `CUSR0000SA0` (CPI) = 334.131 com YoY 3.71%, batendo com o `CPIAUCSL` do
  FRED; `CES0000000001` (NFP) = 159.075 mil empregados, plausível.
- Smoke test ao vivo contra a API real do BEA (chave fornecida pelo
  usuário): 0 erros, `A191RL` (PIB real, % de variação) com
  `quality=OK`, 42 observações trimestrais reais de 2016-2026 persistidas
  (valores entre -0.6% e 4.4%, volatilidade trimestral plausível),
  filtragem por `SeriesCode` confirmada — só linhas de `A191RL` foram
  gravadas, não as outras dezenas de linhas que a tabela T10101 devolve
  junto.

## Métricas da Fase 6 (core + 6b)

- 227/227 testes passando (199 offline + 28 integração real).
- Migrations `0006` e `0007` aplicadas com sucesso; `news_sources`, `news`,
  `news_entities` e `news_asset_status` confirmadas.
- Smoke test ao vivo da Fase 6 (core, 3 fontes): 0 erros, 55 itens reais
  coletados e classificados. Um problema real de classificação foi pego
  nessa validação e corrigido antes de fechar a fase (ver Riscos) — a
  distribuição final de magnitude (25 HIGH / 10 MEDIUM / 20 LOW) é bem
  mais plausível como sinal de triagem do que a primeira tentativa (quase
  100% HIGH).
- Smoke test ao vivo da Fase 6b (9 fontes + conflito): 0 erros, **208
  itens reais** coletados nas 9 fontes (20 Fed, 25 SEC, 10 CFTC, 10
  MarketWatch, 30 CNBC, 25 CoinDesk, 30 Cointelegraph, 38 Decrypt, 20 The
  Block). `update_conflict_statuses` encontrou BTC mencionado por 5 fontes
  distintas, ETH por 4, SOL por 2 — todos `CONFIRMED` com sentimento
  predominante coerente com o teor real das manchetes coletadas
  (ex.: "Bitcoin hits highest level since January at $86,000").

## Métricas da Fase 14

- 499/499 testes passando no total do backend (36 novos desta fase: 7
  ranking + 5 candles + 12 engine com fakes + 7 integração real de
  repositório + 3 trailing bracket + 2 parsing/validação).
- Migration `0014` aplicada com sucesso; `momentum_positions`,
  `momentum_trades`, `momentum_trading_cursor` confirmadas.
- Dois bugs reais encontrados e corrigidos durante a implantação da Fase
  13/14 (nenhum pego por teste offline — os dois só apareceram contra dado
  real):
  1. **Preço de stop/TP não arredondado pro `tick_size` real** — achado
     quando a primeira entrada orgânica do Paper Trading (ETHUSDT) tentou
     a mesma entrada no Shadow Trading e a Binance rejeitou com -1111
     "Precision is over the maximum". Corrigido nos três motores (Shadow,
     Paper, Backtest) com `round_down_to_step`, já testado desde a Fase 7.
  2. **Margem de segurança insuficiente no cálculo manual de quantidade
     dos scripts de validação** — achado ao validar o bracket com trailing
     stop do Momentum: uma margem de 5% acima do `min_notional` foi
     consumida inteiramente pelo arredondamento pro `step_size`
     (ETHUSDT: step de 0,001 a ~$2.750/unidade = ~$2,75 de notional por
     step). Confirmei que o código de PRODUÇÃO (`calculate_position_size`)
     já checava o notional DEPOIS do arredondamento — só os scripts de
     verificação manual tinham a margem curta demais. Corrigido pra 30%
     nos dois scripts (`verify_shadow_trading.py`, `verify_momentum_trading.py`).
- Validação ao vivo completa (`verify_momentum_trading.py`) contra
  testnet: scan real rankeou 5 símbolos líquidos reais por momentum
  (maior movimento observado: MUBARAKUSDT +82,86% em 24h, ainda acima do
  piso de liquidez de $500M), bracket real aberto em ETHUSDT (entrada +
  stop de segurança + `TRAILING_STOP_MARKET` nativo), tudo verificado
  contra o que a exchange reportou, cancelado e desfeito com sucesso.
- Poller contínuo (`run_momentum_trading.py`) rodou sem erro: scan
  completo, 5 candidatos avaliados via candles buscados direto por REST
  (não o coletor), todos `NO_SIGNAL` corretamente (nenhuma condição de
  entrada bateu no momento).
- Testnet conferida limpa (0 posições abertas) depois de toda a
  validação.

## Métricas da Fase 15 (monitoramento ao vivo pós-Fase 14, achados críticos)

Durante o monitoramento contínuo da conta testnet (Shadow + Momentum + Paper
rodando 24/7), o usuário reportou duas divergências reais entre a Binance e o
dashboard, a partir de screenshots. As duas investigações revelaram um total
de três problemas reais — um deles crítico de segurança:

1. **CRÍTICO — leverage real nunca era explicitamente setada antes da
   entrada.** `open_bracket_position`/`open_trailing_bracket_position` nunca
   chamavam o endpoint de "Change Initial Leverage" da Binance. Toda ordem
   real era executada na leverage que a conta/símbolo já tinha por padrão da
   exchange, totalmente desconectada de `config.leverage` (usado apenas
   internamente pelo `RiskEngine` para o cálculo de distância de liquidação).
   Achado ao vivo: SOLUSDT tinha aberto a 20x real na Binance, contra uma
   suposição interna de 3x — invalidando silenciosamente a matemática de
   segurança de liquidação do `RiskEngine.check_liquidation_distance` (ela
   "passava" usando uma leverage fictícia). Corrigido adicionando
   `set_leverage()` ao REST client e chamando-o como primeiro passo,
   obrigatório, nos dois métodos de abertura de bracket — se falhar, aborta
   por completo e nenhuma ordem é enviada (`BracketOpenError(flattened=True)`).
   A posição SOLUSDT pré-existente (aberta antes do fix) foi deixada aberta
   deliberadamente — seus stop/TP reais já cobrem a saída corretamente, e a
   distância de stop de 2,06% dela foi conferida como ainda segura mesmo a
   20x, por coincidência e não por design. 15 testes cobrindo o fix (ordem de
   chamada, falha bloqueia a entrada, falha bloqueia o trailing bracket
   também).
2. **Dashboard mostrando estado desatualizado (stale) após mudança de
   config.** O processo do dashboard carrega `settings`/`pool` uma única vez
   no startup (`app.state`, via `_lifespan`). Quando o universo de símbolos
   foi expandido (bucket Speculative, Fase 13), o processo do dashboard não
   fazia parte do lote de restart — continuou rodando com a lista antiga de
   símbolos em memória, então `/api/positions` nunca via SOLUSDT (real, no
   Shadow e no Paper) nem o card correspondente. Corrigido reiniciando o
   processo do dashboard; confirmado via `curl /api/positions` que agora
   retorna corretamente as posições reais de SOLUSDT em `paper` e `shadow`.
   **Lição de processo**: qualquer mudança em `.env`/`config.py` que afete
   símbolos ou contas exige reiniciar TODOS os processos rodando, dashboard
   incluído — não só os motores de trading.
3. **Ordens algo órfãs no testnet** (2 ordens de stop/TP em BTCUSDT sem
   posição correspondente) — resíduo de uma sessão de debug manual anterior
   à Fase 14 (não um bug de reconciliação ao vivo dos motores). Detectadas
   via `GET /fapi/v1/openAlgoOrders` (sem wrapper ainda — chamado direto via
   `rest._signed_get`) e canceladas com `cancel_algo_order()`. Testnet
   reconferida limpa depois.

**Validação end-to-end do fix de leverage, ao vivo, sem intervenção manual**:
depois do fix redeployado, o Momentum Engine abriu e fechou sozinho um trade
real completo em KERNELUSDT (LONG, entrada 0,0609, saída via
`TRAILING_STOP_MARKET`, net_pnl -0,5, r_multiple -0,099) — confirmando que
`set_leverage` → entrada → stop de segurança → trailing stop → reconciliação
→ registro do trade → checagem do kill switch funcionam de ponta a ponta em
produção real de testnet, não só em teste offline.

## Métricas da Fase 15b (revisão completa pós-lançamento)

Segunda passada de revisão, pedida explicitamente pelo usuário ("revise se
tudo está funcionando corretamente e se algo a melhorar"), com os 9
processos (coletor + 5 motores de dados + Paper + Shadow + Momentum +
Dashboard) já rodando havia horas em produção real de testnet:

1. **Saúde geral: tudo certo.** Nenhum erro nos logs de nenhum processo;
   dashboard servindo 200 OK em todas as rotas; leverage real confirmada via
   `get_position_risk` numa entrada BTCUSDT nova (aberta depois do fix da
   Fase 15) = exatamente 3x, batendo com `config.leverage` — confirma o fix
   de leverage sob uma segunda entrada real, não só a primeira.
2. **Achado — resíduo de dados de teste acumulado no banco real.** As
   suítes de integração (`test_*_repository_integration.py`, testes de
   Kill Switch/Risk) rodam contra o Postgres real e cada execução cria um
   `account_id` aleatório (`test_*`, `testshadow_*`, `testpaper_*`,
   `testmomentum_*`, `testks_*`) que nunca era limpo depois — violando a
   própria regra do projeto de sempre limpar dado de teste/demo do banco.
   150 linhas de resíduo encontradas e removidas de 13 tabelas
   (`*_positions`, `*_trades`, `*_trading_cursor`, `risk_account_state`,
   `kill_switch_state`, `kill_switch_events`, `risk_events`) via
   `DELETE ... WHERE account_id LIKE 'test%'` — seguro porque nenhuma conta
   real (`shadow`/`paper`/`momentum`) começa com `test`. Confirmado que os
   dados reais permaneceram intactos e o dashboard continuou correto depois
   da limpeza. **Não era um bug de produção** (contas de teste nunca
   vazavam pra lógica real, que sempre filtra por `account_id` exato), mas
   é higiene de dado que devia ter sido feita a cada rodada de teste.
3. **Achado e corrigido — trailing stop do Momentum sem preço de ativação,
   funcionando como um stop apertado em vez de "deixar o vencedor correr".**
   Os dois trades reais fechados até aqui (KERNELUSDT e NILUSDT) saíram via
   `TRAILING_STOP` com prejuízo pequeno (r_multiple -0,099 e -0,135) em
   menos de uma hora cada — nenhum teve chance real de desenvolver a alta
   que o usuário quer capturar. Causa raiz:
   `place_trailing_stop_order` nunca recebia `activation_price`, e sem
   esse parâmetro a Binance arma o trailing a partir do preço de mercado
   IMEDIATAMENTE após a entrada — ou seja, qualquer ruído normal de
   mercado logo após abrir a posição já dispara a saída, sem o preço
   precisar cair de verdade. Isso contraria diretamente o objetivo do
   motor (deixado explícito pelo usuário: capturar movimentos fortes e só
   vender quando a força realmente esfriar). Corrigido calculando um
   `activation_price` = preço de entrada ± `trailing_activation_pct` (novo
   campo em `MomentumConfig`, default 1,5%, arredondado pro `tick_size`
   real) e passando pra `open_trailing_bracket_position` →
   `place_trailing_stop_order`. Agora: até o preço se mover 1,5% a favor da
   posição, só o stop de segurança (2×ATR) protege o trade, dando espaço
   pro ruído normal de entrada; só depois disso o trailing arma e começa a
   proteger lucro conforme o preço sobe. Backwards-compatible
   (`activation_price` continua opcional, default `None`, nos métodos do
   REST client e do provider de execução). 3 testes atualizados/novos
   (chamada com ativação explícita, default `None` preservado, engine
   sempre calcula ativação a favor do lado da posição e arredondada pro
   tick). 505/505 testes passando. `MOMENTUM_TRAILING_ACTIVATION_PCT`
   adicionado a `.env`/`.env.example`. Redeployado e confirmado subindo sem
   erro.
   - **Confirmação real, no minuto seguinte ao redeploy**: o motor abriu
     um SHORT real em MUBARAKUSDT logo após o fix subir. Uma queda de
     rede (item 4 abaixo) derrubou o processo com a posição ainda aberta;
     quando o processo voltou ~4 minutos depois, a posição já tinha sido
     fechada pelo trailing stop nativo da Binance (que roda no lado da
     exchange, independente do nosso processo estar de pé) com
     **resultado positivo**: net_pnl +$4,12, r_multiple +0,821 — o
     primeiro trade real do Momentum Engine que efetivamente desenvolveu a
     favor antes de sair, bem diferente dos dois stopouts rápidos de
     antes do fix. Reconciliação na volta funcionou sem intervenção
     manual.
4. **Falha de rede transitória (DNS) derrubou Shadow e Momentum Trading —
   sem resiliência contra isso no loop principal.** Um `getaddrinfo failed`
   passageiro (rede local, não da Binance) durou mais que o orçamento de 4
   tentativas do `BinanceRestError` interno do REST client, e como nada no
   loop principal dos dois scripts capturava esse erro, cada um encerrou
   o processo inteiro (`exit code 1`). Achados importantes: (a) o Paper
   Trading não foi afetado, porque nunca faz chamada de rede real (é
   inteiramente simulado); (b) enquanto os processos ficaram fora do ar,
   as ordens de stop/take-profit/trailing JÁ ABERTAS na Binance continuaram
   protegendo as posições reais normalmente (foi exatamente durante essa
   janela que o trade MUBARAKUSDT acima fechou sozinho, corretamente, do
   lado da exchange) — nenhuma exposição ficou desprotegida. Corrigido
   capturando `BinanceRestError` no nível de cada símbolo/ciclo dentro do
   loop principal de `run_shadow_trading.py` e `run_momentum_trading.py`
   (scan, busca de `symbol_rules`, e avaliação por símbolo) — loga um aviso
   e segue pro próximo símbolo/ciclo em vez de matar o processo. Reiniciados
   os dois com o fix; ambos reconciliaram as posições reais existentes sem
   perda de dado. **Risco residual conhecido, não resolvido nesta rodada**:
   não existe um supervisor de processo (systemd/Task Scheduler/etc.) que
   reinicie esses scripts automaticamente se algo os derrubar por um motivo
   diferente de `BinanceRestError` — hoje, uma falha totalmente inesperada
   ainda exige reinício manual. Vale considerar antes de qualquer operação
   sem supervisão humana constante.
5. **Auditoria de todos os outros processos de longa duração por essa mesma
   classe de falha.** Depois de corrigir Shadow e Momentum, chequei se os
   outros 6 processos (Coletor, Derivatives, Liquidation, Order Book,
   Macro, News) tinham o mesmo problema: Coletor/Liquidation/Order Book já
   são resilientes por natureza (WebSocket com reconexão nativa em
   `ws_client.py`, `except Exception` amplo, sempre reconecta); Macro/News
   já isolam falha por série/feed dentro do próprio `run_once()`
   (`except Exception` por item, não deixa uma fonte ruim derrubar as
   outras). **Só o Derivatives Engine tinha o mesmo buraco** (nenhum
   try/except em volta de `engine.run_once()` no loop principal) —
   corrigido com o mesmo padrão de `BinanceRestError` usado em Shadow/
   Momentum. Redeployado, subiu sem erro.
6. **Achado à parte, mais sério — candle diário sendo marcado como
   "fechado" ~24h antes de realmente fechar, em toda a base.** O health
   check do dashboard (`/api/system/health`) mostrava `age_seconds`
   NEGATIVO pro intervalo "1d" de todo símbolo. Causa raiz:
   `Kline.from_rest_row` (usado por `get_klines`, chamado pelo bootstrap
   e pelo gap-fill do coletor) fixava `is_closed=True` incondicionalmente
   - mas o endpoint REST de klines da Binance, quando consultado sem
   `endTime` (o uso normal de "me dê os últimos N candles"), sempre inclui
   o candle ATUAL, ainda em formação, como última linha. Pra intervalos
   curtos (1m, 5m, 15m, 1h) o dano é invisível - o WebSocket manda o fechamento
   real minutos depois e sobrescreve a linha errada -, mas pro diário o
   candle fica "aberto" por quase 24h, tempo suficiente pra qualquer
   indicador que leia candles fechados no intervalo "1d" estar operando
   sobre um OHLC que ainda vai mudar. É exatamente a mesma classe de bug
   de "lookahead" que já tinha sido corrigida especificamente pro Momentum
   Engine (`klines_to_closed_dataframe`) - só que na fonte genérica
   (`Kline.from_rest_row`), usada por qualquer chamador, incluindo o
   coletor que alimenta a tabela `candles` real. Corrigido calculando
   `is_closed` de verdade (`close_time_ms < now_ms`, com `now_ms`
   sobrescrevível só em teste). 2 testes novos (candle ainda em formação
   fica `is_closed=False`, candle genuinamente passado continua `True`).
   507/507 testes passando. **14 linhas já corrompidas no banco real**
   (7 diárias de cada símbolo + 7 de 1 minuto pegas em trânsito) corrigidas
   direto via `UPDATE candles SET is_closed = false WHERE is_closed = true
   AND close_time > now()` - seguro porque nenhum candle genuinamente
   fechado tem `close_time` no futuro. Coletor reiniciado com o fix;
   `/api/system/health` confirmado mostrando o candle diário de ontem,
   corretamente fechado, `age_seconds` positivo.
7. **Dashboard: seção do Momentum Engine (pedido explícito do usuário -
   feature nova, não um bug).** Até aqui o Momentum Engine só era visível
   via banco/logs, apesar de já estar operando com ordens reais. Adicionado:
   `GET /api/overview` agora inclui a conta "momentum" (equity, drawdown,
   PnL do dia, Kill Switch) - `_TRACKED_ACCOUNTS` passou a ter os 3 nomes
   reais; `GET /api/positions` ganhou a chave `"momentum"`, populada via
   `momentum_repo.get_open_symbols()` (não um loop sobre `settings.symbols`
   como Paper/Shadow, porque o universo de símbolos do Momentum é dinâmico
   - pode ter posição aberta num símbolo que nenhum outro motor conhece,
   ex. MUBARAKUSDT) e sem `take_profit_price` (esse motor não tem alvo
   fixo, só o trailing stop - o card mostra o `momentum_score` no lugar);
   `GET /api/trades?account=momentum` reusa o mesmo formato de Paper/Shadow.
   Frontend (`index.html`) ganhou 2 cards novos: posições abertas e trades
   recentes do Momentum. 2 testes novos em `test_api_routes.py`
   (posições sem `take_profit_price`, trades por conta "momentum").
   508/508 testes passando. Redeployado; `curl` confirmou os 3 endpoints
   servindo dado real, e a página HTML confirmada servindo os elementos
   novos.
   - **Dado real revelado por essa visibilidade nova**: a conta "momentum"
     está com equity $1.024,18 (+2,42% sobre os $1.000 iniciais, sem Kill
     Switch disparado, 0 perdas seguidas), com vários trades fechados
     depois do fix do `trailing_activation_pct` mostrando R-multiples bem
     mais substanciais nos dois sentidos (ex. +0,82R, -0,56R, +0,52R,
     -0,40R) - bem diferente dos primeiros dois stopouts de ~-0,1R de
     antes do fix. Primeira leitura honesta: o fix parece estar deixando
     os trades desenvolverem de verdade antes de sair, tanto pra cima
     quanto pra baixo - ainda é uma amostra pequena pra tirar conclusão
     definitiva sobre a estratégia em si.
8. **Supervisor de processo (`scripts/supervisor.py`), fechando o risco
   residual anotado no item 4.** Até aqui, os 10 processos de longa duração
   (Coletor, Derivatives, Liquidation, Order Book, Macro, News, Paper,
   Shadow, Momentum, Dashboard) eram lançados um a um por mim, sem nada
   reiniciando um que morresse por qualquer motivo fora falha de rede
   transitória (já coberta por item 4/`BinanceRestError`). O supervisor
   lança os 10 como subprocessos e monitora cada um a cada 5s; se um sair
   (`returncode` não-nulo, por qualquer razão), reinicia sozinho usando a
   mesma `BackoffPolicy` (base 1s, teto 60s, com jitter) já usada por todo
   cliente com reconexão neste projeto (`aegis.utils.backoff` -
   reaproveitada em vez de reinventar). Um processo que ficou de pé por
   10+ minutos reseta o próprio backoff, então um crash isolado depois de
   horas rodando bem não herda uma espera longa de um incidente antigo e
   não relacionado. Não mexe em estratégia/risco/execução - só inicia,
   para e reinicia os `scripts/run_*.py` já existentes como processos
   opacos. 3 testes novos (todo script supervisionado existe em disco,
   sem duplicata, os 4 críticos - Shadow/Momentum/Paper/Dashboard - estão
   cobertos). 511/511 testes passando.
   - **Validado ao vivo, não só em teste**: depois de trocar os 10
     processos individuais pelo supervisor único, matei à força
     (`taskkill //F`) o processo real do Momentum Trading pra simular um
     crash inesperado. O supervisor detectou a saída (`exit_code=1`) no
     ciclo de checagem seguinte, esperou ~1,1s de backoff e subiu um novo
     processo sozinho - de volta operando normalmente (novos ciclos reais
     logados) em menos de 10 segundos, sem qualquer intervenção manual.
     Confirma que o gap de "falha inesperada exige reinício manual",
     anotado no item 4, está fechado.
   - **Limitação conhecida, documentada no próprio script**: matar o
     supervisor à força no Windows (`taskkill //F`) não roda sua limpeza
     de desligamento - os filhos continuam rodando independentes em vez de
     serem encerrados junto. Isso só importa pra quem quer derrubar tudo
     de propósito, não afeta o comportamento de auto-restart acima.
9. **Higiene de dado: achado bem maior do que os cleanups anteriores (~1700
   linhas de resíduo em 19 tabelas) + causa raiz corrigida de vez.**
   Investigando se dava pra construir a estratégia LIQUIDATION_SQUEEZE
   agora (ver item 10), reparei que a tabela `liquidations` real só tinha
   linha `TEST...` - nenhum evento genuíno pros 7 símbolos reais, apesar
   do motor rodar continuamente há meses. Isso levou a uma varredura
   completa: TODA tabela com coluna de símbolo/account_id/series_id/
   source_id tinha resíduo de teste (`news`, `macro_series`,
   `macro_observations`, `candles`, `open_interest`, `long_short_ratios`,
   `funding_rates`, `book_ticker`, `assets`, `trades`, `market_features`,
   `backtest_runs`/`backtest_trades`, mais as 13 tabelas de conta já
   limpas antes). Causa: cada teste de integração gera um id único sob um
   prefixo `test`/`TEST` (de propósito, pra nunca colidir com dado real),
   mas nada nunca apagava essas linhas depois - rodar `pytest -q` de novo
   confirmou que o problema volta a cada execução (~2 a dezenas de linhas
   novas por rodada). Corrigido na raiz, não só limpo uma vez:
   `tests/conftest.py` ganhou um hook `pytest_sessionfinish` que varre e
   apaga toda linha com esse prefixo, em toda tabela conhecida, na ordem
   certa de foreign key (`backtest_trades` antes de `backtest_runs`,
   `news`/`news_entities` antes de `news_sources`, `macro_observations`/
   `macro_snapshots` antes de `macro_series`), best-effort (nunca falha a
   suíte se o banco estiver fora do ar). Validado rodando a suíte 3x
   seguidas e confirmando 0 resíduo depois de cada uma. 511/511 passando.
10. **LIQUIDATION_SQUEEZE permanece não implementada - motivo atualizado e
    mais definitivo do que o original.** A razão documentada desde a Fase
    8 era "histórico real mas raso" (motor só smoke-testado). Hoje, depois
    de meses rodando contínuo, a causa real é outra: a testnet da Binance
    Futures aparentemente não gera volume relevante de liquidação forçada
    pros símbolos reais que operamos - o motor, a assinatura do WebSocket
    e o filtro de símbolo estão todos confirmados corretos (a tabela
    ficou genuinamente vazia depois da limpeza do item 9, não por bug).
    Construir uma estratégia sobre um sinal que nunca disparou uma vez
    sequer seria pior do que a razão original pra adiar - fica documentado
    em `aegis/strategy/strategies.py` e adiada até haver sinal real pra
    validar contra (provavelmente só em mainnet). EVENT_REACTION segue
    diferente: macro/news já têm profundidade real (meses de dado
    contínuo) - candidata a ser implementada numa próxima rodada.
11. **Simulação Monte Carlo (spec §73) implementada** -
    `aegis/backtest/monte_carlo.py`. Um único backtest é UMA ordem
    específica de trades sobre UM recorte específico de histórico - a
    curva de equity e o drawdown máximo dependem tanto de sorte (qual
    trade veio primeiro, se as perdas se agruparam) quanto do edge real da
    estratégia. `run_monte_carlo()` reamostra os `net_pnl` reais dos
    trades de um `BacktestResult` (com reposição, via bootstrap - reusa o
    `net_pnl` de cada trade tal como ele realmente saiu, não um R-multiple
    reconstruído, então não assume nada sobre quanto um trade "deveria"
    ter rendido), gera N trajetórias de equity alternativas e reporta a
    distribuição: percentis de equity final (p5/p25/p50/p75/p95),
    percentis de drawdown máximo (mediana/p95/pior caso) e probabilidade
    de ruína (fração das trajetórias em que a equity chegou a zero ou
    menos). Não simula estratégia nem toca dado de mercado - é
    reamostragem estatística pura sobre trades que já existem. 8 testes
    novos (determinístico dado um seed, percentis em ordem, série 100%
    vencedora nunca arruína, uma única perda catastrófica arruína 100%
    das trajetórias). 519/519 testes passando.
    - **Validado ao vivo** (`scripts/run_monte_carlo.py`, reaproveitando o
      mesmo `BacktestEngine` e histórico real de candles do
      `run_backtest.py`): rodou contra BTCUSDT (6 trades reais) e ETHUSDT
      (3 trades reais), 2000 simulações cada, produzindo distribuições
      coerentes (percentis em ordem, drawdown pior-caso > mediana,
      probabilidade de ruína 0% nos dois - nenhum trade da amostra real
      foi catastrófico o bastante). **Ressalva honesta**: com tão poucos
      trades reais na amostra (3-6), a simulação tem pouco poder
      estatístico - reamostrar um punhado pequeno de trades não cria
      informação nova, só mostra a variação possível DENTRO do que já
      existe. Fica mais informativa à medida que mais histórico real de
      backtest se acumular. Não persiste resultado no banco/dashboard
      nesta primeira versão - ferramenta de análise sob demanda, mesmo
      espírito de `demo_risk_engine.py`/`demo_kill_switch.py`.
12. **Estratégia EVENT_REACTION implementada - lógica pura testada e
    validada, mas deliberadamente NÃO ligada a nenhum motor ainda.**
    `evaluate_event_reaction()` em `aegis/strategy/strategies.py` exige
    duas condições independentes, mesma disciplina de todas as outras
    estratégias: (1) veredito `CONFIRMED` do próprio sistema de conflito de
    notícias (spec §41 - nunca opera em `NEWS_CONFLICT` nem com uma única
    fonte não confirmada) e (2) preço já se movendo na mesma direção do
    sentimento da notícia (`roc_12`) - confirma que o mercado já está
    reagindo de verdade, não só que existe uma manchete com o sentimento
    certo. Força escala com `distinct_sources` (mais fontes independentes
    confirmando = mais convicção). `NewsRepository` ganhou
    `get_asset_status()` (faltava um getter - só existia `upsert`). 15
    testes novos (lógica pura + integração real do getter). 532/532
    testes passando.
    - **Por que NÃO foi ligada ao Backtest Engine nem a
      Shadow/Paper/Momentum, apesar de pronta e testada**: `news_asset_status`
      é uma tabela de ESTADO ATUAL (uma linha por asset, sempre
      sobrescrita) - correto pra um motor em tempo real perguntar "o que a
      notícia diz agora", mas o Backtest Engine repete candles PASSADOS; ler
      "o status atual" ao avaliar uma barra histórica vazaria informação de
      hoje pra uma decisão do passado - exatamente o tipo de bug de
      lookahead que este projeto inteiro existe pra evitar. Por isso
      `STRATEGY_EVENT_REACTION` foi deliberadamente deixada fora de
      `ALL_STRATEGY_IDS` (nunca ativa sozinha em nenhum motor real) e o
      `BacktestEngine` não foi tocado. Precisa de uma tabela de histórico
      de status de notícia por ponto-no-tempo (nova migration) antes de
      poder ser validada por backtest honestamente - próximo passo
      explícito, não esquecido.
    - **Validado ao vivo (fora de qualquer motor, como demonstração
      isolada)**: buscou candles reais de BTCUSDT + status real de notícia
      de "BTC" (`CONFIRMED`, positivo, 4 fontes, 21 itens) e rodou
      `evaluate_event_reaction` contra os dois - resultado real:
      `NO_TRADE` porque o preço recente (`roc_12` levemente negativo)
      ainda não confirmava a direção da notícia. Comportamento correto e
      exatamente o esperado - a notícia existe mas o mercado ainda não
      reagiu, então a estratégia espera, como projetada.
13. **A peça que faltava - tabela de histórico por ponto-no-tempo - foi
    implementada, e EVENT_REACTION agora é backtestável de verdade.**
    Migration `0015`: `news_asset_status_history`, append-only, um índice
    em `(asset, computed_at)`. `NewsRepository.upsert_asset_status()`
    continua atualizando `news_asset_status` (estado atual, sem mudança de
    comportamento pra quem já usa) e agora TAMBÉM grava uma linha aqui, na
    mesma transação. Nova função pura `most_recent_status_as_of(history,
    as_of)` em `aegis/news/conflict.py` (o status mais recente com
    `computed_at <= as_of` - inclusivo no instante exato, nunca um status
    calculado depois). `BacktestEngine` ganhou um `news_repo` opcional:
    se `STRATEGY_EVENT_REACTION` estiver em `config.strategy_ids`, busca o
    histórico completo do ativo UMA VEZ antes do loop (não uma query por
    barra - mesmo padrão de "um await no início, loop puro depois" que o
    motor já usava), e cada barra faz sua própria leitura no-lookahead
    usando o `close_time` daquela barra como corte. Pedir EVENT_REACTION
    sem passar `news_repo` falha alto (`ValueError`), não silenciosamente
    ignora a estratégia. Símbolo→ativo é `config.symbol.removesuffix
    ("USDT")` (cobre os 5 ativos que o News Engine rastreia hoje -
    1000PEPEUSDT/NEARUSDT não têm cobertura de notícia ainda, então
    corretamente nunca disparam EVENT_REACTION, não é bug). 10 testes
    novos: `most_recent_status_as_of` (vazio, corte exato inclusivo, nunca
    aceita status futuro, não exige entrada ordenada), integração real do
    histórico (upsert grava nos dois lugares, fetch traz a transição certa
    + a linha imediatamente anterior ao início da janela), e o teste mais
    importante - **regressão de lookahead no BacktestEngine**: planta uma
    mudança de status exatamente no meio do intervalo avaliado e usa um
    spy em `evaluate_all` pra provar, barra a barra, que nenhuma barra
    antes da mudança já vê o novo status e nenhuma barra depois vê o
    antigo. 542/542 testes passando.
    - **Redeployado e validado contra dado real de produção, não só
      sintético**: reiniciei o News Engine (agora sob o supervisor - só
      matei o processo filho, ele voltou sozinho) pra começar a gravar
      histórico de verdade; confirmado gravando (BTC/ETH/SOL/XRP/DOGE/ADA
      todos com linha nova). Rodei um backtest real de BTCUSDT com
      EVENT_REACTION incluído: 0 erros, 0 trades disparados por essa
      estratégia - e o motivo é exatamente correto, não um bug: o último
      candle fechado do backtest (10:59 UTC) é ~2h MAIS VELHO que o status
      de notícia atual (12:51 UTC), então a proteção contra lookahead
      corretamente exclui esse status inteiro da janela - ele ainda não
      "existia" no momento em que a última barra do backtest fechou. Prova
      real, com timestamp real, de que a proteção funciona - não apenas
      no teste sintético.
    - **Um dos 10 testes saiu flaky ao rodar a suíte inteira (2 falhas em
      3 tentativas isoladas)** - `test_fetch_status_history_...`
      comparava um `datetime.now(UTC)` capturado no processo do teste
      contra timestamps `computed_at` gerados pelo servidor Postgres;
      qualquer diferença mínima de relógio entre os dois faz a
      comparação de fronteira falhar de forma não determinística. Corrigido
      lendo o `computed_at` real de volta do banco e usando-o como
      fronteira, em vez de um timestamp client-side - elimina a
      comparação entre dois relógios diferentes por completo. 5/5
      execuções seguidas depois do fix. Lição: qualquer teste que compare
      um timestamp gerado no cliente contra um `computed_at`/`now()`
      gerado no servidor é inerentemente frágil - usar sempre o valor que
      o próprio banco gravou.
14. **Dashboard: Market Scanner — mostra os candidatos do Momentum mesmo
    sem posição aberta.** Até aqui, o scan que o Momentum Engine roda a
    cada `momentum_scan_interval_seconds` (900s por padrão) só aparecia
    nos logs — se nenhum candidato virasse trade, ficava invisível na
    interface. Migration `0016`: `momentum_scan_results`, mesma filosofia
    de `news_asset_status` (uma tabela de estado ATUAL, não histórico) —
    substituída por completo a cada ciclo de scan, nunca acumula símbolo
    obsoleto que caiu do top-N. `MomentumRepository.save_scan_results()`
    (deleta tudo + insere o novo lote, numa transação) chamado direto de
    `run_momentum_trading.py` logo depois de `engine.scan()`. Novo
    endpoint `GET /api/momentum/scan` + card novo no dashboard. 2 testes
    novos de API/repositório.
    - **Achado de design ao escrever o teste de integração**: diferente
      de toda outra tabela deste projeto, `momentum_scan_results` não tem
      `account_id`/prefixo `test` pra isolar — é o único estado global
      "top-N atual", o mesmo que o processo real de produção escreve a
      cada 15 minutos. Testar `save_scan_results` de verdade significa
      apagar TUDO que está lá, inclusive dado real, momentaneamente. Como
      essa tabela é puramente decorativa pro dashboard (a lógica de
      trading do Momentum usa o retorno em memória de `engine.scan()`,
      nunca lê essa tabela de volta - zero influência em decisão real),
      o teste tira uma foto do que está lá de verdade antes, roda sua
      lógica, e restaura essa foto exata no `finally` - sem esse cuidado,
      cada rodada de `pytest` deixaria a interface mostrando dado de
      teste (`TESTABCUSDT` etc.) até o próximo ciclo real do motor
      sobrescrever, 15 minutos depois.
    - **Validado ao vivo contra dado real**: reiniciei Momentum Trading e
      o Dashboard (via o supervisor - só matei os processos filhos
      diretos, voltaram sozinhos). `GET /api/momentum/scan` confirmado
      retornando candidatos reais do scan mais recente, incluindo
      TAKEUSDT com +224,67% em 24h - exatamente o tipo de "moonshot" que
      o usuário perguntou se o sistema conseguiria enxergar. Página HTML
      confirmada servindo o card novo.
15. **Calibração dos pesos de confluência: avaliada e adiada de propósito,
    dado real insuficiente.** Antes de escolher o próximo item do
    backlog, chequei quantos trades reais existem pra calibrar contra:
    Shadow 2, Paper 2, Momentum 17, Backtest 18. Calibrar pesos em cima de
    2-18 pontos não é calibração, é ruído travestido de sinal - o risco
    real é o oposto do que essa feature deveria entregar (mais confiança
    baseada em dado, não menos). Adiado até o volume real crescer o
    suficiente pra um resultado que valha a pena confiar; não é um "não
    fiz", é um "ainda não faz sentido fazer" documentado.
16. **Dashboard: Feed de Notícias + status por ativo.** Dois cards novos:
    status de notícia por ativo (CONFIRMED/WAIT_FOR_CONFIRMATION/
    NEWS_CONFLICT, fontes distintas, sentimento dominante - a mesma leitura
    que EVENT_REACTION usa pra decidir) e feed das notícias mais recentes
    (fonte, título, sentimento). `NewsRepository` ganhou
    `fetch_all_asset_statuses()` (faltava - só existia por ativo
    individual). Dois endpoints novos: `GET /api/news/asset-status`,
    `GET /api/news/recent`. 4 testes novos (2 de API, 1 de repositório, e
    a correção de segurança abaixo). 547/547 testes passando.
    - **Achado de segurança ao escrever o frontend**: título de notícia
      vem de feed RSS externo - a primeira vez que este dashboard
      renderiza texto genuinamente não confiável (todo outro campo já
      exibido vem de dado interno: símbolo, razão de estratégia, valores
      numéricos). Inserir isso direto em `innerHTML` sem escapar seria um
      XSS armazenado real (um feed RSS comprometido ou malicioso poderia
      injetar `<script>` num título). Adicionado `escapeHtml()` e aplicado
      no título e no `source_id` antes de renderizar - simples, mas o tipo
      de coisa fácil de esquecer numa dashboard "local-only, sem
      autenticação" onde a guarda baixa por achar que não importa.
    - **Validado ao vivo**: reiniciei o Dashboard (supervisor trouxe de
      volta sozinho); `GET /api/news/asset-status` e `GET /api/news/recent`
      confirmados retornando dado real (BTC CONFIRMED, 4 fontes; notícias
      reais da CNBC/CoinDesk com sentimento). Página HTML confirmada
      servindo os cards novos.
17. **Dashboard: Trading Journal — timeline única cruzando as 3 contas
    reais.** Os cards de "trades recentes" já existiam por conta; esse é
    diferente: `GET /api/journal` busca os trades de Paper/Shadow/Momentum
    em paralelo (`asyncio.gather`), junta tudo com um campo `account`
    marcando a origem, ordena por `closed_at` decrescente e corta pro
    `limit`. Card novo, largura cheia, mostrando conta/símbolo/lado/saída/
    PnL/R/quando/razões (a lista de `reasons` de cada estratégia que
    contribuiu pra decisão). 1 teste novo de API (contas válidas, ordem
    cronológica correta).
    - **Achado ao vivo, não fabricado**: o teste ao vivo revelou que a
      posição de SOLUSDT que estava aberta há dias em Paper e Shadow
      finalmente bateu o stop hoje (~13:09-13:15 UTC), explicando a queda
      de equity notada nos health checks anteriores - o Trading Journal já
      mostrou isso corretamente na primeira consulta real, exatamente o
      tipo de visibilidade que essa tela deveria dar.
    - **Pesquisa de fontes de "Event Risk" (calendário macro + eventos
      cripto + Twitter/X), a pedido do usuário**: nenhum endpoint foi
      inventado - toda alegação abaixo foi verificada via busca/fetch real,
      não assumida.
      - Calendário macro (FOMC/CPI/NFP): nenhuma API oficial gratuita e
        confiável encontrada. Finnhub tem conta gratuita, mas o endpoint
        `/calendar/economic` especificamente é bloqueado pra quem não paga.
        O calendário `.ics` oficial do BLS existe e é público, mas bloqueia
        ativamente acesso automatizado ("Access Denied" pra bot) - respeitado
        e não contornado. TradingEconomics é pago. Fica sem boa opção
        gratuita por enquanto - pendente de decisão do usuário sobre pagar
        por uma fonte confiável.
      - Eventos específicos de cripto (listagens, unlocks, forks):
        CoinMarketCal tem plano "Personal" gratuito real (7 dias à frente,
        sem histórico) - usuário escolheu implementar essa. Tentei acessar
        a documentação oficial da API (`coinmarketcal.com/en/api` e
        `/en/doc/redoc`) via fetch automatizado e ambas retornaram 403
        (bloqueio de bot); fontes secundárias (wrappers de terceiros no
        GitHub) se contradisseram sobre o formato exato (uma dizia
        paginação `cursor`/`limit`, outra `page`/`max`). Em vez de
        implementar em cima de documentação não verificada e conflitante,
        pedi ao usuário pra se cadastrar (gratuito) e trazer a chave real,
        pra eu implementar e validar contra a API de verdade - mesmo rigor
        usado com FRED/BEA/Binance desde o início do projeto. Usuário
        trouxe a chave; testei contra o host real (`api.coinmarketcal.com`)
        de várias formas (com/sem chave, headers de navegador completos,
        caminho inexistente) e **todo pedido voltou o mesmo `{"message":
        "Forbidden"}` (403) sem exceção** - inclusive pra um caminho
        inventado, o que descarta problema de chave/formato e aponta pra
        bloqueio de rede (WAF bloqueando IP de datacenter/nuvem, de onde
        este ambiente roda) antes mesmo de chegar na aplicação deles. Não
        tentei contornar (trocar IP, mascarar mais headers) - seria evasão
        de detecção. Pedi ao usuário pra testar o mesmo endpoint da rede
        dele (que não deve ter esse bloqueio) e trazer a resposta real.
        **Aguardando confirmação da rede do usuário** - motor ainda não
        implementado, mas a causa raiz do bloqueio já está identificada.
      - Twitter/X (posts de figuras relevantes como o Presidente Trump):
        a API oficial não tem mais camada gratuita desde fev/2026 (virou
        pay-per-use, ~$0,005/leitura, sem plano grátis pra novos
        desenvolvedores). Alternativas "gratuitas" (Nitter, pontes RSS de
        terceiros) violam os termos de uso do X e são historicamente
        instáveis (várias já saíram do ar) - não construí em cima disso,
        de propósito. Efeito prático hoje: declarações relevantes de
        figuras como Trump sobre cripto normalmente já viram matéria nos
        veículos que o News Engine já monitora (CoinDesk, Cointelegraph,
        Decrypt, CNBC) dentro de minutos - cobertura indireta já existe,
        com um pequeno atraso em relação ao tweet original. Caminho pago
        real, se o usuário quiser no futuro: API oficial pay-per-use
        (bem mais barata que o antigo tier Pro de $5.000/mês pra
        monitorar só algumas contas específicas). Nada implementado
        ainda - fica como decisão pendente, revisitada no balanço final.
18. **Event Risk Engine implementado e rodando (CoinMarketCal) - a parte
    de eventos específicos de cripto do "Event Risk", com um desvio real
    de percurso que vale registrar.** O usuário trouxe uma chave que
    validei contra a API real da CoinMarketCap (dados de mercado) por
    engano - funcionou, mas não era o serviço certo (CoinMarketCal, o
    calendário de eventos, é um produto totalmente diferente, nome quase
    idêntico). Confirmado com um teste ao vivo: a chave dava
    `{"error_code":1001,"error_message":"This API Key is invalid."}` na
    CoinMarketCal. O usuário trouxe uma segunda chave dizendo ser a
    correta - também não validava (nem pra mim, nem pra rede do próprio
    usuário, o que descartou bloqueio de IP como causa). A causa real:
    minha suposição de host/rota (`api.coinmarketcal.com/v1/events`)
    estava errada - a **rota certa é `/v2/events`, não `/v1/events`** (o
    site principal `coinmarketcal.com`, incluindo a página de docs, fica
    atrás de proteção Cloudflare que bloqueia acesso automatizado -
    por isso nem eu nem o usuário conseguíamos ver a doc real por fetch
    automatizado). Pedi a documentação exata ao usuário, que trouxe o
    texto oficial - com ela, confirmei ao vivo: `GET
    https://api.coinmarketcal.com/v2/events?coins=bitcoin,ethereum&limit=5`
    com header `x-api-key` retornou HTTP 200 e evento real. A segunda
    chave do usuário (`cmc_live_...`) era a CoinMarketCal certa o tempo
    todo - só a rota estava errada.
    - **Implementado seguindo o padrão do Macro Engine** (mesma
      arquitetura de FRED/BEA/BLS): `aegis/providers/coinmarketcal/
      client.py` (cliente HTTP, retry/backoff, erro claro se faltar
      chave), `aegis/events/models.py` (`CoinMarketCalEvent`, cuidado
      especial com `date_end=""` → `None` e com `is_estimated` - quando
      true, a API avisa que `date` é só um prazo/janela-fim e `displayed_date`
      deve ser usado pra exibição, nunca `date` direto), `aegis/events/
      service.py` (`EventRiskEngine`, não liga em nenhuma decisão de
      trade ainda - mesma disciplina de rollout em etapas do
      EVENT_REACTION), `aegis/db/events_repository.py` (upsert por
      `event_id` - datas de evento mudam com o tempo conforme se
      confirmam, confirmado ao vivo: `updatedAt` diferente de `createdAt`
      em eventos reais), migration `0017` (`coinmarketcal_events`),
      script `scripts/run_event_risk_engine.py`
      (recusa rodar sem `COINMARKETCAL_API_KEY`, mesma política de
      FRED/BEA), endpoint `GET /api/events/upcoming`, card novo no
      dashboard. Escopo deliberadamente restrito a eventos cripto-nativos
      (listagens, mainnet, forks) - eventos macro (FOMC/CPI/NFP) seguem
      sem fonte gratuita boa, não fingido como coberto. 21 testes novos
      (cliente HTTP offline com mock transport, parsing de modelo com
      payload real capturado ao vivo, engine com fakes, integração real
      contra Postgres, config). 564/564 testes passando.
    - **Validado ao vivo de ponta a ponta**: rodei o script standalone
      contra a API e o banco reais - sincronizou 5 eventos reais (RFP da
      Harmonia em Solana, chamada de ecossistema, ativação Alpenglow,
      mainnet Alpenglow Q3 2026, testnets Glamsterdam do Ethereum),
      persistidos corretamente. Adicionado ao supervisor (agora 11
      processos) - precisou reiniciar o supervisor inteiro dessa vez (não
      só matar um filho), já que a lista de scripts é lida uma vez no
      início; confirmado que parar o supervisor via harness mata a árvore
      de processos inteira de verdade (sem órfão), diferente do
      `taskkill //F` direto documentado como limitação no próprio script.
      `GET /api/events/upcoming` confirmado servindo os 4 eventos futuros
      reais (o 5º já passou da data). Dashboard e as 3 contas reais
      conferidas saudáveis depois do reinício completo.
    - **Reforço de segurança consistente**: título/descrição de evento
      também vêm de conteúdo editorial de terceiro (CoinMarketCal, não
      gerado internamente) - `escapeHtml()` aplicado no card novo, mesma
      disciplina do Feed de Notícias.
    - Chave real gravada em `backend/.env` (`COINMARKETCAL_API_KEY`, não
      commitada, mesmo tratamento de toda outra chave neste projeto).
19. **Auditoria completa (a pedido do usuário) contra o blueprint original
    (`aegis-quant-blueprint.html`), não só contra a própria memória desta
    sessão - achado real corrigido: `risk_events` nunca era escrito por
    nenhum motor real.** A tabela/repositório existiam desde a Fase 7
    (`RiskRepository.insert_risk_event`), com teste próprio, mas o único
    chamador em todo o código era `scripts/demo_risk_engine.py` - uma
    demonstração pontual. Shadow, Paper e Momentum sempre chamaram
    `risk_engine.evaluate()` corretamente (a lógica de PASS/BLOCK sempre
    funcionou), mas nunca persistiam a decisão em si - ou seja, toda
    decisão de risco de todo trade real (aberto ou bloqueado) até agora
    nunca deixou rastro de auditoria além do log efêmero do processo.
    Corrigido: os três motores agora chamam `insert_risk_event()` logo
    depois de cada avaliação, PASS ou BLOCKED. 3 fakes de teste
    atualizados + 3 asserts novos confirmando que uma decisão BLOCKED é
    de fato registrada. 564/564 testes passando. Redeployado (reiniciei
    os 3 processos filhos via supervisor); tabela ainda com 0 linhas no
    momento do redeploy - esperado, vai popular no próximo ciclo real em
    que algum símbolo sem posição aberta gerar uma decisão de confluência
    (PASS ou BLOCKED), não antes.
    - **Outros achados da auditoria, cruzados linha a linha com o
      blueprint original** (não corrigidos ainda - documentados pra
      decisão do usuário, ver relatório completo dado no chat): dos 22
      módulos do catálogo original, ~8 nunca foram construídos
      (FlowEngine/CVD, SentimentService/Fear&Greed, RegimeEngine,
      PortfolioCorrelationEngine, AsymmetryEngine, PortfolioEngine de
      alocação Core/Growth/Speculative com rebalanceamento, MLService,
      NotificationService/alertas); o eixo de automação MANUAL/SEMI_AUTO/
      FULL_AUTO com fila de aprovação nunca foi implementado (o sistema
      sempre operou em automação plena implícita); e várias telas da
      interface original (Control Center, Detalhe do Ativo, Correlação,
      Walk-Forward, Growth Simulator, Relatório Diário, Audit Log,
      Alertas) não existem. Em contrapartida, o projeto também construiu
      bastante coisa ALÉM do blueprint original, a pedido explícito do
      usuário ao longo da sessão (bucket Speculative, Momentum "moonshot"
      scanner, EVENT_REACTION, Event Risk/CoinMarketCal, Monte Carlo,
      supervisor de processo).
    - **Verificação cruzada contra a Binance real, ponta a ponta**: as 5
      posições reais abertas no momento (BTCUSDT/ETHUSDT/SOLUSDT/XRPUSDT/
      1000PEPEUSDT, todas SHORT) batem exatamente com o que o Shadow
      rastreia - mesmo símbolo, lado, leverage (3x em todas), e as 10
      ordens algo abertas na exchange batem exatamente com 5 posições ×
      2 pernas (stop+TP) cada, sem órfã.
    - **Achado operacional fora do código**: o repositório git da raiz do
      projeto existe, mas está com **"No commits yet"** depois de toda
      essa sessão - nada do trabalho feito até aqui está versionado.
      `.gitignore` confirmado protegendo `.env`/segredos corretamente
      (`git check-ignore` testado). Recomendado ao usuário fazer o
      primeiro commit - risco real de perda de trabalho enquanto isso não
      acontece, não é uma questão de estilo.
20. **NotificationService implementado (Telegram) - a lacuna de alertas
    identificada na auditoria, fechada.** Escopo deliberadamente restrito
    ao que precisa de humano AGORA, não a todo evento do sistema:
    - **Kill Switch disparado/resetado** - `aegis/notifications/
      telegram.py` (`TelegramNotifier`, best-effort, nunca lança exceção -
      uma falha de envio não pode derrubar a lógica de trading real que
      estava tentando avisar algo) injetado como colaborador opcional em
      `KillSwitchRepository` (`notifier=None` por padrão - dashboard,
      testes e demos nunca precisam saber que isso existe). Dispara
      exatamente uma vez por transição real (o próprio código já garante
      "sticky, não repete TRIGGERED" - reaproveitado, não duplicado).
    - **MANUAL_INTERVENTION_REQUIRED** (bracket + flatten de emergência
      falharam os dois - uma posição real pode estar sem proteção) -
      Shadow e Momentum alertam direto no ponto onde já logavam isso em
      nível CRÍTICO. Paper não tem esse alerta (nunca coloca ordem real).
    - **Supervisor: crash-loop de qualquer processo** - alerta uma vez
      quando uma nova sequência de falhas começa (não a cada tentativa de
      restart, o que viraria spam a cada `_MAX_BACKOFF_SECONDS` pra sempre
      numa falha persistente).
    - 12 testes novos (cliente Telegram isolado com mock transport,
      wiring do Kill Switch com fake notifier confirmando disparo único +
      reset + comportamento seguro sem notifier configurado, crash-loop
      do supervisor). 574/574 testes passando.
    - **Validado ao vivo, ponta a ponta, contra a API real do Telegram**:
      antes de escrever qualquer código, mandei uma mensagem de teste
      direto via curl com o token e chat id reais do usuário - confirmado
      entregue (canal "SNIPER EDGE QUANT"). Depois de implementado e
      redeployado (supervisor inteiro reiniciado, os 11 processos
      confirmados de volta, as 5 posições reais do Shadow reconciliadas
      sem perda), disparei um Kill Switch real numa conta de teste
      descartável (nunca nas contas reais - `check_and_maybe_trigger` +
      `reset` chamados diretamente contra o Postgres real e o Telegram
      real) e confirmei as duas mensagens (disparo + reset) chegando de
      verdade no canal do usuário - não só nos testes offline. Resíduo da
      conta de teste limpo do banco logo em seguida.
    - Chaves reais gravadas em `backend/.env` (`TELEGRAM_BOT_TOKEN`,
      `TELEGRAM_CHAT_ID`), mesmo tratamento de toda outra credencial.

## Métricas da Fase 11

- 456/456 testes passando no total do backend (10 novos desta fase, todos
  de integração real contra Postgres via `httpx.ASGITransport` — sem
  mocks de banco).
- Validação ao vivo completa (`run_dashboard.py` rodando de verdade,
  `curl` contra cada rota): `/` serve o HTML (200), `/api/overview`
  mostrando as contas reais "paper"/"shadow" (equity $1.000,00 cada, Kill
  Switch normal nos dois), `/api/positions` corretamente vazio (nenhuma
  posição aberta no momento), `/api/backtests` mostrando rodadas reais,
  `/docs` (Swagger automático do FastAPI) respondendo 200. Logs do
  servidor conferidos — zero erro em nenhuma requisição.
- `/api/system/health` capturou um sinal real e correto: com o
  `run_collector.py` desligado no momento do teste, marcou `stale:true`
  pra 1m/5m/15m/1h/4h (candles de ontem) e `stale:false` só pro intervalo
  diário (ainda dentro da janela de 48h) — prova que o limiar de atraso
  funciona contra dado real, não só num teste sintético.
- Resíduo de teste (`TESTBT*` em `backtest_runs`, contas `test*` em
  `risk_account_state`/`kill_switch_*`/`paper_*`/`shadow_*`) limpo do
  banco depois da suíte — mesma disciplina de higiene das fases
  anteriores.

## Métricas da Fase 10

- 446/446 testes passando no total do backend (35 novos desta fase: 14
  offline de assinatura/parsing + 9 do BinanceExecutionProvider com REST
  falso + 12 do ShadowTradingEngine com repositórios/provider falsos).
- Migration `0013` aplicada com sucesso; `shadow_positions`,
  `shadow_trades` e `shadow_trading_cursor` confirmadas.
- Dois bugs reais foram encontrados e corrigidos durante a validação ao
  vivo contra a testnet — nenhum dos dois seria pego por testes offline,
  porque nenhum dos dois é um bug de lógica:
  1. **Erro -4120** ("Order type not supported for this endpoint. Please
     use the Algo Order API endpoints instead."): a Binance migrou ordens
     condicionais (STOP_MARKET/TAKE_PROFIT_MARKET/STOP/TAKE_PROFIT/
     TRAILING_STOP_MARKET) do endpoint `/fapi/v1/order` pro dedicado
     `/fapi/v1/algoOrder` em 09/12/2025 — depois do meu conhecimento de
     treinamento. Confirmado via busca na documentação oficial da Binance
     antes de qualquer tentativa de correção às cegas (regra 151: nunca
     inventar um endpoint).
  2. **Erro -1021** ("Timestamp for this request is outside of the
     recvWindow"): o relógio local desta máquina estava ~4,7 segundos
     atrasado do servidor da Binance (medido ao vivo via
     `/fapi/v1/time`), o que quase esgotava sozinho o `recvWindow` padrão
     de 5000ms antes de qualquer latência de rede. Corrigido subindo pra
     10000ms.
  3. Um terceiro problema menor: o payload de resposta de
     `DELETE /fapi/v1/algoOrder` (cancelamento) tem um formato mínimo
     (`algoId`/`clientAlgoId`/`code`/`msg`), diferente do formato completo
     que POST/GET retornam — causava um `KeyError` ao tentar fazer parse
     como se fosse um `AlgoOrderResult` completo. Corrigido pra retornar o
     dict cru nesse caso específico.
- Validação ao vivo completa (`verify_shadow_trading.py`) contra
  BTCUSDT/testnet, depois dos 3 bugs acima corrigidos: entrada a mercado
  real (0,0006 BTC ≈ $52, pouco acima do `min_notional`), stop-loss e
  take-profit reais colocados via Algo Order, posição confirmada aberta
  via `get_position_risk`, ambas as pernas confirmadas `NEW` via
  `get_algo_order`, ambas canceladas com sucesso, posição fechada via
  ordem reduce-only — saldo final conferido ($4.999,77 de $5.000,00
  iniciais, a diferença sendo só o custo real do round-trip de teste).
- Poller contínuo (`run_shadow_trading.py`) rodou ~45s contra dado real
  sem erro: conta "shadow" inicializada com $1.000 real no Postgres,
  avaliou as estratégias reais contra BTCUSDT/ETHUSDT, decidiu `NO_SIGNAL`
  corretamente pros dois (nenhuma condição bateu na janela curta de
  observação) — mesma limitação honesta documentada em Riscos.
- Todo estado de teste (posições, ordens, saldo gasto em fees de teste)
  foi limpo/zerado na testnet ao final da sessão — confirmado via
  `verify_execution_setup.py` mostrando 0 posições abertas.

## Métricas da Fase 9

- 405/405 testes passando no total do backend (34 novos desta fase: 16
  diretos do módulo `execution/fills` extraído + 9 do engine com
  repositórios falsos + 9 de integração real do `PaperRepository`).
- Migration `0012` aplicada com sucesso; `paper_positions`,
  `paper_trades` e `paper_trading_cursor` confirmadas via `\d`.
- Refactor de extração (`aegis.execution.fills`) verificado sem nenhuma
  regressão: os 371 testes que já existiam antes desta fase (incluindo
  todos os 41 de Backtest Engine/metrics/helpers da Fase 8) continuaram
  passando exatamente como antes, byte a byte, depois do `backtest/engine.py`
  virar um repassador fino pro novo módulo compartilhado.
- Smoke test ao vivo (`run_paper_trading.py`, ~75s, Ctrl+C manual):
  conectou na Binance, inicializou a conta "paper" com $1.000 real no
  Postgres, buscou as regras reais de BTCUSDT/ETHUSDT, leu os 500 candles
  de 1h reais já coletados, computou um `TechnicalSnapshot` real, avaliou
  as três estratégias reais contra o candle mais recente de verdade — e
  decidiu corretamente `NO_SIGNAL` pros dois símbolos (nenhuma condição de
  entrada bateu no momento do teste). Estado final conferido direto no
  Postgres: `risk_account_state` com equity=$1.000, `paper_trading_cursor`
  com o `close_time` real do candle mais recente pros dois símbolos —
  tudo batendo com o log estruturado.
- O caminho de ABERTURA de posição (RiskEngine aprova → posição persistida)
  não foi observado ao vivo nesta sessão porque nenhum sinal real bateu no
  candle mais atual durante os ~75s de observação — coberto por testes
  (fake-repo + integração real do repositório), mas não por uma execução
  orgânica completa ao vivo. Ver Riscos.

## Métricas da Fase 7b

- 371/371 testes passando no total do backend (16 novos desta fase: 7
  puros + 7 de integração real contra Postgres + 2 de integração do
  BacktestEngine).
- Migrations `0010` e `0011` aplicadas com sucesso; `kill_switch_state` e
  `kill_switch_events` confirmadas, e as 3 colunas novas em `backtest_runs`
  confirmadas via `\d backtest_runs`.
- Smoke test ao vivo (`demo_kill_switch.py`) contra preço real do BTCUSDT
  ($85.997,70) e conta real no Postgres: disparo por drawdown (perda de
  12% de $1.000 → equity $880, disparo confirmado com motivo
  `MAX_DRAWDOWN_BREACHED`), recuperação de equity até $994 (0,6% de
  drawdown, bem abaixo do limite de 10%) sem destravar o switch, uma
  proposta de trade saudável bloqueada só pelo Kill Switch (o RiskEngine
  nunca chegou a rodar), reset manual com nota obrigatória, e um novo
  disparo por sequência de 8 perdas seguidas depois do reset — trilha de
  auditoria completa (`TRIGGERED → RESET → TRIGGERED`) conferida direto no
  Postgres, batendo exatamente com o log estruturado.
- Um bug real de contagem de parâmetros SQL foi pego pelos testes de
  integração antes de qualquer execução ao vivo: a migration `0011`
  adicionou 3 colunas a `backtest_runs`, mas a query `INSERT` em
  `BacktestRepository.save_run` só foi atualizada pra 41 placeholders em
  vez de 42 — `asyncpg.exceptions.PostgresSyntaxError: INSERT has more
  target columns than expressions`, pego rodando a suíte de testes,
  corrigido antes de qualquer validação ao vivo.
- Re-rodei `run_backtest.py` (BTCUSDT/ETHUSDT reais) depois da mudança de
  schema pra confirmar que nada quebrou: mesmas métricas de antes,
  `kill_switch_triggered=false` corretamente persistido em ambas as
  rodadas (nem BTCUSDT nem ETHUSDT chegaram perto dos limiares em 500
  barras).

## Métricas da Fase 8

- 354/354 testes passando (348 offline/puros de Strategy/Backtest Engine +
  6 de integração real de `BacktestRepository` contra Postgres).
- Migration `0009` aplicada com sucesso; `backtest_runs` e
  `backtest_trades` confirmadas (FK com `ON DELETE CASCADE`).
- Backtest real contra `BTCUSDT/1h` (500 candles reais, `warmup_bars=210`,
  equity inicial $1.000): 7 trades, 2 vitórias/5 perdas, win rate 28,6%,
  profit factor 0,62, PnL líquido -$10,95, R médio -0,31, drawdown máximo
  2,43%, sharpe (por R) -0,23 — persistido como `backtest_runs.id=15`.
- Backtest real contra `ETHUSDT/1h` (mesmas condições): 2 trades, 1
  vitória/1 perda, win rate 50%, profit factor 1,62, PnL líquido +$3,64, R
  médio 0,37, drawdown máximo 1,15% — persistido como
  `backtest_runs.id=16`. Os dois `run_id`s foram conferidos direto no
  Postgres (`total_trades`/`wins`/`losses`/`net_pnl` na tabela batendo
  exatamente com o log estruturado, e a contagem de linhas em
  `backtest_trades` batendo com `total_trades`).
- Nenhum trade, métrica ou log apresentou valor inválido (NaN/inf) ou
  quantidade/notional zero/negativo — os R múltiplos e fees batem com o
  sizing real do `RiskEngine` sobre precisão real da Binance
  (BTCUSDT tick=0.1/step=0.001, ETHUSDT tick=0.01/step=0.001).
- Números acima são resultado de uma única rodada sobre uma janela curta
  (500 barras ≈ 20 dias) e parâmetros default não calibrados — não são uma
  alegação de que as estratégias "funcionam", são a confirmação de que o
  motor de backtest roda ponta a ponta contra dado real sem quebrar e
  produz métricas coerentes (ver Riscos).
- Três bugs de dado sintético foram encontrados e corrigidos durante a
  construção dos testes de integração (dados sem ruído suficiente para
  variância real do RSI/ADX, ciclo senoidal cujas condições de
  TREND_PULLBACK nunca se alinhavam, volume constante zerando o
  volume_zscore) — nenhum deles é um bug do engine, todos eram falhas no
  gerador de dado de teste; documentados em detalhe no histórico da fase.
- Um teste de não-lookahead (`test_no_lookahead_...`) pegou uma falha real
  de design do PRÓPRIO teste (comparava trades pela entrada, não pelo
  ciclo de vida completo) antes de confirmar que o engine em si é seguro —
  ver "Como a Fase 8 cumpre a especificação".

## Métricas da Fase 7

- 285/285 testes passando (248 offline + 37 integração real).
- Migration `0008` aplicada com sucesso; `risk_account_state` e
  `risk_events` confirmadas (a segunda como hypertable).
- Smoke test ao vivo (`demo_risk_engine.py`) contra dados reais da
  Binance: preço real do BTCUSDT ($85.742,50) e regras reais
  (tick=0.1, step=0.0001, minNotional=50). 4 cenários de proposta de
  trade cobrindo trade saudável (PASS, sizing real de 0.0029 BTC), sem
  stop, alavancagem excessiva e liquidação perto demais — todos com o
  resultado esperado. Simulação de 10 dias de uma perda cada mostrou a
  máquina de estados completa: NORMAL→CAUTION (dia 4)→REDUCED_RISK (dia
  6)→HALTED (dia 7), e NONE→COOLDOWN (dia 3)→REDUCE_RISK (dia 5)→HALT
  (dia 8), com `risk_per_trade` efetivo caindo de 0.5% pra 0.25% e os
  eventos de bloqueio combinando motivos corretamente (`DRAWDOWN_LIMIT` +
  `LOSS_STREAK_HALT` juntos a partir do dia 8) — tudo persistido e
  conferido direto no Postgres.
- Um cenário da demonstração estava errado na primeira versão (leverage
  usado não gerava de fato o `LIQUIDATION_TOO_CLOSE` pretendido, e o
  limite de perda diária mascarava a progressão do loss-streak antes de
  ficar visível) — corrigido antes de considerar a fase validada; ver
  Riscos/histórico do smoke test.

## Próximos passos

Fase 14, Fase 13, Fase 11, Fase 10, Fase 9, Fase 8 e Fase 7b estão
completas. Sugestão de ordem para o que falta, mas o usuário decide:

- **Deixar `run_momentum_trading.py` (testnet) rodando por mais tempo**
  pra observar uma entrada real acontecer organicamente — assim como
  Paper/Shadow, isso ainda não foi visto ao vivo.
- **Dashboard: seção pro Momentum Engine** (posições/trades/scan atual) —
  hoje o dashboard só mostra Paper/Shadow.
- **Mais telas do Dashboard**: Market Scanner, Correlação & Exposição,
  Calendário Macro, Feed de Notícias, Trading Journal — natural depois
  que Event Risk/mais estratégias existirem pra ter o que mostrar.
- **Deixar `run_shadow_trading.py` (testnet) rodando por mais tempo**
  (horas/dias) pra observar uma entrada real acontecer organicamente —
  única parte do fluxo ainda não vista ao vivo de forma orgânica (ver
  Métricas/Riscos da Fase 10). Recomendado antes de sequer considerar
  mainnet.
- **Live Trading contra mainnet real** (spec §9/120): uma decisão
  separada e deliberada, não uma continuação automática da Fase 10 — exige
  chaves de API de conta real (não testnet), `LIVE_TRADING=true`, e
  provavelmente um tamanho de posição inicial bem pequeno. Só depois de
  Shadow Trading provar estabilidade por um período mais longo.
- **Deixar `run_paper_trading.py` rodando por mais tempo** (horas/dias)
  pra observar uma entrada real acontecer organicamente — a única parte
  do fluxo de Paper Trading ainda não vista ao vivo (ver Métricas/Riscos
  da Fase 9).
- **Event Risk / calendário de eventos** (spec §37): estados
  NO_EVENT/LOW/MEDIUM/HIGH/EXTREME_IMPACT em torno de divulgações
  agendadas (FOMC, CPI, NFP, GDP) — FRED/BLS/BEA já existem pra construir
  isso em cima. Precisa de uma fonte de calendário de eventos futuros, que
  nenhum provider atual expõe diretamente (FRED/BLS/BEA dão séries
  históricas, não datas de divulgação futuras) — vale decidir a fonte
  antes de começar.
- **Calibração dos pesos de confluência e dos parâmetros de
  stop/TP/threshold** — natural agora que o motor de backtest existe e
  roda contra dado real; hoje são todos valores default não otimizados
  (documentado em Riscos).
- **(Resolvido na Fase 15b, item 13.)** Tabela de histórico de status de
  notícia por ponto-no-tempo — EVENT_REACTION já pode ser backtestada.
  Próximo passo real: deixar rodar até `news_asset_status_history`
  acumular histórico suficiente, então decidir com base no resultado do
  backtest se vale ligar a Shadow/Paper/Momentum.
- **LIQUIDATION_SQUEEZE** segue de fora - não é falta de tempo, é falta de
  sinal real (testnet não gera liquidação forçada pros símbolos que
  operamos, ver Fase 15b item 10); só reconsiderar em mainnet.
- Pendência aberta da Fase 4b: validar `LiquidationEvent.from_ws_payload`
  contra um evento real assim que um aparecer numa sessão futura (rodar
  `run_liquidation_engine.py` por mais tempo, ou observar durante maior
  volatilidade de mercado).
