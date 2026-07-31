"""CLI entry point (``wit <command>``).

``doctor``/``version`` landed in Phase N1 (config/env presence only).
Phase N7 adds the rest of the build plan's CLI surface: ``doctor`` grows a
real LLM round-trip check; ``halt``/``resume``/``status`` operate the kill
switch directly (the same file ``FundStateActor`` polls); ``review``/
``dream`` manually fire the Phase N7 ops modules against the journal;
``paper``/``live`` boot the live node against Alpaca (execution) + Polygon
(data) (``live`` needs an explicit ``--i-know``, since it's the command that
actually calls ``node.run()`` unattended against a connected broker).

**``doctor`` DOES check Alpaca/Polygon connectivity** (broker swap addition,
``docs/whatif-we-used-alpaca-quirky-aurora.md``'s own stated verification
requirement) — unlike the IB build, which deferred all live-connection
checking to Phase N9's attended gate. Both calls here are read-only
(``get_account``, one price lookup) and cheap enough to run from a manual
command: Alpaca's REST has no meaningful per-call rate limit for this, and
Polygon's confirmed 5/min free-tier ceiling easily absorbs one ``doctor``
invocation's single call. What's still deliberately NOT added here: a
broker-side ``reconcile`` (Nautilus's own exec-engine reconciliation already
runs automatically on every ``node.run()`` connect —
``LiveExecEngineConfig(reconciliation=True)`` in ``build_config()``) and any
order-submission check (verifying a live paper order end-to-end is Phase 7's
staged validation gate, not something ``doctor`` should do unattended).
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from wit.config import CONFIG

# wit dream's default state path when --state-path is omitted (Phase N7
# audit finding L2) - never CONFIG.dream.state_path, the real production
# file a manual/exploratory invocation must not silently overwrite.
_SCRATCH_DREAM_STATE_PATH = str(Path(CONFIG.journal_path).parent / "dream_state.manual.json")


def cmd_version(_: argparse.Namespace) -> int:
    from wit import __version__

    print(f"wit-nautilus {__version__}")
    return 0


def _check_alpaca(problems: list[str]) -> None:
    """Read-only ``get_account`` round-trip - confirms the key pair
    authenticates AND (independent of ``ALPACA_PAPER``) that the account
    number itself is ``PA``-prefixed, mirroring the live check Phase 0 of the
    broker swap ran by hand before this was wired into ``doctor``."""
    if not (CONFIG.alpaca.api_key and CONFIG.alpaca.secret_key):
        problems.append("ALPACA_API_KEY/ALPACA_SECRET_KEY are not both set (.env) - "
                        "needed for the paper_only boot assertion")
        print("Alpaca    : SKIPPED (key/secret not both set)")
        return
    try:
        from alpaca.trading.client import TradingClient

        client = TradingClient(
            api_key=CONFIG.alpaca.api_key, secret_key=CONFIG.alpaca.secret_key,
            paper=CONFIG.alpaca.paper,
        )
        account = client.get_account()
        is_paper_account = str(account.account_number).startswith("PA")
        print(f"Alpaca    : connected, account={account.account_number} "
             f"status={account.status.value} {'PAPER' if is_paper_account else 'LIVE!!'}")
        if not is_paper_account:
            problems.append(
                f"Alpaca account_number={account.account_number!r} does not start with "
                f"'PA' - this looks like a LIVE account, refusing to treat as paper "
                f"regardless of ALPACA_PAPER"
            )
    except Exception as e:  # noqa: BLE001 - a doctor check must report, never crash
        problems.append(f"Alpaca connectivity check failed: {type(e).__name__}: {e}")
        print(f"Alpaca    : FAILED ({type(e).__name__}: {e})")


def _check_polygon(problems: list[str]) -> None:
    """One read-only price lookup, used only to distinguish free (delayed,
    403 on the real-time endpoint) from paid (real-time, 200) tier - the same
    live signal Phase 0 of the broker swap used to confirm this account is
    free-tier, now surfaced in ``doctor`` instead of a one-off manual check."""
    if not CONFIG.polygon.api_key:
        print("Polygon   : SKIPPED (POLYGON_API_KEY not set)")
        return
    import json
    import urllib.error
    import urllib.request

    try:
        url = f"https://api.polygon.io/v2/last/trade/AAPL?apiKey={CONFIG.polygon.api_key}"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                json.loads(resp.read().decode())
            tier = "REAL-TIME (paid tier)"
        except urllib.error.HTTPError as e:
            if e.code != 403:
                raise
            tier = f"DELAYED ~{CONFIG.polygon.delayed_minutes}min (free tier - confirmed via 403 on the real-time endpoint)"
        print(f"Polygon   : connected, {tier}")
    except Exception as e:  # noqa: BLE001 - a doctor check must report, never crash
        problems.append(f"Polygon connectivity check failed: {type(e).__name__}: {e}")
        print(f"Polygon   : FAILED ({type(e).__name__}: {e})")


def cmd_doctor(_: argparse.Namespace) -> int:
    """Config/env sanity, a real LLM round-trip, and read-only Alpaca/Polygon
    connectivity checks - see this module's docstring for why the latter is
    safe to run unattended (unlike an order-submission check, which isn't)."""
    problems: list[str] = []
    if CONFIG.committee_mode not in ("llm", "rules"):
        problems.append(f"WIT_COMMITTEE_MODE={CONFIG.committee_mode!r} not recognized "
                        f"(expected 'llm' or 'rules') - would raise, not silently run 'llm'")
    rules_mode = CONFIG.committee_mode == "rules"

    if not rules_mode:
        if not CONFIG.llm.api_key:
            problems.append("ANTHROPIC_API_KEY is not set (.env)")
        if not CONFIG.llm.nara_api_key:
            problems.append("NARA_API_KEY is not set (.env)")
        if not CONFIG.llm.deep_model or not CONFIG.llm.quick_model:
            problems.append("WIT_DEEP_MODEL / WIT_QUICK_MODEL are not both set (.env)")
    if not CONFIG.alpaca.paper:
        problems.append("ALPACA_PAPER is false — this build must stay paper until Phase 7's gate passes")
    if not CONFIG.safety.paper_only:
        problems.append("WIT_PAPER_ONLY is false — this build must stay paper until Phase 7's gate passes")

    print(f"paper_only: {CONFIG.safety.paper_only} (alpaca.paper={CONFIG.alpaca.paper})")
    print(f"journal   : {CONFIG.journal_path}")
    print(f"kill sw   : {'ENGAGED' if Path(CONFIG.safety.kill_switch_file).exists() else 'clear'}")
    print(f"committee : {CONFIG.committee_mode}"
         f"{' (no LLM)' if rules_mode else ''}")

    def _ping_llm(label: str, api_key: str, base_url: str, model: str) -> None:
        """One round-trip against one of the committee's two clients (see
        wit/committee/live.py's module docstring for the PM/quick split)."""
        if not (api_key and model):
            print(f"LLM {label:<5}: SKIPPED (see problems below)")
            return
        try:
            import anthropic

            client_kwargs = {"api_key": api_key, "timeout": 30.0}
            if base_url:
                client_kwargs["base_url"] = base_url
            client = anthropic.Anthropic(**client_kwargs)
            msg = client.messages.create(
                model=model, max_tokens=16,
                messages=[{"role": "user", "content": "Reply with the single word: OK"}],
            )
            text = "".join(b.text for b in msg.content if b.type == "text").strip()
            served = (getattr(msg, "model", "") or "").strip()
            print(f"LLM {label:<5}: {model} responded {text!r}"
                 f"{f' (served by {served})' if served and served != model else ''}")
        except Exception as e:  # noqa: BLE001 - a doctor check must report, never crash
            problems.append(f"{label} LLM round-trip failed: {type(e).__name__}: {e}")

    if rules_mode:
        print("LLM       : SKIPPED (rules mode — no LLM committee, see wit/committee/rules.py)")
    else:
        _ping_llm("PM", CONFIG.llm.api_key, CONFIG.llm.base_url, CONFIG.llm.deep_model)
        _ping_llm("quick", CONFIG.llm.nara_api_key, CONFIG.llm.nara_base_url, CONFIG.llm.quick_model)

    _check_alpaca(problems)
    _check_polygon(problems)

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


def cmd_healthcheck(_: argparse.Namespace) -> int:
    """Container liveness check (Phase N8 audit finding I2): unlike `wit
    status`, which always exits 0, this fails if the actor's poll loop
    hasn't touched its heartbeat file recently - the difference between
    "trading, halted, or hung" and "the process itself is dead or wedged".
    Not meant for interactive use; Dockerfile's HEALTHCHECK runs it."""
    from datetime import UTC, datetime

    path = Path(CONFIG.journal_path).parent / "heartbeat"
    if not path.exists():
        print(f"No heartbeat file at {path} - node not started, or heartbeat_path unset.")
        return 1
    try:
        last = datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as e:
        print(f"Could not read heartbeat: {type(e).__name__}: {e}")
        return 1
    age = (datetime.now(UTC) - last).total_seconds()
    # Generous relative to the actor's default 30s poll interval - this
    # only needs to catch a genuinely wedged loop, not fire on ordinary
    # jitter, and the CLI has no way to know a non-default
    # poll_interval_seconds without also loading node-specific config.
    stale_after_seconds = 300
    if age > stale_after_seconds:
        print(f"Heartbeat is {age:.0f}s old (> {stale_after_seconds}s) - poll loop looks wedged.")
        return 1
    print(f"Heartbeat {age:.0f}s old - OK.")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    """Score journaled decisions vs. realized P&L. Reflection reads realized
    P&L straight from the journal's own ``position_closed`` events (Phase N7
    audit finding C1 - no external P&L source needed, and none would be
    correct: Nautilus's ``position_id`` isn't a trade identifier under
    ``OmsType.NETTING``, the only OMS this system runs)."""
    from wit.ops.journal import Journal
    from wit.ops.reflection import Reflection

    journal = Journal(CONFIG.journal_path)
    text = Reflection.format(Reflection(journal).review(days=args.days))
    if args.telegram:
        from wit.ops.alerts import Alerter
        Alerter.from_env().send(text)
    else:
        print(text)
    return 0


def cmd_dream(args: argparse.Namespace) -> int:
    """Manually fire the weekly self-review (also runs automatically on
    FundStateActor's own timer while a node is running - see `wit paper`).
    ``--state-path`` defaults to a scratch file, not the real production
    `data/dream_state.json` (Phase N7 audit finding L2/M1): a manual
    invocation - the kind of thing run to sanity-check the journal - must
    not silently overwrite the fund's live lessons file. Pass
    `--state-path` explicitly (e.g. `CONFIG.dream.state_path`) to update
    production deliberately."""
    from wit.committee.provider import build_committee_provider
    from wit.ops import dream
    from wit.ops.journal import Journal

    try:
        committee = build_committee_provider()
    except ValueError as e:
        # Only "llm" mode can raise here (a half-configured .env) - "rules"
        # mode's RulePolicyProvider() never does.
        print(f"Cannot run the dream cycle: {e}")
        return 1

    journal = Journal(CONFIG.journal_path)
    cfg = replace(CONFIG.dream, state_path=args.state_path or _SCRATCH_DREAM_STATE_PATH)
    state = dream.run(committee, journal, cfg)
    print(dream.format_digest(state))
    print(f"\n(state written to {cfg.state_path})")
    return 0


def _run_node(args: argparse.Namespace) -> int:
    from wit.nautilus import node_live

    print(f"Booting node: Alpaca (execution, paper={CONFIG.alpaca.paper}) + "
         f"Polygon (data) paper_only={CONFIG.safety.paper_only}")
    node_live.run()
    return 0


def cmd_paper(args: argparse.Namespace) -> int:
    """Boot the live paper-trading node and block until stopped (Ctrl+C).
    See `wit/nautilus/node_live.py`'s module docstring for the manual,
    attended gate this needs before the first real run against Alpaca's
    paper API."""
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
    sub.add_parser("healthcheck", help="container liveness check (for Dockerfile's HEALTHCHECK)") \
        .set_defaults(func=cmd_healthcheck)

    p_review = sub.add_parser("review", help="score journaled decisions vs realized P&L")
    p_review.add_argument("--days", type=int, default=7)
    p_review.add_argument("--telegram", action="store_true",
                          help="send the summary to Telegram instead of just printing it")
    p_review.set_defaults(func=cmd_review)

    p_dream = sub.add_parser("dream", help="manually run the weekly self-review")
    p_dream.add_argument("--state-path",
                         help="where to write dream state (defaults to a scratch file, "
                              "never the production data/dream_state.json)")
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
