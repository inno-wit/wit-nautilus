"""NautilusTrader integration: WitStrategy, FundStateActor, node assembly.

Phase N5 (``strategy.py`` — ``WitStrategy``; ``actor.py`` — ``FundStateActor``) landed against
the real, installed ``nautilus_trader==1.230.0`` (API signatures confirmed via introspection
and the package's own bundled ``examples/strategies/ema_cross_bracket.py``, not guessed), with
a genuine ``BacktestEngine`` smoke test in ``tests/test_strategy_backtest.py`` — not mocked.

Phase N6 (``node_live.py``): assembles a real ``TradingNode`` against Interactive Brokers.
``assert_paper_only()`` — the §1.4 hard lock — runs before ``node.build()``, checking the
*configured* account id prefix/port/``WIT_PAPER_ONLY`` flag, no IB connection required to
enforce it. Strategies/actors are added manually after ``node.build()``, not through Nautilus's
config-driven ``ImportableStrategyConfig`` path, which can't construct ``WitStrategy`` (it needs
a live ``DecisionProvider``/``FundStateActor`` reference, not just serializable config — see
that module's docstring, Phase N5 audit finding F9). This is where ``Orchestrator.process_symbol``
becomes ``on_bar``/``_on_decision``, and where every safety guarantee from the MT5 build's
``SafetyMonitor`` finds its new home. See the build plan §1.2/§1.4.

Actually connecting (``node.run()`` against a live TWS/Gateway session) is a manual, watched
verification step — the build plan's Phase N6 gate — not something a unit test does or this
session ran unattended; see the build plan's Phase N6 section for what to watch for.
"""
