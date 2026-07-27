"""CLI entry point (``wit <command>``).

``doctor``/``version`` landed in Phase N1 (config/env presence only).
Phase N7 adds the rest of the build plan's CLI surface: ``doctor`` grows a
real LLM round-trip check; ``halt``/``resume``/``status`` operate the kill
switch directly (the same file ``FundStateActor`` polls); ``review``/
``dream`` manually fire the Phase N7 ops modules against the journal;
``paper``/``live`` boot the Phase N6 IB node (``live`` needs an explicit
``--i-know``, since it's the command that actually calls ``node.run()``
unattended against a connected broker).

**Deliberately NOT added here**: a live IB connectivity/instrument-resolution
check inside ``doctor``, and a broker-side ``reconcile``. Both require an
actual TWS/Gateway connection to mean anything, which the build plan treats
as crossing from "write and test code" into "operate a connected trading
system" (see ``wit/nautilus/node_live.py``'s module docstring on Phase N6's
gate) - Nautilus's own exec-engine reconciliation already runs automatically
on every ``node.run()`` connect (``LiveExecEngineConfig(reconciliation=True)``
in ``build_config()``). Verifying that live is Phase N9's attended gate, the
same way N6 deferred ``node.run()`` itself rather than guess at it unverified.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from wit.config import CONFIG


def cmd_version(_: argparse.Namespace) -> int:
    from wit import __version__

    print(f"wit-nautilus {__version__}")
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    """Config/env sanity plus a real LLM round-trip. IB connectivity is
    deliberately not checked here - see this module's docstring."""
    problems: list[str] = []

    if not CONFIG.llm.api_key:
        problems.append("ANTHROPIC_API_KEY is not set (.env)")
    if not CONFIG.llm.deep_model or not CONFIG.llm.quick_model:
        problems.append("WIT_DEEP_MODEL / WIT_QUICK_MODEL are not both set (.env)")
    if not CONFIG.ib.account_id:
        problems.append("TWS_ACCOUNT is not set (.env) - needed for the paper_only boot assertion")
    if not CONFIG.safety.paper_only:
        problems.append("WIT_PAPER_ONLY is false — this build must stay paper until Phase N9's gate passes")

    print(f"IB target : {CONFIG.ib.host}:{CONFIG.ib.port} (client_id={CONFIG.ib.client_id})")
    print(f"paper_only: {CONFIG.safety.paper_only}")
    print(f"journal   : {CONFIG.journal_path}")
    print(f"kill sw   : {'ENGAGED' if Path(CONFIG.safety.kill_switch_file).exists() else 'clear'}")

    if CONFIG.llm.api_key and CONFIG.llm.quick_model:
        try:
            import anthropic

            client_kwargs = {"api_key": CONFIG.llm.api_key, "timeout": 30.0}
            if CONFIG.llm.base_url:
                client_kwargs["base_url"] = CONFIG.llm.base_url
            client = anthropic.Anthropic(**client_kwargs)
            msg = client.messages.create(
                model=CONFIG.llm.quick_model, max_tokens=16,
                messages=[{"role": "user", "content": "Reply with the single word: OK"}],
            )
            text = "".join(b.text for b in msg.content if b.type == "text").strip()
            served = (getattr(msg, "model", "") or "").strip()
            print(f"LLM       : {CONFIG.llm.quick_model} responded {text!r}"
                 f"{f' (served by {served})' if served and served != CONFIG.llm.quick_model else ''}")
        except Exception as e:  # noqa: BLE001 - a doctor check must report, never crash
            problems.append(f"LLM round-trip failed: {type(e).__name__}: {e}")
    else:
        print("LLM       : SKIPPED (see problems below)")

    print("IB        : SKIPPED - live connectivity is verified attended, "
         "Phase N9's gate (see this module's docstring)")

    if problems:
        print("\nProblems:")
        for p in problems:
            print(f"  - {p}")
        return 1

    print("\nConfig + LLM OK.")
    return 0


def cmd_halt(args: argparse.Namespace) -> int:
    """Engage the kill switch - the same file FundStateActor polls, so the
    next bar's decision halts within one poll interval (default 30s)."""
    path = Path(CONFIG.safety.kill_switch_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"manual: {args.reason}", encoding="utf-8")
    print(f"Kill switch ENGAGED: {path}\nNo new orders will be placed. "
         f"Release with: wit resume")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    """Release the kill switch."""
    path = Path(CONFIG.safety.kill_switch_file)
    if path.exists():
        path.unlink()
        print("Kill switch released — trading resumes on the next poll.")
    else:
        print("Kill switch was not engaged; nothing to do.")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    """Config, safety state, and watchlist. Live account/position state
    needs a connected node - see this module's docstring."""
    path = Path(CONFIG.safety.kill_switch_file)
    engaged = path.exists()
    print(f"paper_only : {CONFIG.safety.paper_only}")
    print(f"Kill switch: {'ENGAGED' if engaged else 'clear'}"
         f"{f' — {path.read_text(encoding='utf-8').strip()}' if engaged else ''}")
    print(f"Watchlist  : {', '.join(CONFIG.watchlist)}")
    print(f"Journal    : {CONFIG.journal_path}")
    print("\n(account equity/positions require a connected node - `wit paper`/`wit live`)")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    """Score journaled decisions vs. realized P&L. Needs a source of closed-
    position P&L - `--pnl-json` accepts a `{position_id: realized_pnl}` file
    (e.g. exported from `wit status` once live, or hand-built for a dry
    run); without it, every decision is considered but none can be scored,
    since Reflection has no P&L to join against."""
    import json

    from wit.ops.journal import Journal
    from wit.ops.reflection import Reflection

    pnl_by_position: dict[str, float] = {}
    if args.pnl_json:
        pnl_by_position = json.loads(Path(args.pnl_json).read_text(encoding="utf-8"))

    journal = Journal(CONFIG.journal_path)
    text = Reflection.format(Reflection(journal).review(pnl_by_position, days=args.days))
    if args.telegram:
        from wit.ops.alerts import Alerter
        Alerter.from_env().send(text)
    else:
        print(text)
    return 0


class _StaticPosition:
    """Enough of a Nautilus ``Position`` for ``dream.pnl_by_position`` to
    read - used only by ``wit dream``'s manual, no-node invocation."""

    def __init__(self, id: str, ts_closed: int, realized_pnl: float) -> None:
        self.id = id
        self.ts_closed = ts_closed
        self.realized_pnl = realized_pnl


class _StaticCache:
    def __init__(self, pnl_by_position: dict[str, float]) -> None:
        from datetime import UTC, datetime
        now_ns = int(datetime.now(UTC).timestamp() * 1_000_000_000)
        self._positions = [_StaticPosition(pid, now_ns, pnl)
                          for pid, pnl in pnl_by_position.items()]

    def positions_closed(self) -> list[_StaticPosition]:
        return self._positions


def cmd_dream(args: argparse.Namespace) -> int:
    """Manually fire the weekly self-review (also runs automatically on
    FundStateActor's own timer while a node is running - see `wit paper`).
    Needs the same P&L source as `review`."""
    import json

    from wit.committee.live import LiveCommitteeProvider
    from wit.ops import dream
    from wit.ops.journal import Journal

    pnl_by_position: dict[str, float] = {}
    if args.pnl_json:
        pnl_by_position = json.loads(Path(args.pnl_json).read_text(encoding="utf-8"))

    journal = Journal(CONFIG.journal_path)
    committee = LiveCommitteeProvider()
    state = dream.run(committee, _StaticCache(pnl_by_position), journal, CONFIG.dream)
    print(dream.format_digest(state))
    return 0


def _run_node(args: argparse.Namespace) -> int:
    from wit.nautilus import node_live

    print(f"Booting IB node: {CONFIG.ib.host}:{CONFIG.ib.port} "
         f"account={CONFIG.ib.account_id or '(unset)'} paper_only={CONFIG.safety.paper_only}")
    node_live.run()
    return 0


def cmd_paper(args: argparse.Namespace) -> int:
    """Boot the live paper-trading node and block until stopped (Ctrl+C).
    See `wit/nautilus/node_live.py`'s module docstring for the manual,
    attended gate this needs before the first real run against TWS."""
    return _run_node(args)


def cmd_live(args: argparse.Namespace) -> int:
    """Same node as `paper` - `assert_paper_only` refuses a genuinely live
    account/port regardless. `--i-know` is a separate, deliberate
    confirmation for the command that actually calls `node.run()`
    unattended against a connected broker, not a bypass of that lock."""
    if not args.i_know:
        print("Refusing to start: `wit live` needs --i-know to confirm you intend to "
             "run an unattended, connected trading node. Use `wit paper` to make that "
             "explicit, or pass --i-know here.")
        return 1
    return _run_node(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wit")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version").set_defaults(func=cmd_version)
    sub.add_parser("doctor").set_defaults(func=cmd_doctor)

    p_halt = sub.add_parser("halt", help="engage the kill switch")
    p_halt.add_argument("--reason", default="manual")
    p_halt.set_defaults(func=cmd_halt)

    sub.add_parser("resume", help="release the kill switch").set_defaults(func=cmd_resume)
    sub.add_parser("status", help="config, safety state, watchlist").set_defaults(func=cmd_status)

    p_review = sub.add_parser("review", help="score journaled decisions vs realized P&L")
    p_review.add_argument("--days", type=int, default=7)
    p_review.add_argument("--pnl-json", help="path to a {position_id: realized_pnl} JSON file")
    p_review.add_argument("--telegram", action="store_true",
                          help="send the summary to Telegram instead of just printing it")
    p_review.set_defaults(func=cmd_review)

    p_dream = sub.add_parser("dream", help="manually run the weekly self-review")
    p_dream.add_argument("--pnl-json", help="path to a {position_id: realized_pnl} JSON file")
    p_dream.set_defaults(func=cmd_dream)

    sub.add_parser("paper", help="boot the live paper-trading node (blocks until stopped)") \
        .set_defaults(func=cmd_paper)

    p_live = sub.add_parser("live", help="same node as paper - requires --i-know")
    p_live.add_argument("--i-know", action="store_true", dest="i_know")
    p_live.set_defaults(func=cmd_live)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
