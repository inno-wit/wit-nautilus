"""NautilusTrader integration: WitStrategy, FundStateActor, node assembly.

Phase N5 (``strategy.py`` — ``WitStrategy``; ``actor.py`` — ``FundStateActor``) landed against
the real, installed ``nautilus_trader==1.230.0`` (API signatures confirmed via introspection
and the package's own bundled ``examples/strategies/ema_cross_bracket.py``, not guessed), with
a genuine ``BacktestEngine`` smoke test in ``tests/test_strategy_backtest.py`` — not mocked.
``node_backtest.py``/``node_live.py`` (assembling a real `Trader`/`TradingNode` from config, IBKR
wiring, the paper-only boot assertion) are Phase N6. This is where ``Orchestrator.process_symbol``
becomes ``on_bar``/``_on_decision``, and where every safety guarantee from the MT5 build's
``SafetyMonitor`` finds its new home. See the build plan §1.2/§1.4.
"""
