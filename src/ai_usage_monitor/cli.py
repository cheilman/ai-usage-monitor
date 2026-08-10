from __future__ import annotations

import argparse
import json
import sys
import time

from rich.columns import Columns
from rich.console import Console
from rich.live import Live

from ai_usage_monitor.cache import DEFAULT_MAX_AGE, SnapshotCache, fetch_snapshots
from ai_usage_monitor.config import load_config
from ai_usage_monitor.models import ProviderSnapshot, ProviderStatus
from ai_usage_monitor.providers import ALL_PROVIDERS
from ai_usage_monitor.render import render_snapshot_panel, snapshots_to_document

# Exit codes are part of the consumer contract (design K-000076 §5.5), so that a caller can
# gate on `ai-usage-monitor --json` without parsing anything.
EXIT_OK = 0
EXIT_NO_USABLE_DATA = 2

DEFAULT_INTERVAL = 30.0


def _build_providers(names: list[str]):
    config = load_config()
    providers = []
    for name in names:
        provider_cls = ALL_PROVIDERS[name]
        sub_config = getattr(config, name)
        providers.append(provider_cls(sub_config))
    return providers


def _resolve_provider_names(selected: str) -> list[str]:
    if selected == "all":
        return list(ALL_PROVIDERS)
    return [selected]


def _cache_for(args: argparse.Namespace) -> SnapshotCache | None:
    """None means "don't touch the cache at all" -- neither read nor write."""
    return None if args.no_cache else SnapshotCache()


def exit_code(snapshots: list[ProviderSnapshot], fail_on_degraded: bool = False) -> int:
    """0 when at least one provider is usable, 2 when nothing is.

    `--fail-on-degraded` raises the bar to *every* provider being ok, for callers that would
    rather stop than act on a fallback estimate.
    """
    statuses = [s.status for s in snapshots]
    if fail_on_degraded and any(status is not ProviderStatus.OK for status in statuses):
        return EXIT_NO_USABLE_DATA
    if any(status is ProviderStatus.OK for status in statuses):
        return EXIT_OK
    return EXIT_NO_USABLE_DATA


def cmd_snapshot(args: argparse.Namespace) -> int:
    names = _resolve_provider_names(args.provider)
    providers = _build_providers(names)
    snapshots = fetch_snapshots(providers, cache=_cache_for(args), max_age=args.max_age)
    code = exit_code(snapshots, getattr(args, "fail_on_degraded", False))

    if args.json:
        print(json.dumps(snapshots_to_document(snapshots), indent=2))
        return code

    console = Console()
    panels = [render_snapshot_panel(s) for s in snapshots]
    console.print(Columns(panels, equal=True, expand=True))
    if code != EXIT_OK:
        # A bare non-zero exit with a screenful of panels above it is a puzzle; say why.
        summary = ", ".join(f"{s.provider}={s.status.value}" for s in snapshots)
        Console(stderr=True).print(f"[dim]exit {code}: no usable quota data ({summary})[/dim]")
    return code


def cmd_dashboard(args: argparse.Namespace) -> int:
    names = _resolve_provider_names(args.provider)
    providers = _build_providers(names)
    cache = _cache_for(args)
    console = Console()

    def render():
        # Read-through cache: with the default 30s interval and 60s max-age we hit the
        # providers every other tick, and the panel subtitle shows the real data age.
        snapshots = fetch_snapshots(providers, cache=cache, max_age=args.max_age)
        panels = [render_snapshot_panel(s) for s in snapshots]
        return Columns(panels, equal=True, expand=True)

    try:
        with Live(render(), console=console, refresh_per_second=1, screen=True) as live:
            while True:
                time.sleep(args.interval)
                live.update(render())
    except KeyboardInterrupt:
        pass
    return 0


def _add_cache_args(target: argparse.ArgumentParser) -> None:
    target.add_argument(
        "--max-age",
        type=float,
        default=DEFAULT_MAX_AGE,
        metavar="S",
        help="reuse a cached snapshot up to S seconds old (0 = always fetch live; default: "
        f"{DEFAULT_MAX_AGE:g})",
    )
    target.add_argument(
        "--no-cache",
        action="store_true",
        help="ignore the on-disk cache entirely, and don't write to it",
    )


def _add_snapshot_args(parser: argparse.ArgumentParser) -> None:
    """Shared by `snapshot` and the bare top-level form, so the two can't drift apart."""
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the machine-readable schema_version 1 document",
    )
    parser.add_argument("--provider", choices=["all", *ALL_PROVIDERS], default="all")
    parser.add_argument(
        "--fail-on-degraded",
        action="store_true",
        help=f"exit {EXIT_NO_USABLE_DATA} unless every provider is ok, not just one",
    )
    _add_cache_args(parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-usage-monitor",
        description="Report Claude and Gemini usage against plan limits.",
        epilog=(
            f"exit codes: {EXIT_OK} = at least one provider ok; "
            f"{EXIT_NO_USABLE_DATA} = no usable quota data "
            "(or, with --fail-on-degraded, any provider not ok)"
        ),
    )
    subparsers = parser.add_subparsers(dest="command")

    snapshot_parser = subparsers.add_parser("snapshot", help="print current usage once and exit")
    _add_snapshot_args(snapshot_parser)
    snapshot_parser.set_defaults(func=cmd_snapshot)

    dashboard_parser = subparsers.add_parser("dashboard", help="live-updating terminal dashboard")
    dashboard_parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help=f"refresh interval in seconds (default: {DEFAULT_INTERVAL:g})",
    )
    dashboard_parser.add_argument(
        "--provider", choices=["all", *ALL_PROVIDERS], default="all"
    )
    _add_cache_args(dashboard_parser)
    dashboard_parser.set_defaults(func=cmd_dashboard)

    # Bare `ai-usage-monitor` with no subcommand behaves like `snapshot`.
    _add_snapshot_args(parser)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "command", None) is None:
        return cmd_snapshot(args)

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
