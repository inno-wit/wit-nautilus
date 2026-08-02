# wit-nautilus

Wit Hedge Fund's LLM committee + deterministic risk pipeline, ported onto
[NautilusTrader](https://nautilustrader.io/) over Alpaca (execution, paper) +
Polygon (bar data) — originally built over Interactive Brokers, swapped after
IB's paper account turned out to have no US-equity market-data entitlement
(see `docs/whatif-we-used-alpaca-quirky-aurora.md`). A second,
Linux-native build alongside the original MT5 build
([`Wit-Hedge-fund`](https://github.com/inno-wit/Wit-Hedge-fund)), which keeps running
unchanged on its own Windows VPS.

**Status (2026-08-03): further along than the phase list below suggests, but not yet validated.**
Desks (N2), the LLM committee behind a `DecisionProvider` (N3), risk/sizing (N4), and
`WitStrategy`/`FundStateActor` running against a real NautilusTrader `BacktestEngine` (N5) are
done and tested. Docker/Compose on the VPS (N8) is also done. The broker was swapped from the
originally-planned Interactive Brokers to **Alpaca (execution) + Polygon (data)** earlier than
`BUILD_PLAN.md`'s phase list implies — IB's paper account turned out to have no US-equity
market-data entitlement, so N6/N10 effectively merged and landed out of the order described
below. Read `BUILD_PLAN.md`'s phase sections for design rationale, not as a literal checklist
of what's done.

**Not done: any sealed, end-to-end validation of the full desks + committee + risk chain.**
There is no `backtest`/`sweep` CLI yet (only `version, doctor, halt, resume, status,
healthcheck, review, dream, paper, live`), no walk-forward, no holdout. That's the next work,
tracked outside this repo's own phase list.

## Build plan

The full architecture mapping, phase-by-phase plan, open questions, and risks live in
`docs/BUILD_PLAN.md` (copied from the approved planning session —
`whatif-we-used-alpaca-quirky-aurora.md`). Read that before touching `wit/nautilus/` or
`wit/risk/` — it explains *why* the port is shaped the way it is, not just what to type.

Short version: this is not a rewrite of the trading logic. The desks (`markov`/`garch`/
`technicals`), the LLM committee (bull/bear researchers → Portfolio Manager), and the
risk/consensus gate (`sizing.py`) port over close to verbatim — they're pure Python with zero
broker coupling in the source repo. What actually changes is everything MT5-shaped: the broker
adapter, the per-symbol scheduling loop, and where each safety guarantee (kill switch,
`paper_only`, daily-loss breaker) lives inside NautilusTrader's event-driven model.

## Setup

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"     # or ".[alpaca,polygon]" for the live/paper node
cp .env.example .env      # fill in ANTHROPIC_API_KEY + Alpaca/Polygon paper account details
pytest -q
```

## Usage (grows with each phase)

```bash
wit doctor     # config/env sanity today; IB + LLM connectivity checks land in N3/N6
wit version
```

## Project layout

```
wit/
  config.py      typed config + .env loader
  desks/         technicals, markov, garch, market_intel, quant_analyst   (Phase N2)
  committee/      CommitteeDecision, DecisionProvider (live/replay/stub)   (Phase N3)
  risk/          sizing.py, adaptive.py, instrument_spec.py               (Phase N4)
  nautilus/      WitStrategy, FundStateActor, node_backtest/node_live     (Phase N5/N6)
  ops/            journal, reflection, dream, alerts, market_hours, safety (Phase N7)
  research/      vectorbt sweeps — [research] extra only, never in the live image
  cli.py         doctor | backtest | sweep | paper | live | halt | resume | status
docker/          Dockerfile, compose.yml, compose.research.yml            (Phase N8)
data/            journal.jsonl, dream_state.json, KILL_SWITCH, decisions.db (gitignored)
tests/
```

## Safety

Same non-negotiables as the MT5 build, relocated rather than relaxed — see the build plan
§1.4 for exactly where each one lives in the new architecture:

- Kill switch (file-based, `wit halt`/`wit resume`)
- `paper_only` — boot-time assertion against the IB account id prefix (`DU…`), not just a
  config flag
- Daily-loss breaker (3% of start-of-day equity → halts *and latches*)
- The full consensus gate (conviction floor, Markov veto, correlation cap, cooldown, spread
  cap, position caps, margin) — ported unchanged from `sizing.py`

No live-money trading in any phase of the current plan. "Go live" is a separate decision after
≥2 clean weeks on IB paper (Phase N9).
