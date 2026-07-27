"""Offline vectorbt parameter sweeps over the desk layer. [research] extra only.

Never installed in the live image (see the build plan §2 — vectorbt pulls numba, which pins
numpy, and a research dependency must never be able to constrain the process placing orders).
Produces a shortlist of candidate desk parameters; confirmation runs happen in
``wit nautilus.node_backtest`` against the real committee and risk engine, not here.
"""
