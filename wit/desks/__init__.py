"""Quant desks: technicals, Markov regime, GARCH vol, market intel, quant_analyst packaging.

Phase N2 — ported near-verbatim from ``Wit-Hedge-fund/engine/signals/``. Pure pandas/numpy/
``arch`` operating on OHLCV DataFrames; zero broker coupling. Gate: byte-identical output to
the MT5 build's desks on the same input fixtures.
"""
