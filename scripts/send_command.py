# Operator CLI: enqueue one worker command on the STORE_BACKEND store, or list runs to find run ids.
# Payloads match Worker._dispatch_command in pantry/agents/worker.py and AutoRunner.handle in
# pantry/agents/autorun.py; only the worker processes commands.
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pantry.models import Command, Run  # noqa: E402
from pantry.store.base import Store  # noqa: E402
from pantry.tracing import trace_id_for_run  # noqa: E402

RUN_COMMANDS: tuple[str, ...] = ("APPROVE", "REJECT", "UNDO_RUN")
AUTO_RUN_COMMANDS: tuple[str, ...] = ("START_AUTO_RUN", "PAUSE_AUTO_RUN")
COMMANDS: tuple[str, ...] = ("ADVANCE_CLOCK", *RUN_COMMANDS, "FEEDBACK", *AUTO_RUN_COMMANDS)
LIST_LIMIT: int = 50


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Enqueue a worker command, or list runs with --list.",
        epilog=(
            "examples: ADVANCE_CLOCK | APPROVE --run-id ID | REJECT --run-id ID | UNDO_RUN --run-id ID | "
            "FEEDBACK --trace-id ID --outcome accepted|rejected | START_AUTO_RUN [--max-days N] | "
            "PAUSE_AUTO_RUN | --list"
        ),
    )
    parser.add_argument("command", nargs="?", choices=COMMANDS, help="command type to enqueue")
    parser.add_argument("--run-id", help="run id for APPROVE, REJECT and UNDO_RUN")
    parser.add_argument("--trace-id", help="trace id for FEEDBACK")
    parser.add_argument("--outcome", choices=["accepted", "rejected"], help="feedback outcome for FEEDBACK")
    parser.add_argument("--days", type=int, default=1, help="days to advance for ADVANCE_CLOCK (default 1)")
    parser.add_argument(
        "--max-days", type=int, default=None,
        help="day limit for START_AUTO_RUN (default: the worker's AutoRunner default)",
    )
    parser.add_argument("--backend", choices=["mongo", "json"], help="override STORE_BACKEND")
    parser.add_argument("--list", action="store_true", help="print auto-run state and runs, then exit")
    parser.add_argument(
        "--wait", type=float, default=0.0, metavar="SECONDS",
        help="poll until the worker marks the command done or failed (default: do not wait)",
    )
    return parser


def build_payload(args: argparse.Namespace, parser: argparse.ArgumentParser) -> dict[str, Any]:
    """Return the payload the worker expects for args.command; parser.error exits with status 2."""
    if args.command == "ADVANCE_CLOCK":
        if args.days < 1:
            parser.error("--days must be a positive integer")
        return {"days": args.days}
    if args.command in RUN_COMMANDS:
        if not args.run_id:
            parser.error(f"{args.command} requires --run-id")
        return {"run_id": args.run_id}
    if args.command == "FEEDBACK":
        if not args.trace_id or not args.outcome:
            parser.error("FEEDBACK requires --trace-id and --outcome accepted|rejected")
        return {"trace_id": args.trace_id, "outcome": args.outcome, "source": "user"}
    if args.command == "START_AUTO_RUN":
        if args.max_days is None:
            return {}
        if args.max_days < 1:
            parser.error("--max-days must be a positive integer")
        return {"max_days": args.max_days}
    if args.command == "PAUSE_AUTO_RUN":
        return {}
    parser.error("a command or --list is required")
    raise AssertionError("unreachable")


def is_pending(run: Run) -> bool:
    return run.state == "PENDING_CONFIRMATION" or run.pending_confirmation is not None


def print_auto_run(store: Store) -> None:
    state = store.get_auto_run()
    print(
        f"auto_run mode={state.mode} days_completed={state.days_completed} "
        f"max_days={state.max_days} pause_reason={state.pause_reason or '-'}"
    )


def print_runs(store: Store) -> None:
    runs = store.list_runs(limit=LIST_LIMIT)
    print(f"today={store.get_today().isoformat()} runs={len(runs)} (newest first)")
    for run in runs:
        marker = "  <-- PENDING_CONFIRMATION" if is_pending(run) else ""
        rule = f" rule={run.pending_confirmation.rule}" if run.pending_confirmation else ""
        committed = " committed" if run.committed else ""
        print(
            f"  {run.run_id}  state={run.state}{committed}  {run.trigger}:{run.item_name}"
            f"  trace={trace_id_for_run(run.run_id)}{rule}{marker}"
        )


def check_target(store: Store, command_type: str, payload: dict[str, Any]) -> str | None:
    """Return an error message if the payload names a run or trace that does not exist."""
    if command_type in RUN_COMMANDS and store.get_run(payload["run_id"]) is None:
        return f"unknown run id {payload['run_id']!r}; use --list to see runs"
    if command_type == "FEEDBACK" and store.get_trace(payload["trace_id"]) is None:
        return f"unknown trace id {payload['trace_id']!r}; use --list to see trace ids"
    return None


def wait_for(store: Store, command: Command, seconds: float) -> Command:
    deadline = time.monotonic() + seconds
    current = command
    while current.status == "pending" and time.monotonic() < deadline:
        time.sleep(0.5)
        current = store.get_command(command.command_id) or current
    return current


def main(argv: list[str] | None = None, store: Store | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if store is None:
        from pantry.config import get_settings
        from pantry.store.factory import make_store

        settings = get_settings()
        store = make_store(args.backend or settings.store_backend, settings)

    if args.list:
        print_auto_run(store)
        print_runs(store)
        return 0

    payload = build_payload(args, parser)
    problem = check_target(store, args.command, payload)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2

    command = store.enqueue_command(
        Command(command_id=f"cmd:{uuid4().hex}", type=args.command, payload=payload)
    )
    if args.wait > 0:
        command = wait_for(store, command, args.wait)

    print(f"command_id={command.command_id}")
    print(f"type={command.type} payload={payload}")
    print(f"status={command.status}")
    if command.error:
        print(f"error={command.error}")
    return 1 if command.status == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
