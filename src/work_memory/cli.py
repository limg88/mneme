import argparse
import json
import os
import sys
from pathlib import Path

from .budget import Budget
from .models import Settings
from .retention import prune_logs
from .store import Store
from .worker import (
    EXTRACTOR_VERSION,
    DockerClaude,
    derive_tables,
    exclusive,
    observations_from_evidence,
    schedule,
    sync,
)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Local temporal work memory")
    parser.add_argument("--data", default=os.environ.get("WORK_MEMORY_DATA", ".local/memory"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("status")
    commands.add_parser("budget")
    commands.add_parser("housekeeping")
    commands.add_parser("backup")
    restore = commands.add_parser("restore")
    restore.add_argument("snapshot", type=Path)
    restore.add_argument("--into", type=Path, required=True)
    catchup = commands.add_parser("restore-catchup")
    catchup.add_argument("--from-live", type=Path, required=True)
    derive = commands.add_parser("derive")
    derive.add_argument(
        "--observation",
        help="Derive literal tables or native Jira history from a saved observation",
    )
    local_extract = commands.add_parser("extract-local")
    local_extract.add_argument("--model", default="qwen3:8b")
    selection = local_extract.add_mutually_exclusive_group(required=True)
    selection.add_argument("--observation")
    selection.add_argument("--project")
    local_extract.add_argument("--max-sections", type=int, default=10)
    index = commands.add_parser("index")
    index.add_argument("--model", default="bge-m3:latest")
    index.add_argument("--observation")
    index.add_argument("--max-batches", type=int, default=20)
    index.add_argument("--batch-size", type=int, default=8)
    reset = commands.add_parser("reset-scope")
    reset.add_argument("scope_id")
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--open", action="store_true", help="Open the local UI with one-use login")
    serve.add_argument(
        "--allow-cloud", action="store_true", help="Enable Haiku chat, within budget"
    )
    serve.add_argument("--compose", default="docker/claude-probe/compose.yaml", type=Path)
    commands.add_parser("mcp")
    for name in ("sync", "schedule", "extract", "import-evidence"):
        sub = commands.add_parser(name)
        sub.add_argument("--config", default=".local/settings.json")
        if name == "import-evidence":
            sub.add_argument("file", type=Path)
        else:
            sub.add_argument("--compose", default="docker/claude-probe/compose.yaml", type=Path)
            if name == "schedule":
                sub.add_argument(
                    "--cycles", type=int, help="Stop after N cycles; no permanent timer"
                )
            if name == "extract":
                sub.add_argument("--observation", help="Process only this saved observation")
            sub.add_argument(
                "--allow-cloud",
                action="store_true",
                help="Explicit consent to consume the configured Claude plan quota",
            )
            sub.add_argument("--no-extract", action="store_true")
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--mode", choices=["lexical", "semantic", "hybrid"], default="lexical")
    facts = commands.add_parser("facts")
    facts.add_argument("--project")
    facts.add_argument("--subject")
    facts.add_argument("--valid-at")
    facts.add_argument("--known-at")
    withdraw = commands.add_parser("withdraw")
    withdraw.add_argument("source")
    withdraw.add_argument("source_id")
    withdraw.add_argument("project")
    args = parser.parse_args()
    store = Store(args.data, control_dir=Path(".local/control").resolve())
    try:
        existing_budget = store.runtime_policy("budget_file")
        startup_cleanup = None
        if existing_budget and Path(existing_budget).exists():
            startup_cleanup = Budget(Path(existing_budget)).cleanup_logs()
        if args.command == "init":
            store.token()
            print(
                json.dumps(
                    {
                        "database": str(store.path),
                        "api_token_file": str(store.directory / "api-token"),
                    }
                )
            )
        elif args.command == "serve":
            import uvicorn

            from .api import create_app
            from .chat import Chat

            chat_worker = None
            if args.allow_cloud:
                ledger = (
                    Path(existing_budget)
                    if existing_budget
                    else args.compose.resolve().parents[2] / ".local/control/budget.sqlite3"
                )
                if not ledger.is_file():
                    raise RuntimeError("Existing spending ledger required; no chat budget created")
                chat_worker = DockerClaude(
                    args.compose, store.directory / "runs", "haiku", 180, budget=Budget(ledger)
                )
                store.set_runtime_policy("budget_file", str(ledger.resolve()))
                print(
                    "Chat Haiku: consumes corporate Claude quota; shared spending ledger.",
                    file=sys.stderr,
                )
            import secrets
            import threading
            import webbrowser

            launch_key = secrets.token_urlsafe(32) if args.open else None
            if launch_key:
                # The one-use key is exchanged for an HttpOnly session cookie. Never
                # expose the long-lived API bearer in HTML, browser storage or URLs.
                threading.Timer(
                    1, webbrowser.open, args=[f"http://127.0.0.1:{args.port}/connect/{launch_key}"]
                ).start()
            print(f"Mneme: http://127.0.0.1:{args.port}", file=sys.stderr)
            uvicorn.run(
                create_app(store, chat=Chat(store, chat_worker), launch_key=launch_key),
                host="127.0.0.1",
                port=args.port,
                access_log=False,
            )
        elif args.command == "mcp":
            from .mcp_server import create_server

            create_server(store).run(transport="stdio")
        elif args.command == "status":
            print(json.dumps(store.status(), indent=2))
        elif args.command == "budget":
            if existing_budget and not Path(existing_budget).exists():
                raise RuntimeError("Spending ledger missing; refusing to reset the budget")
            budget = Budget(
                Path(existing_budget) if existing_budget else Path(".local/control/budget.sqlite3")
            )
            store.set_runtime_policy("budget_file", str(budget.path))
            print(json.dumps(budget.status(), indent=2))
        elif args.command == "housekeeping":
            result = startup_cleanup or prune_logs(store.directory / "runs")
            print(json.dumps(result, indent=2))
        elif args.command == "backup":
            from .backups import backup

            with exclusive(store.directory):
                print(json.dumps(backup(store), indent=2))
        elif args.command == "restore":
            from .backups import restore

            with exclusive(store.directory):
                print(json.dumps(restore(store, args.snapshot, args.into), indent=2))
        elif args.command == "restore-catchup":
            from .backups import catch_up_from_live

            live = Store(args.from_live)
            with exclusive(store.directory):
                print(json.dumps(catch_up_from_live(store, live), indent=2))
        elif args.command == "derive":
            with exclusive(store.directory):
                if not store.runtime_policy("active_extractor"):
                    store.set_runtime_policy("active_extractor", EXTRACTOR_VERSION + ":haiku")
                print(
                    json.dumps(
                        {
                            "deterministic_facts": derive_tables(store, args.observation),
                            "cloud_calls": 0,
                        }
                    )
                )
        elif args.command == "extract-local":
            from .local_extraction import extract_local

            with exclusive(store.directory):
                result = extract_local(
                    store,
                    model=args.model,
                    observation_id=args.observation,
                    project=args.project,
                    max_sections=args.max_sections,
                )
                print(json.dumps(result, indent=2))
                if result["errors"]:
                    raise SystemExit(1)
        elif args.command == "index":
            from .semantic import index

            with exclusive(store.directory):
                print(
                    json.dumps(
                        index(
                            store,
                            model=args.model,
                            observation_id=args.observation,
                            max_batches=args.max_batches,
                            batch_size=args.batch_size,
                        ),
                        indent=2,
                    )
                )
        elif args.command == "reset-scope":
            state = store.collection_state(args.scope_id)
            if not state:
                raise ValueError("Unknown collection scope")
            state.update(scan_upper=None, cursor=None, seen_cursors=[], state="reset", error=None)
            store.save_collection_state(args.scope_id, state)
            print(json.dumps({"scope_id": args.scope_id, "state": "reset"}))
        elif args.command == "search":
            print(
                json.dumps(store.search(args.query, mode=args.mode), indent=2, ensure_ascii=False)
            )
        elif args.command == "facts":
            print(
                json.dumps(
                    store.facts(
                        project=args.project,
                        subject=args.subject,
                        valid_at=args.valid_at,
                        known_at=args.known_at,
                    ),
                    indent=2,
                    ensure_ascii=False,
                )
            )
        elif args.command == "withdraw":
            print(
                json.dumps(
                    {
                        "withdrawn_observations": store.withdraw(
                            args.source, args.source_id, args.project
                        )
                    }
                )
            )
        else:
            settings = Settings.model_validate_json(Path(args.config).read_text())
            if args.command == "import-evidence":
                payload = json.loads(args.file.read_text())
                items = observations_from_evidence(payload["evidence"], settings)
                print(json.dumps({"imported": [store.ingest(item) for item in items]}))
                return
            if not args.allow_cloud and (
                settings.targets
                or settings.scopes
                or settings.mail_scopes
                or settings.calendar_scopes
                or settings.teams_scopes
                or (not args.no_extract and not settings.local_model)
            ):
                parser.error("This run may consume Claude quota: pass --allow-cloud explicitly")
            client = (
                DockerClaude(
                    args.compose, store.directory / "runs", settings.model, settings.timeout_seconds
                )
                if args.allow_cloud
                else None
            )
            if client:
                if existing_budget and not Path(existing_budget).exists():
                    raise RuntimeError("Spending ledger missing; no cloud call allowed")
                client.budget = Budget(
                    Path(existing_budget)
                    if existing_budget
                    else args.compose.resolve().parents[2] / ".local/control/budget.sqlite3"
                )
                store.set_runtime_policy("budget_file", str(client.budget.path))
            if args.allow_cloud:
                print(
                    f"Cloud run: {settings.model}; consumes Claude plan quota. "
                    "No automatic model upgrade.",
                    file=sys.stderr,
                )
            if args.command == "schedule":
                schedule(store, settings, client, extract=not args.no_extract, cycles=args.cycles)
            else:
                result = sync(
                    store,
                    settings,
                    client,
                    extract=not args.no_extract,
                    collect=args.command != "extract",
                    observation_id=getattr(args, "observation", None),
                )
                print(json.dumps(result, indent=2))
                if result["errors"]:
                    raise SystemExit(1)
    except (ValueError, RuntimeError, KeyError, OSError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
