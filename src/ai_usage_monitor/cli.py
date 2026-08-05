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
from ai_usage_monitor.providers import ALL_PROVIDERS
from ai_usage_monitor.render import render_snapshot_panel, snapshot_to_dict

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


def cmd_snapshot(args: argparse.Namespace) -> int:
    names = _resolve_provider_names(args.provider)
    providers = _build_providers(names)
    snapshots = fetch_snapshots(providers, cache=_cache_for(args), max_age=args.max_age)

    if args.json:
        print(json.dumps([snapshot_to_dict(s) for s in snapshots], indent=2))
        return 0

    console = Console()
    panels = [render_snapshot_panel(s) for s in snapshots]
    console.print(Columns(panels, equal=True, expand=True))
    return 0


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-usage-monitor",
        description="Report Claude and Gemini usage against plan limits.",
    )
    subparsers = parser.add_subparsers(dest="command")

    snapshot_parser = subparsers.add_parser("snapshot", help="print current usage once and exit")
    snapshot_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    snapshot_parser.add_argument(
        "--provider", choices=["all", *ALL_PROVIDERS], default="all"
    )
    _add_cache_args(snapshot_parser)
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
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--provider", choices=["all", *ALL_PROVIDERS], default="all")
    _add_cache_args(parser)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "command", None) is None:
        return cmd_snapshot(args)

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
