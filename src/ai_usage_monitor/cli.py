from __future__ import annotations

import argparse
import json
import sys
import time

from rich.columns import Columns
from rich.console import Console
from rich.live import Live

from ai_usage_monitor.config import load_config
from ai_usage_monitor.providers import ALL_PROVIDERS
from ai_usage_monitor.render import render_snapshot_panel, snapshot_to_dict


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


def cmd_snapshot(args: argparse.Namespace) -> int:
    names = _resolve_provider_names(args.provider)
    providers = _build_providers(names)
    snapshots = [p.fetch() for p in providers]

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
    console = Console()

    def render():
        snapshots = [p.fetch() for p in providers]
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
    snapshot_parser.set_defaults(func=cmd_snapshot)

    dashboard_parser = subparsers.add_parser("dashboard", help="live-updating terminal dashboard")
    dashboard_parser.add_argument(
        "--interval", type=float, default=5.0, help="refresh interval in seconds"
    )
    dashboard_parser.add_argument(
        "--provider", choices=["all", *ALL_PROVIDERS], default="all"
    )
    dashboard_parser.set_defaults(func=cmd_dashboard)

    # Bare `ai-usage-monitor` with no subcommand behaves like `snapshot`.
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--provider", choices=["all", *ALL_PROVIDERS], default="all")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "command", None) is None:
        return cmd_snapshot(args)

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
