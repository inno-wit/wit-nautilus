"""CLI entry point (``wit <command>``).

Phase N1: ``doctor`` and ``version`` only, and ``doctor`` checks config/env presence - no IB
or LLM connectivity yet (that lands in Phase N6/N3). ``backtest``/``sweep``/``paper``/``live``/
``halt``/``resume``/``status``/``reconcile`` are added as their owning phases land; see the
build plan, Phase N7.
"""
from __future__ import annotations

import argparse
import sys

from wit.config import CONFIG


def cmd_version(_: argparse.Namespace) -> int:
    from wit import __version__

    print(f"wit-nautilus {__version__}")
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    """Config/env sanity only for now. Real IB + LLM connectivity checks land in N6/N3."""
    problems: list[str] = []

    if not CONFIG.llm.api_key:
        problems.append("ANTHROPIC_API_KEY is not set (.env)")
    if not CONFIG.llm.deep_model or not CONFIG.llm.quick_model:
        problems.append("WIT_DEEP_MODEL / WIT_QUICK_MODEL are not both set (.env)")
    if not CONFIG.ib.account_id:
        problems.append("TWS_ACCOUNT is not set (.env) - needed for the paper_only boot assertion (Phase N6)")
    if not CONFIG.safety.paper_only:
        problems.append("WIT_PAPER_ONLY is false — this build must stay paper until Phase N9's gate passes")

    print(f"IB target : {CONFIG.ib.host}:{CONFIG.ib.port} (client_id={CONFIG.ib.client_id})")
    print(f"paper_only: {CONFIG.safety.paper_only}")
    print(f"journal   : {CONFIG.journal_path}")

    if problems:
        print("\nProblems:")
        for p in problems:
            print(f"  - {p}")
        return 1

    print("\nConfig OK. (Live IB/LLM connectivity checks are not implemented yet - Phase N3/N6.)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wit")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version").set_defaults(func=cmd_version)
    sub.add_parser("doctor").set_defaults(func=cmd_doctor)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
