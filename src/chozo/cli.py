"""argparse CLI wiring for chozo.

Subcommands:
  init / new / up / down (rollback) / status / mark / unmark / run / history / inspect

Every state-changing path is non-interactive when given --yes/--json; prod is
gated behind --confirm-prod (or "type PROD" when interactive). Exit codes:
0 success, 1 failure, 2 nothing to do.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from fnmatch import fnmatch

from chozo import config, discovery, history, migrator
from chozo.constants import APP_NAME, EXIT_FAIL, EXIT_NOTHING_TO_DO, EXIT_OK
from chozo.discovery import Migration
from chozo.history import FileHistoryStore
from chozo.output import Output

_NAME_SANITIZE = re.compile(r"[^a-z0-9_]+")


@dataclass
class Ctx:
    cfg: config.ProjectConfig
    out: Output


def _store(ctx: Ctx) -> FileHistoryStore:
    return FileHistoryStore(ctx.cfg.history_path)


def _env_url(ctx: Ctx, env: str) -> str:
    return config.require_env_url(ctx.cfg.envs, env)


def _prod_gate(env: str, dry_run: bool, confirm_prod: bool, out: Output) -> bool:
    """True if a prod apply/rollback may proceed."""
    if env != "prod" or dry_run:
        return True
    if confirm_prod:
        return True
    if out.quiet:
        out.error("prod requires --confirm-prod (or run interactively without --json).")
        return False
    out.warning("You are about to run this against PRODUCTION.")
    return out.prompt("Type PROD to confirm") == "PROD"


# --- init ---


def cmd_init(args: argparse.Namespace, ctx: Ctx) -> int:
    cfg = ctx.cfg
    config_file = cfg.root / "chozo.toml"
    if config_file.exists() and not args.force:
        ctx.out.error(f"{config_file} already exists (use --force to overwrite).")
        return EXIT_FAIL
    migrations_dir = cfg.root / (args.migrations_dir or "migrations")
    migrations_dir.mkdir(parents=True, exist_ok=True)
    name = args.name or cfg.root.name
    config_file.write_text(
        f'''[project]
name = "{name}"
migrations_dir = "{args.migrations_dir or "migrations"}"

[envs.local]
url_var = "DATABASE_URL_LOCAL"

[envs.dev]
url_var = "DATABASE_URL_DEV"

[envs.prod]
url_var = "DATABASE_URL"
'''
    )
    ctx.out.success(f"Initialized chozo project '{name}' at {cfg.root}")
    ctx.out.note(f"Migrations dir: {migrations_dir}")
    ctx.out.note("Set DATABASE_URL_LOCAL / _DEV / or DATABASE_URL to connect.")
    return EXIT_OK


# --- new ---


def cmd_new(args: argparse.Namespace, ctx: Ctx) -> int:
    cfg = ctx.cfg
    slug = _NAME_SANITIZE.sub("_", args.name.lower()).strip("_")
    if not slug:
        ctx.out.error("Migration name must contain at least one alphanumeric character.")
        return EXIT_FAIL
    cfg.migrations_dir.mkdir(parents=True, exist_ok=True)
    num = discovery.next_number(cfg.migrations_dir)
    name = f"{num:03d}_{slug}"
    mig_dir = cfg.migrations_dir / name
    mig_dir.mkdir(parents=True, exist_ok=True)
    (mig_dir / "up.sql").write_text(f"-- migration: {name} (up)\n-- describe the change here\n\n")
    (mig_dir / "down.sql").write_text(f"-- migration: {name} (down)\n-- reverse the change here\n\n")
    ctx.out.success(f"Created {name}/")
    ctx.out.note(str(mig_dir / "up.sql"))
    ctx.out.note(str(mig_dir / "down.sql"))
    return EXIT_OK


# --- shared UP apply (used by `up` and `run`) ---


def _select_up_targets(
    all_migs: list[Migration], pending_names: list[str], *, target: str | None, to: str | None
) -> list[Migration] | None:
    """Resolve which pending migrations to apply. None means the selector matched nothing."""
    by_name = {m.name: m for m in all_migs}
    ordered_names = [m.name for m in all_migs]
    pending_set = set(pending_names)
    base = [by_name[n] for n in ordered_names if n in pending_set]

    if to is not None:
        idx = next((i for i, n in enumerate(ordered_names) if fnmatch(n, to)), None)
        if idx is None:
            return None
        allowed = set(ordered_names[: idx + 1])
        return [m for m in base if m.name in allowed]

    if target is not None and target != "all":
        matched = [m for m in base if fnmatch(m.name, target)]
        return matched

    return base


def _run_up(
    ctx: Ctx,
    env: str,
    *,
    selector: Callable[[], list[Migration] | None],
    dry_run: bool,
    yes: bool,
    confirm_prod: bool,
    json_out: bool,
) -> int:
    cfg = ctx.cfg
    url = _env_url(ctx, env)
    ctx.out.env_badge(env, cfg.envs.get(env, ""), config_target(url))

    store = _store(ctx)
    hist = history.load(store)
    _ = hist  # selector reads history fresh; keep load to surface corrupt-file errors early.

    targets = selector()
    if targets is None or not targets:
        ctx.out.info(f"No pending migrations for {env}.")
        if json_out:
            ctx.out.emit_json(
                {
                    "status": "success",
                    "env": env,
                    "dry_run": dry_run,
                    "migrations": [],
                    "summary": {"applied": 0, "failed": 0, "skipped": 0},
                }
            )
        return EXIT_NOTHING_TO_DO

    if not _prod_gate(env, dry_run, confirm_prod, ctx.out):
        return EXIT_FAIL

    if not yes and not ctx.out.quiet:
        ctx.out.pending_table([(m.name, m.up.stat().st_size) for m in targets])
        if not ctx.out.confirm(f"Apply {len(targets)} pending migration(s) to {env}?", default=False):
            ctx.out.info("Aborted.")
            return EXIT_FAIL

    return _execute_up(ctx, env, url, hist, store, targets, dry_run=dry_run, json_out=json_out)


def _execute_up(
    ctx: Ctx,
    env: str,
    url: str,
    hist: dict,
    store: FileHistoryStore,
    targets: list[Migration],
    *,
    dry_run: bool,
    json_out: bool,
) -> int:
    results: list[dict] = []
    applied = failed = skipped = 0
    for mig in targets:
        res = migrator.execute(url, mig, dry_run=dry_run)
        entry: dict = {
            "file": mig.name,
            "result": res.status,
            "elapsed_s": round(res.duration, 3) if res.duration else 0,
        }
        if res.status == "applied":
            if not dry_run:
                history.record(store, hist, env, mig.name, method="executed", duration_seconds=res.duration)
            applied += 1
            ctx.out.success(f"Applied {mig.name}" + (f" [{res.duration:.2f}s]" if res.duration else ""))
        elif res.status == "blocked":
            skipped += 1
            entry["error"] = res.error
            ctx.out.warning(f"Blocked {mig.name}: {res.error}")
        else:
            failed += 1
            entry["error"] = res.error
            if not dry_run:
                history.record(
                    store,
                    hist,
                    env,
                    mig.name,
                    method="executed",
                    duration_seconds=res.duration,
                    success=False,
                    error=res.error,
                )
            ctx.out.error(f"Failed {mig.name}: {res.error}")
            results.append(entry)
            break
        results.append(entry)

    verb = "applied" if not dry_run else "dry-run"
    ctx.out.summary(applied, len(targets), env, verb=verb, skipped=skipped)
    if json_out:
        ctx.out.emit_json(
            {
                "status": "failed" if failed else "success",
                "env": env,
                "dry_run": dry_run,
                "migrations": results,
                "summary": {"applied": applied, "failed": failed, "skipped": skipped},
            }
        )
    return EXIT_FAIL if failed else EXIT_OK


def cmd_up(args: argparse.Namespace, ctx: Ctx) -> int:
    if args.env is None:
        ctx.out.error("--env is required (set in chozo.toml [envs]).")
        return EXIT_FAIL

    def selector() -> list[Migration] | None:
        store = _store(ctx)
        hist = history.load(store)
        all_migs = discovery.discover(ctx.cfg.migrations_dir)
        names = [m.name for m in all_migs]
        pending_names = history.get_pending(args.env, hist, names)
        return _select_up_targets(all_migs, pending_names, target=None, to=args.to)

    return _run_up(
        ctx,
        args.env,
        selector=selector,
        dry_run=args.dry_run,
        yes=args.yes,
        confirm_prod=args.confirm_prod,
        json_out=args.json,
    )


def cmd_run(args: argparse.Namespace, ctx: Ctx) -> int:
    def selector() -> list[Migration] | None:
        store = _store(ctx)
        hist = history.load(store)
        all_migs = discovery.discover(ctx.cfg.migrations_dir)
        names = [m.name for m in all_migs]
        pending_names = history.get_pending(args.env, hist, names)
        return _select_up_targets(all_migs, pending_names, target=args.target, to=None)

    return _run_up(
        ctx,
        args.env,
        selector=selector,
        dry_run=args.dry_run,
        yes=True,
        confirm_prod=args.confirm_prod,
        json_out=args.json,
    )


# --- down / rollback ---


def cmd_down(args: argparse.Namespace, ctx: Ctx) -> int:
    cfg = ctx.cfg
    url = _env_url(ctx, args.env)
    ctx.out.env_badge(args.env, cfg.envs.get(args.env, ""), config_target(url))

    store = _store(ctx)
    hist = history.load(store)
    all_migs = discovery.discover(cfg.migrations_dir)
    by_name = {m.name: m for m in all_migs}
    applied_names = [n for n in history.get_applied_names(args.env, hist) if n in by_name]
    # Most-recently-applied first.
    targets = sorted((by_name[n] for n in applied_names), key=lambda m: m.number, reverse=True)[: args.count]

    if not targets:
        ctx.out.info(f"No applied migrations to roll back for {args.env}.")
        if args.json:
            ctx.out.emit_json(
                {
                    "status": "success",
                    "env": args.env,
                    "dry_run": args.dry_run,
                    "migrations": [],
                    "summary": {"rolled_back": 0, "failed": 0},
                }
            )
        return EXIT_NOTHING_TO_DO

    if not _prod_gate(args.env, args.dry_run, args.confirm_prod, ctx.out):
        return EXIT_FAIL

    if not args.yes and not ctx.out.quiet:
        rows = [(m.name, (m.down.stat().st_size if m.down is not None else 0)) for m in targets]
        ctx.out.pending_table(rows)
        if not ctx.out.confirm(f"Roll back {len(targets)} migration(s) from {args.env}?", default=False):
            ctx.out.info("Aborted.")
            return EXIT_FAIL

    results: list[dict] = []
    rolled_back = failed = 0
    for mig in targets:
        res = migrator.execute_down(url, mig, dry_run=args.dry_run)
        entry = {"file": mig.name, "result": res.status, "elapsed_s": round(res.duration, 3) if res.duration else 0}
        if res.status == "applied":
            if not args.dry_run:
                history.record(store, hist, args.env, mig.name, method="rolled_back", duration_seconds=res.duration)
                history.remove(store, hist, args.env, mig.name)
            rolled_back += 1
            ctx.out.success(f"Rolled back {mig.name}")
        else:
            failed += 1
            entry["error"] = res.error
            ctx.out.error(f"Rollback failed {mig.name}: {res.error}")
            results.append(entry)
            break
        results.append(entry)

    ctx.out.summary(rolled_back, len(targets), args.env, verb="rolled back")
    if args.json:
        ctx.out.emit_json(
            {
                "status": "failed" if failed else "success",
                "env": args.env,
                "dry_run": args.dry_run,
                "migrations": results,
                "summary": {"rolled_back": rolled_back, "failed": failed},
            }
        )
    return EXIT_FAIL if failed else EXIT_OK


# --- status / mark / unmark / history / inspect ---


def cmd_status(args: argparse.Namespace, ctx: Ctx) -> int:
    store = _store(ctx)
    hist = history.load(store)
    all_migs = discovery.discover(ctx.cfg.migrations_dir)
    names = [m.name for m in all_migs]
    pending = history.get_pending(args.env, hist, names)
    applied = hist.get(args.env, {})
    if args.json:
        ctx.out.emit_json({"env": args.env, "applied": [n for n in names if n not in pending], "pending": pending})
        return EXIT_OK
    ctx.out.status_tree(args.env, names, applied, pending)
    ctx.out.note(f"Total {len(names)}  Applied {len(names) - len(pending)}  Pending {len(pending)}")
    return EXIT_OK


def cmd_mark(args: argparse.Namespace, ctx: Ctx) -> int:
    store = _store(ctx)
    hist = history.load(store)
    all_migs = discovery.discover(ctx.cfg.migrations_dir)
    names = [m.name for m in all_migs]
    pending = history.get_pending(args.env, hist, names)
    targets = [n for n in pending if fnmatch(n, args.pattern or "*")] if args.pattern else pending
    if not targets:
        ctx.out.success(f"No pending migrations to mark for {args.env}.")
        return EXIT_OK
    if (
        not args.yes
        and not ctx.out.quiet
        and not ctx.out.confirm(f"Mark {len(targets)} migration(s) as applied in {args.env}?", default=False)
    ):
        ctx.out.info("Aborted.")
        return EXIT_FAIL
    for name in targets:
        history.record(store, hist, args.env, name, method="marked")
        ctx.out.success(f"Marked {name}")
    ctx.out.summary(len(targets), len(targets), args.env, verb="marked")
    return EXIT_OK


def cmd_unmark(args: argparse.Namespace, ctx: Ctx) -> int:
    store = _store(ctx)
    hist = history.load(store)
    if not history.remove(store, hist, args.env, args.name):
        ctx.out.warning(f"{args.name} not found in {args.env} history.")
        return EXIT_OK
    ctx.out.success(f"Removed {args.name} from {args.env} history.")
    return EXIT_OK


def cmd_history(args: argparse.Namespace, ctx: Ctx) -> int:
    store = _store(ctx)
    hist = history.load(store)
    env_data = hist.get(args.env, {})
    if args.json:
        ctx.out.emit_json({"env": args.env, "history": env_data})
        return EXIT_OK
    if not env_data:
        ctx.out.info(f"No history recorded for {args.env}.")
        return EXIT_OK
    for name, entry in sorted(env_data.items()):
        at = (entry.get("applied_at") or "")[:19].replace("T", " ")
        method = entry.get("method") or ""
        ctx.out.note(f"● {name}  {method}  {at}")
        for ev in entry.get("events", []):
            ok = ev.get("success", True)
            icon = "✓" if ok else "✗"
            ev_at = (ev.get("at") or "")[:19].replace("T", " ")
            line = f"  {icon} {ev.get('action', '?')}  {ev_at}  by {ev.get('by', '?')}"
            if ev.get("error"):
                line += f"  error: {ev['error']}"
            ctx.out.note(line)
    return EXIT_OK


def cmd_inspect(args: argparse.Namespace, ctx: Ctx) -> int:
    url = _env_url(ctx, args.env)
    schema = migrator.reflect(url)
    ctx.out.emit_json({"env": args.env, "schema": schema})
    return EXIT_OK


def config_target(url: str) -> str:
    from chozo.connection import format_target

    return format_target(url)


# --- parser ---


def _add_env(sp: argparse.ArgumentParser, required: bool = False) -> None:
    sp.add_argument("--env", required=required, help="Environment (from chozo.toml [envs]).")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Agent-friendly PostgreSQL migrations + schema inspection.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--json", action="store_true", dest="json_out", help="Emit JSON to stdout (agent mode).")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("init", help="Create chozo.toml + migrations dir.")
    sp.add_argument("--name", help="Project name (defaults to directory name).")
    sp.add_argument("--migrations-dir", default="migrations", help="Migrations directory (relative to project root).")
    sp.add_argument("--force", action="store_true", help="Overwrite an existing chozo.toml.")

    sp = sub.add_parser("new", help="Create a new migration (UP + DOWN).")
    sp.add_argument("name", help="Snake_case migration name, e.g. add_users_table.")

    sp = sub.add_parser("up", help="Apply pending migrations.")
    _add_env(sp)
    sp.add_argument("--to", metavar="NAME", help="Apply up to (and including) the migration matching NAME.")
    sp.add_argument("--yes", "-y", action="store_true", help="No confirmation prompt.")
    sp.add_argument("--dry-run", action="store_true", help="Execute then ROLLBACK; record nothing.")
    sp.add_argument("--confirm-prod", action="store_true", help="Consent flag required for --env prod.")
    sp.add_argument("--json", action="store_true", dest="json", help="Emit JSON to stdout.")

    sp = sub.add_parser("down", aliases=["rollback"], help="Roll back applied migrations (DOWN).")
    _add_env(sp)
    sp.add_argument("--count", "-n", type=int, default=1, help="Number of migrations to roll back (default 1).")
    sp.add_argument("--yes", "-y", action="store_true", help="No confirmation prompt.")
    sp.add_argument("--dry-run", action="store_true", help="Execute then ROLLBACK; record nothing.")
    sp.add_argument("--confirm-prod", action="store_true", help="Consent flag required for --env prod.")
    sp.add_argument("--json", action="store_true", dest="json", help="Emit JSON to stdout.")

    sp = sub.add_parser("status", help="Show applied vs pending.")
    _add_env(sp, required=True)
    sp.add_argument("--json", action="store_true", dest="json", help="Emit JSON to stdout.")

    sp = sub.add_parser("mark", help="Mark migrations as applied without executing.")
    _add_env(sp, required=True)
    sp.add_argument("pattern", nargs="?", default="*", help="Glob, e.g. '003_*' or '*'.")
    sp.add_argument("--yes", "-y", action="store_true", help="No confirmation prompt.")

    sp = sub.add_parser("unmark", help="Remove a migration from history.")
    sp.add_argument("name", help="Migration directory name.")
    _add_env(sp, required=True)

    sp = sub.add_parser("history", help="Show the event trail for an environment.")
    _add_env(sp, required=True)
    sp.add_argument("--json", action="store_true", dest="json", help="Emit JSON to stdout.")

    sp = sub.add_parser("inspect", help="Reflect the live schema as JSON (agents).")
    _add_env(sp, required=True)
    sp.add_argument("--json", action="store_true", dest="json", help="Emit JSON to stdout (default for inspect).")

    sp = sub.add_parser("run", help="Non-interactive apply for agents/CI.")
    sp.add_argument("target", help="'all', a glob, or a migration name.")
    _add_env(sp, required=True)
    sp.add_argument("--dry-run", action="store_true", help="Execute then ROLLBACK; record nothing.")
    sp.add_argument("--confirm-prod", action="store_true", help="Consent flag required for --env prod.")
    sp.add_argument("--json", action="store_true", dest="json", help="Emit JSON to stdout.")

    return p


_HANDLERS = {
    "init": cmd_init,
    "new": cmd_new,
    "up": cmd_up,
    "down": cmd_down,
    "rollback": cmd_down,
    "status": cmd_status,
    "mark": cmd_mark,
    "unmark": cmd_unmark,
    "history": cmd_history,
    "inspect": cmd_inspect,
    "run": cmd_run,
}


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    out = Output(quiet=bool(args.json_out or getattr(args, "json", False)))
    ctx = Ctx(cfg=config.load_config(), out=out)

    out.banner(ctx.cfg.name)
    fn = _HANDLERS.get(args.command)
    if fn is None:
        out.error(f"unknown command: {args.command}")
        sys.exit(EXIT_FAIL)
    try:
        rc = fn(args, ctx)
    except Exception as exc:  # surfacing a clean error beats a traceback for agents/CI
        if out.quiet:
            out.emit_json({"status": "error", "error": str(exc), "type": type(exc).__name__})
        else:
            out.error(str(exc))
        sys.exit(EXIT_FAIL)
    sys.exit(rc if rc is not None else EXIT_OK)


if __name__ == "__main__":
    main()
