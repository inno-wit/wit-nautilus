"""``AccountSnapshot`` — the bare minimum ``wit.risk.sizing`` needs, in place
of the MT5 build's ``engine.broker.base.AccountInfo``.

New, not a port: ``AccountInfo`` carried MT5-account fields (login, server,
leverage, ``is_demo``) that ``build_plan``/``revalidate_plan`` never actually
read — only ``equity`` and ``margin_free`` do. ``is_demo``'s job (the
``paper_only`` safety lock) moves to a boot-time assertion against the IB
account id prefix (build plan §1.4), not a sizing-time field.

Phase N5 builds this from NautilusTrader's real ``Portfolio``/``Account``
(``self.portfolio.equity(venue)`` / an account's free margin accessor, per
the build plan §1 mapping table); this module stays framework-free so
``wit.risk.sizing`` is testable without a Nautilus kernel, matching the MT5
original's broker-decoupled design.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AccountSnapshot:
    equity: float
    margin_free: float
