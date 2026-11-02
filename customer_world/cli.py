"""Command-line entry points. No inference happens during init or inspection."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from . import __version__


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Autonomous customer research for product teams.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    def project_command(name: str, help_text: str) -> argparse.ArgumentParser:
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--project", type=Path, required=True)
        return command

    init = project_command("init", "Prepare source contexts without generating customer profiles")
    init.add_argument("--sources", type=Path, required=True)
    init.add_argument("--count", type=int, default=100)
    init.add_argument("--timezone", default="UTC")
    init.add_argument("--tool", action="append", default=[], metavar="ID=URL")

    for name in ("generate", "run"):
        command = project_command(name, "Generate grounded profiles" if name == "generate" else "Run today's autonomous follow-up")
        command.add_argument("--provider", choices=("openai", "command"), required=True)
        command.add_argument("--model", help="Explicit model identifier for the openai provider")
        command.add_argument("--command-json", help="JSON argv array for an external worker; never a shell command")
        command.add_argument("--provider-timeout", type=float, default=120)
        command.add_argument("--workers", type=int, choices=range(1, 4), default=3)
        if name == "run":
            command.add_argument("--date", help="Must equal today's date in the project timezone")
            command.add_argument("--max-turns", type=int, default=20)
            command.add_argument("--broker-timeout", type=float, default=300)

    status = project_command("status", "Inspect aggregate counts without calling a provider")
    status.add_argument("--date")
    report = project_command("report", "Export aggregate-only HTML")
    report.add_argument("--date")
    report.add_argument("--output", type=Path, required=True)
    report.add_argument("--force", action="store_true", help="Replace an existing report")
    project_command("verify", "Check stored identity and evidence integrity")
    project_command("pending", "List pending requests for the host UI operator")
    respond = project_command("respond", "Attach an operator observation to a pending UI request")
    respond.add_argument("--request", required=True, metavar="REQUEST_ID")
    respond.add_argument("--text-file", type=Path)
    respond.add_argument("--url", default="")
    respond.add_argument("--screenshot", type=Path)
    respond.add_argument("--ax", type=Path)
    respond.add_argument("--error", help="Record an environment failure instead of successful UI use")
    respond.add_argument("--closed", action="store_true", help="Attest that the requested customer UI context has been closed; finish requests only")
    return parser


def _provider(args: argparse.Namespace):
    from .providers import CommandProvider, OpenAIResponsesProvider
    if args.provider == "openai":
        if not args.model:
            raise ValueError("--model is required for the openai provider")
        return OpenAIResponsesProvider(args.model, timeout=args.provider_timeout)
    if not args.command_json:
        raise ValueError("--command-json is required for the command provider")
    try:
        argv = json.loads(args.command_json)
    except json.JSONDecodeError:
        raise ValueError("--command-json must contain a JSON argv array") from None
    if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or not x for x in argv):
        raise ValueError("--command-json must contain a nonempty JSON argv array of nonempty strings")
    return CommandProvider(argv, timeout=args.provider_timeout)


def _tools(values: list[str]) -> list[dict]:
    result = []
    for value in values:
        if "=" not in value:
            raise ValueError("--tool must use ID=URL")
        key, url = value.split("=", 1)
        result.append({"id": key, "url": url, "primary": key == "product"})
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        from .core import Project, generate_profiles, get_summary, run_day, verify_project
        from .broker import FileBroker
        from .report import write_report
        if args.command == "init":
            project = Project.init(args.project, args.sources, count=args.count,
                                   timezone=args.timezone, tools=_tools(args.tool) or None)
            result = get_summary(project)
        else:
            project = Project(args.project)
            if args.command == "generate":
                result = generate_profiles(project, _provider(args), workers=args.workers)
            elif args.command == "run":
                if args.max_turns < 1 or args.broker_timeout <= 0:
                    raise ValueError("Turn and timeout limits must be positive")
                broker = FileBroker(args.project, timeout=args.broker_timeout)
                result = run_day(project, _provider(args), broker, day=args.date,
                                 workers=args.workers, max_turns=args.max_turns)
            elif args.command == "status":
                result = get_summary(project, day=args.date)
            elif args.command == "report":
                output = write_report(get_summary(project, day=args.date), args.output, force=args.force)
                result = {"report": str(output), "scope": "aggregate_only"}
            elif args.command == "verify":
                result = verify_project(project)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0 if result.get("valid", result.get("ok", not result.get("issues"))) else 1
            elif args.command == "pending":
                broker = FileBroker(args.project)
                result = []
                for path in sorted(broker.pending_dir.glob("*.json")):
                    if path.is_symlink() or not path.resolve().is_relative_to(broker.pending_dir.resolve()):
                        raise ValueError("Pending requests must be regular project files")
                    result.append(json.loads(path.read_text(encoding="utf-8")))
            else:
                broker = FileBroker(args.project)
                text = args.text_file.read_text(encoding="utf-8") if args.text_file else ""
                if not args.error and (not text.strip() or not args.screenshot or not args.ax):
                    raise ValueError("Successful responses require --text-file, --screenshot and --ax")
                result = broker.respond(args.request, text=text, url=args.url,
                                        screenshot=args.screenshot, ax=args.ax,
                                        error=args.error, closed=args.closed)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        print("Interrupted; recorded checkpoints remain available for resume.", file=sys.stderr)
        return 130
    except Exception as exc:
        # External transport errors are already sanitized by their adapters.
        from .providers import ProviderError
        from .broker import BrokerError
        if isinstance(exc, (ValueError, FileExistsError, FileNotFoundError, ProviderError, BrokerError)):
            print(f"customer-world: {exc}", file=sys.stderr)
        else:
            print(f"customer-world: {type(exc).__name__}; inspect the local project checkpoints", file=sys.stderr)
        return 1
