"""NautilusTrader integration: WitStrategy, FundStateActor, node assembly.

Phase N5 (``strategy.py``, ``actor.py``, ``node_backtest.py``) and Phase N6
(``node_live.py`` — IBKR wiring, paper-only boot assertion). This is where
``Orchestrator.process_symbol`` becomes ``on_bar``, and where every safety guarantee from the
MT5 build's ``SafetyMonitor`` finds its new home. See the build plan §1.2/§1.4.
"""
