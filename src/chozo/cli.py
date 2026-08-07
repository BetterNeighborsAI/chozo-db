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
from pathlib import Path

from chozo import config, discovery, history, integrity, migrator, registry, sync
from chozo.constants import APP_NAME, APP_TAGLINE, EXIT_FAIL, EXIT_NOTHING_TO_DO, EXIT_OK
from chozo.discovery import Migration
from chozo.history import FileHistoryStore, HistoryStore
from chozo.output import Output

_NAME_SANITIZE = re.compile(r"[^a-z0-9_]+")


@dataclass
class Ctx:
    """Runtime context. `cfg` resolves the active project lazily, so registry-only
    commands (register/projects/use/...) never trigger project resolution."""

    out: Output
    resolver: Callable[[], config.ProjectConfig]
    _cfg: config.ProjectConfig | None = None

    @property
    def cfg(self) -> config.ProjectConfig:
        if self._cfg is None:
            self._cfg = self.resolver()
        return self._cfg


def _local_store(ctx: Ctx) -> FileHistoryStore:
    return FileHistoryStore(ctx.cfg.history_path)


def _store(ctx: Ctx) -> HistoryStore:
    local = _local_store(ctx)
    remote = sync.remote_from_config(ctx.cfg)
    if remote is None:
        return local
    return sync.SyncedHistoryStore(local, remote, warn=ctx.out.warning)


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
    # init creates a *new* project in the cwd, independent of registry resolution.
    root = Path.cwd().resolve()
    config_file = root / "chozo.toml"
    if config_file.exists() and not args.force:
        ctx.out.error(f"{config_file} already exists (use --force to overwrite).")
        return EXIT_FAIL
    migrations_dir = root / (args.migrations_dir or "migrations")
    migrations_dir.mkdir(parents=True, exist_ok=True)
    name = args.name or root.name
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

# One shared bucket for every project; history stays isolated per project
# at gs://<bucket>/<project-slug>/history.json (override with `path`).
# [sync]
# bucket = "chozo-migrations"
'''
    )
    ctx.out.success(f"Initialized chozo project '{name}' at {root}")
    ctx.out.note(f"Migrations dir: {migrations_dir}")
    ctx.out.note("Set DATABASE_URL_LOCAL / _DEV / or DATABASE_URL to connect.")
    ctx.out.note("Run `chozo register` to add this project to the ~/.chozo registry.")
    if args.json:
        ctx.out.emit_json(
            {
                "status": "initialized",
                "name": name,
                "root": str(root),
                "migrations_dir": str(migrations_dir),
            }
        )
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
    if args.oneoff:
        # One-way flat file for inserts / data fixes. Tracked by content hash,
        # so it cannot silently re-run even if renamed.
        path = cfg.migrations_dir / f"{name}.sql"
        path.write_text(
            f'-- one-off: {name}\n-- Purpose: [what and why]\n-- Rollback: [how to undo by hand, or "not reversible"]\n\n'
        )
        ctx.out.success(f"Created {name}.sql (one-off, no automatic rollback)")
        ctx.out.note(str(path))
        if args.json:
            ctx.out.emit_json({"status": "created", "migration": f"{name}.sql", "kind": "oneoff", "up": str(path)})
        return EXIT_OK
    mig_dir = cfg.migrations_dir / name
    mig_dir.mkdir(parents=True, exist_ok=True)
    (mig_dir / "up.sql").write_text(f"-- migration: {name} (up)\n-- describe the change here\n\n")
    (mig_dir / "down.sql").write_text(f"-- migration: {name} (down)\n-- reverse the change here\n\n")
    ctx.out.success(f"Created {name}/")
    ctx.out.note(str(mig_dir / "up.sql"))
    ctx.out.note(str(mig_dir / "down.sql"))
    if args.json:
        ctx.out.emit_json(
            {
                "status": "created",
                "migration": name,
                "up": str(mig_dir / "up.sql"),
                "down": str(mig_dir / "down.sql"),
            }
        )
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
    selector: Callable[[dict], list[Migration] | None],
    dry_run: bool,
    yes: bool,
    confirm_prod: bool,
    json_out: bool,
    history_env: str | None = None,
    record_history: bool = True,
    target: str | None = None,
    allow_rerun: bool = False,
) -> int:
    cfg = ctx.cfg
    url = _env_url(ctx, env)
    ctx.out.env_badge(env, cfg.envs.get(env, ""), config_target(url))

    store = _store(ctx)
    hist = history.load(store)
    history_key = history_env or env
    targets = selector(hist)
    if targets is None:
        message = f"No migration matches '{target}'" if target else "Migration selector matched nothing."
        ctx.out.error(message)
        if json_out:
            ctx.out.emit_json(
                {
                    "status": "failed",
                    "env": env,
                    "history_env": history_key,
                    "record_history": record_history,
                    "dry_run": dry_run,
                    "migrations": [],
                    "error": message,
                    "summary": {"applied": 0, "failed": 0, "skipped": 0},
                }
            )
        return EXIT_FAIL
    if not targets:
        ctx.out.info(f"No pending migrations for {env}.")
        if json_out:
            ctx.out.emit_json(
                {
                    "status": "success",
                    "env": env,
                    "history_env": history_key,
                    "record_history": record_history,
                    "dry_run": dry_run,
                    "migrations": [],
                    "summary": {"applied": 0, "failed": 0, "skipped": 0},
                }
            )
        return EXIT_NOTHING_TO_DO

    if not _prod_gate(env, dry_run, confirm_prod, ctx.out):
        if json_out:
            ctx.out.emit_json(
                {
                    "status": "failed",
                    "env": env,
                    "history_env": history_key,
                    "record_history": record_history,
                    "dry_run": dry_run,
                    "migrations": [],
                    "error": "prod requires --confirm-prod",
                    "summary": {"applied": 0, "failed": 0, "skipped": 0},
                }
            )
        return EXIT_FAIL

    if not yes and not ctx.out.quiet:
        ctx.out.pending_table([(m.name, m.up.stat().st_size) for m in targets])
        if not ctx.out.confirm(f"Apply {len(targets)} pending migration(s) to {env}?", default=False):
            ctx.out.info("Aborted.")
            return EXIT_FAIL

    return _execute_up(
        ctx,
        env,
        url,
        hist,
        store,
        targets,
        dry_run=dry_run,
        json_out=json_out,
        history_env=history_key,
        record_history=record_history,
        yes=yes,
        allow_rerun=allow_rerun,
    )


def _execute_up(
    ctx: Ctx,
    env: str,
    url: str,
    hist: dict,
    store: HistoryStore,
    targets: list[Migration],
    *,
    dry_run: bool,
    json_out: bool,
    history_env: str,
    record_history: bool,
    yes: bool,
    allow_rerun: bool,
) -> int:
    results: list[dict] = []
    applied = failed = skipped = 0
    for mig in targets:
        digest = integrity.content_hash(mig)
        # Rename detection: identical content already applied under a different
        # name (e.g. a renamed one-off data upload). Never silently re-run it.
        if not dry_run:
            dup = integrity.find_duplicate(digest, hist, history_env, mig.name)
            if dup is not None and not allow_rerun:
                old, at = dup["name"], dup["applied_at"]
                if yes or ctx.out.quiet:
                    skipped += 1
                    entry = {
                        "file": mig.name,
                        "result": "blocked",
                        "elapsed_s": 0,
                        "error": f"identical content already applied as '{old}' on {at}; pass --allow-rerun to force",
                    }
                    results.append(entry)
                    ctx.out.warning(
                        f"Blocked {mig.name}: identical content already applied as '{old}' (pass --allow-rerun to force)."
                    )
                    continue
                if not ctx.out.confirm(
                    f"'{mig.name}' has identical content to already-applied '{old}' (applied {at}). Run again as '{mig.name}'?",
                    default=False,
                ):
                    skipped += 1
                    results.append({"file": mig.name, "result": "skipped", "elapsed_s": 0})
                    ctx.out.info(f"Skipped {mig.name}")
                    continue
        res = migrator.execute(url, mig, dry_run=dry_run)
        entry = {
            "file": mig.name,
            "result": res.status,
            "elapsed_s": round(res.duration, 3) if res.duration else 0,
        }
        if res.status == "applied":
            if not dry_run and record_history:
                history.record(
                    store,
                    hist,
                    history_env,
                    mig.name,
                    method="executed",
                    duration_seconds=res.duration,
                    content_hash=digest,
                )
            applied += 1
            ctx.out.success(f"Applied {mig.name}" + (f" [{res.duration:.2f}s]" if res.duration else ""))
        elif res.status == "blocked":
            skipped += 1
            entry["error"] = res.error
            ctx.out.warning(f"Blocked {mig.name}: {res.error}")
        else:
            failed += 1
            entry["error"] = res.error
            if not dry_run and record_history:
                history.record(
                    store,
                    hist,
                    history_env,
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
                "history_env": history_env,
                "record_history": record_history,
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

    def selector(hist: dict) -> list[Migration] | None:
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
        allow_rerun=args.allow_rerun,
    )


def cmd_run(args: argparse.Namespace, ctx: Ctx) -> int:
    history_key = args.history_env or args.env

    def selector(hist: dict) -> list[Migration] | None:
        all_migs = discovery.discover(ctx.cfg.migrations_dir)
        names = [m.name for m in all_migs]
        pending_names = history.get_pending(history_key, hist, names)
        if args.target == "all":
            return _select_up_targets(all_migs, pending_names, target="all", to=None)
        matches = [m for m in all_migs if fnmatch(m.name, args.target)]
        if not matches:
            return None
        if args.dry_run:
            # Match migracli: a targeted dry-run may re-run an applied migration.
            return matches
        pending_set = set(pending_names)
        return [m for m in matches if m.name in pending_set]

    return _run_up(
        ctx,
        args.env,
        selector=selector,
        dry_run=args.dry_run,
        yes=True,
        confirm_prod=args.confirm_prod,
        json_out=args.json,
        history_env=history_key,
        record_history=not args.no_record_history,
        target=args.target,
        allow_rerun=args.allow_rerun,
    )


# --- exec: tracked one-off scripts (inserts, data fixes) ---


def cmd_exec(args: argparse.Namespace, ctx: Ctx) -> int:
    """Apply an arbitrary SQL file once, recorded in history with who/when/hash.

    Unlike migrations, exec'd files live outside the migrations flow: they never
    appear in `up`/`status`, but the history entry (with content hash) prevents
    silently re-running the same script — even under a different file name.
    """
    file = Path(args.file).expanduser().resolve()
    if not file.is_file():
        ctx.out.error(f"no such file: {file}")
        if args.json:
            ctx.out.emit_json({"status": "failed", "error": f"no such file: {file}"})
        return EXIT_FAIL

    cfg = ctx.cfg
    url = _env_url(ctx, args.env)
    ctx.out.env_badge(args.env, cfg.envs.get(args.env, ""), config_target(url))

    store = _store(ctx)
    hist = history.load(store)
    mig = Migration(name=file.name, number=0, dir=file.parent, up=file, down=None)
    digest = integrity.content_hash(mig)

    if not args.dry_run and not args.allow_rerun:
        existing = hist.get(args.env, {}).get(mig.name)
        if isinstance(existing, dict) and existing.get("applied_at"):
            at = (existing.get("applied_at") or "")[:19].replace("T", " ")
            by = existing.get("applied_by") or "?"
            message = f"{mig.name} was already applied to {args.env} on {at} by {by}; pass --allow-rerun to force."
            ctx.out.warning(message)
            if args.json:
                ctx.out.emit_json(
                    {
                        "status": "blocked",
                        "env": args.env,
                        "file": mig.name,
                        "error": message,
                        "applied_at": existing.get("applied_at"),
                        "applied_by": existing.get("applied_by"),
                    }
                )
            return EXIT_FAIL
        dup = integrity.find_duplicate(digest, hist, args.env, mig.name)
        if dup is not None:
            message = (
                f"identical content already applied as '{dup['name']}' on {dup['applied_at']}; "
                "pass --allow-rerun to force."
            )
            ctx.out.warning(message)
            if args.json:
                ctx.out.emit_json({"status": "blocked", "env": args.env, "file": mig.name, "error": message})
            return EXIT_FAIL

    if not _prod_gate(args.env, args.dry_run, args.confirm_prod, ctx.out):
        if args.json:
            ctx.out.emit_json(
                {"status": "failed", "env": args.env, "file": mig.name, "error": "prod requires --confirm-prod"}
            )
        return EXIT_FAIL

    if not args.yes and not ctx.out.quiet and not args.dry_run:
        ctx.out.sql_preview(file.read_text())
        if not ctx.out.confirm(f"Execute {mig.name} against {args.env}?", default=False):
            ctx.out.info("Aborted.")
            return EXIT_FAIL

    res = migrator.execute(url, mig, dry_run=args.dry_run)
    if res.status == "applied":
        if not args.dry_run:
            history.record(
                store, hist, args.env, mig.name, method="exec", duration_seconds=res.duration, content_hash=digest
            )
        ctx.out.success(f"Executed {mig.name}" + (f" [{res.duration:.2f}s]" if res.duration else ""))
    elif res.status == "blocked":
        ctx.out.warning(f"Blocked {mig.name}: {res.error}")
    else:
        if not args.dry_run:
            history.record(
                store,
                hist,
                args.env,
                mig.name,
                method="exec",
                duration_seconds=res.duration,
                success=False,
                error=res.error,
            )
        ctx.out.error(f"Failed {mig.name}: {res.error}")

    if args.json:
        ctx.out.emit_json(
            {
                "status": "success" if res.status == "applied" else "failed",
                "env": args.env,
                "file": mig.name,
                "result": res.status,
                "dry_run": args.dry_run,
                "elapsed_s": round(res.duration, 3) if res.duration else 0,
                "error": res.error,
            }
        )
    return EXIT_OK if res.status == "applied" else EXIT_FAIL


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
        if args.json:
            ctx.out.emit_json(
                {
                    "status": "failed",
                    "env": args.env,
                    "dry_run": args.dry_run,
                    "migrations": [],
                    "error": "prod requires --confirm-prod",
                    "summary": {"rolled_back": 0, "failed": 0},
                }
            )
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
                history.record_rollback(store, hist, args.env, mig.name, duration_seconds=res.duration)
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
    # Drift: applied migrations whose on-disk content no longer matches the
    # hash recorded when they were applied.
    by_name = {m.name: m for m in all_migs}
    drift = [
        name
        for name in names
        if name not in pending
        and integrity.find_drift(by_name[name], integrity.content_hash(by_name[name]), hist, args.env)
    ]
    if args.json:
        meta = hist.get("_meta", {})
        ctx.out.emit_json(
            {
                "env": args.env,
                "applied": [n for n in names if n not in pending],
                "pending": pending,
                "drift": drift,
                "last_synced_at": meta.get("last_synced_at"),
                "last_synced_by": meta.get("last_synced_by"),
            }
        )
        return EXIT_OK
    ctx.out.status_tree(args.env, names, applied, pending)
    for name in drift:
        ctx.out.warning(f"{name}: content changed since it was applied.")
    ctx.out.note(f"Total {len(names)}  Applied {len(names) - len(pending)}  Pending {len(pending)}")
    meta = hist.get("_meta", {})
    if meta.get("last_synced_at"):
        sync_at = meta["last_synced_at"][:19].replace("T", " ")
        ctx.out.note(f"GCS synced {sync_at} by {meta.get('last_synced_by', '?')}")
    return EXIT_OK


def cmd_mark(args: argparse.Namespace, ctx: Ctx) -> int:
    store = _store(ctx)
    hist = history.load(store)
    all_migs = discovery.discover(ctx.cfg.migrations_dir)
    by_name = {m.name: m for m in all_migs}
    names = [m.name for m in all_migs]
    pending = history.get_pending(args.env, hist, names)
    targets = [n for n in pending if fnmatch(n, args.pattern or "*")] if args.pattern else pending
    if not targets:
        ctx.out.success(f"No pending migrations to mark for {args.env}.")
        if args.json:
            ctx.out.emit_json({"status": "success", "env": args.env, "marked": []})
        return EXIT_OK
    if (
        not args.yes
        and not ctx.out.quiet
        and not ctx.out.confirm(f"Mark {len(targets)} migration(s) as applied in {args.env}?", default=False)
    ):
        ctx.out.info("Aborted.")
        return EXIT_FAIL
    for name in targets:
        history.record(store, hist, args.env, name, method="marked", content_hash=integrity.content_hash(by_name[name]))
        ctx.out.success(f"Marked {name}")
    ctx.out.summary(len(targets), len(targets), args.env, verb="marked")
    if args.json:
        ctx.out.emit_json({"status": "success", "env": args.env, "marked": targets})
    return EXIT_OK


def cmd_unmark(args: argparse.Namespace, ctx: Ctx) -> int:
    store = _store(ctx)
    hist = history.load(store)
    if not history.remove(store, hist, args.env, args.name):
        ctx.out.warning(f"{args.name} not found in {args.env} history.")
        if args.json:
            ctx.out.emit_json({"status": "success", "env": args.env, "unmarked": [], "not_found": args.name})
        return EXIT_OK
    ctx.out.success(f"Removed {args.name} from {args.env} history.")
    if args.json:
        ctx.out.emit_json({"status": "success", "env": args.env, "unmarked": [args.name]})
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


def cmd_sync(args: argparse.Namespace, ctx: Ctx) -> int:
    remote = sync.remote_from_config(ctx.cfg)
    if remote is None:
        message = f"GCS sync is not configured; set {sync.GCS_BUCKET_ENV} or add a [sync] section to chozo.toml."
        ctx.out.warning(message)
        if args.json:
            ctx.out.emit_json({"status": "not_configured", "error": message})
        return EXIT_NOTHING_TO_DO
    result = sync.synchronize(_local_store(ctx), remote)
    payload = {
        "status": "synced",
        "remote": sync.remote_uri(remote),
        "remote_found": result.remote_found,
        "last_synced_at": result.history.get("_meta", {}).get("last_synced_at"),
        "last_synced_by": result.history.get("_meta", {}).get("last_synced_by"),
    }
    if args.json:
        ctx.out.emit_json(payload)
    else:
        ctx.out.success(f"Migration history synchronized with {sync.remote_uri(remote)}")
    return EXIT_OK


def config_target(url: str) -> str:
    from chozo.connection import format_target

    return format_target(url)


# --- registry commands (operate on the registry itself; no project resolution) ---


def cmd_register(args: argparse.Namespace, ctx: Ctx) -> int:
    root = Path(args.path).expanduser() if args.path else Path.cwd()
    try:
        result = registry.register(root, name=args.name, slug=args.slug)
    except FileNotFoundError as exc:
        ctx.out.error(str(exc))
        return EXIT_FAIL
    if args.json:
        ctx.out.emit_json({"status": "registered", **result})
        return EXIT_OK
    ctx.out.success(f"Registered project '{result['name']}' as slug '{result['slug']}'")
    ctx.out.note(f"Root: {result['root']}")
    if result["history_migrated"]:
        ctx.out.note("Migrated existing local history into the registry (archived migrations/_history.json).")
    return EXIT_OK


def cmd_unregister(args: argparse.Namespace, ctx: Ctx) -> int:
    if (
        not args.yes
        and not ctx.out.quiet
        and not ctx.out.confirm(
            f"Unregister '{args.slug}' and delete its registry history? Source files are never touched.", default=False
        )
    ):
        ctx.out.info("Aborted.")
        return EXIT_FAIL
    if not registry.unregister(args.slug):
        ctx.out.error(f"unknown project slug '{args.slug}'.")
        return EXIT_FAIL
    if args.json:
        ctx.out.emit_json({"status": "unregistered", "slug": args.slug})
        return EXIT_OK
    ctx.out.success(f"Unregistered '{args.slug}' (registry entry + history removed; source untouched).")
    return EXIT_OK


def cmd_projects(args: argparse.Namespace, ctx: Ctx) -> int:
    rows = registry.list_projects()
    if args.json:
        ctx.out.emit_json({"projects": rows})
        return EXIT_OK
    if not rows:
        ctx.out.info("No projects registered. Run `chozo register` inside a project to add it.")
        return EXIT_OK
    for row in rows:
        marker = "*" if row["current"] else " "
        state = "" if row["exists"] else "  [missing root]"
        ctx.out.note(f"{marker} {row['slug']:<24} {row['name'] or '':<20} {row['root']}{state}")
    return EXIT_OK


def cmd_use(args: argparse.Namespace, ctx: Ctx) -> int:
    if not registry.set_current(args.slug):
        ctx.out.error(f"unknown project slug '{args.slug}'.")
        return EXIT_FAIL
    if args.json:
        ctx.out.emit_json({"status": "current", "slug": args.slug})
        return EXIT_OK
    ctx.out.success(f"Current project set to '{args.slug}'.")
    return EXIT_OK


def cmd_which(args: argparse.Namespace, ctx: Ctx) -> int:
    cfg = ctx.cfg  # resolves the active project
    payload = {
        "slug": cfg.slug,
        "name": cfg.name,
        "root": str(cfg.root),
        "registered": cfg.registered,
        "migrations_dir": str(cfg.migrations_dir),
        "history_path": str(cfg.history_path),
        "envs": cfg.envs,
        "sync": sync.remote_uri(remote) if (remote := sync.remote_from_config(cfg)) else None,
    }
    if args.json:
        ctx.out.emit_json(payload)
        return EXIT_OK
    scope = f"registered (slug '{cfg.slug}')" if cfg.registered else "local (unregistered)"
    ctx.out.note(f"Project: {cfg.name}  [{scope}]")
    ctx.out.note(f"Root: {cfg.root}")
    ctx.out.note(f"Migrations: {cfg.migrations_dir}")
    ctx.out.note(f"History: {cfg.history_path}")
    remote = sync.remote_from_config(cfg)
    if remote is not None:
        ctx.out.note(f"Sync: {sync.remote_uri(remote)}")
    return EXIT_OK


# --- parser ---


def _add_env(sp: argparse.ArgumentParser, required: bool = False) -> None:
    sp.add_argument("--env", required=required, help="Environment (from chozo.toml [envs]).")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Agent-friendly PostgreSQL + SQLite migrations + schema inspection.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--json", action="store_true", dest="json_out", help="Emit JSON to stdout (agent mode).")
    p.add_argument(
        "--project",
        metavar="SLUG",
        help="Project slug from the ~/.chozo registry. Overrides cwd/current resolution (agents use this).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("init", help="Create chozo.toml + migrations dir.")
    sp.add_argument("--name", help="Project name (defaults to directory name).")
    sp.add_argument("--migrations-dir", default="migrations", help="Migrations directory (relative to project root).")
    sp.add_argument("--force", action="store_true", help="Overwrite an existing chozo.toml.")
    sp.add_argument("--json", action="store_true", dest="json", help="Emit JSON to stdout.")

    sp = sub.add_parser("new", help="Create a new migration (UP + DOWN).")
    sp.add_argument("name", help="Snake_case migration name, e.g. add_users_table.")
    sp.add_argument(
        "--oneoff",
        action="store_true",
        help="Create a one-way flat file (insert / data fix) instead of an UP+DOWN directory.",
    )
    sp.add_argument("--json", action="store_true", dest="json", help="Emit JSON to stdout.")

    sp = sub.add_parser("exec", help="Execute an arbitrary SQL file once, tracked in history (inserts, one-offs).")
    sp.add_argument("file", help="Path to the SQL file to execute.")
    _add_env(sp, required=True)
    sp.add_argument("--dry-run", action="store_true", help="Execute then ROLLBACK; record nothing.")
    sp.add_argument("--yes", "-y", action="store_true", help="No confirmation prompt.")
    sp.add_argument("--confirm-prod", action="store_true", help="Consent flag required for --env prod.")
    sp.add_argument(
        "--allow-rerun",
        action="store_true",
        help="Allow re-running a script (or identical content) already applied.",
    )
    sp.add_argument("--json", action="store_true", dest="json", help="Emit JSON to stdout.")

    sp = sub.add_parser("up", help="Apply pending migrations.")
    _add_env(sp)
    sp.add_argument("--to", metavar="NAME", help="Apply up to (and including) the migration matching NAME.")
    sp.add_argument("--yes", "-y", action="store_true", help="No confirmation prompt.")
    sp.add_argument("--dry-run", action="store_true", help="Execute then ROLLBACK; record nothing.")
    sp.add_argument("--confirm-prod", action="store_true", help="Consent flag required for --env prod.")
    sp.add_argument(
        "--allow-rerun",
        action="store_true",
        help="Allow re-running content already applied under a different name (e.g. a renamed one-off).",
    )
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
    sp.add_argument("--json", action="store_true", dest="json", help="Emit JSON to stdout.")

    sp = sub.add_parser("unmark", help="Remove a migration from history.")
    sp.add_argument("name", help="Migration directory name.")
    _add_env(sp, required=True)
    sp.add_argument("--json", action="store_true", dest="json", help="Emit JSON to stdout.")

    sp = sub.add_parser("history", help="Show the event trail for an environment.")
    _add_env(sp, required=True)
    sp.add_argument("--json", action="store_true", dest="json", help="Emit JSON to stdout.")

    sp = sub.add_parser("inspect", help="Reflect the live schema as JSON (agents).")
    _add_env(sp, required=True)
    sp.add_argument("--json", action="store_true", dest="json", help="Emit JSON to stdout (default for inspect).")

    sp = sub.add_parser("sync", help="Merge local migration history with the configured GCS state.")
    sp.add_argument("--json", action="store_true", dest="json", help="Emit JSON to stdout.")

    sp = sub.add_parser("run", help="Non-interactive apply for agents/CI.")
    sp.add_argument("target", help="'all', a glob, or a migration name.")
    _add_env(sp, required=True)
    sp.add_argument("--dry-run", action="store_true", help="Execute then ROLLBACK; record nothing.")
    sp.add_argument("--confirm-prod", action="store_true", help="Consent flag required for --env prod.")
    sp.add_argument(
        "--yes", "-y", action="store_true", help="Accepted for migracli compatibility; run is non-interactive."
    )
    sp.add_argument(
        "--history-env",
        help="Use another environment's history for pending checks (requires --no-record-history).",
    )
    sp.add_argument(
        "--no-record-history",
        action="store_true",
        help="Execute without writing migration history (useful for disposable clones).",
    )
    sp.add_argument(
        "--allow-rerun",
        action="store_true",
        help="Allow re-running content already applied under a different name (e.g. a renamed one-off).",
    )
    sp.add_argument("--json", action="store_true", dest="json", help="Emit JSON to stdout.")

    sp = sub.add_parser("register", help="Register the current project in the ~/.chozo registry.")
    sp.add_argument("--path", help="Project root to register (defaults to cwd).")
    sp.add_argument("--name", help="Project name (defaults to chozo.toml name or dir name).")
    sp.add_argument("--slug", help="Override the generated slug.")
    sp.add_argument("--json", action="store_true", dest="json", help="Emit JSON to stdout.")

    sp = sub.add_parser("unregister", help="Remove a project from the registry (never touches source).")
    sp.add_argument("slug", help="Project slug to remove.")
    sp.add_argument("--yes", "-y", action="store_true", help="No confirmation prompt.")
    sp.add_argument("--json", action="store_true", dest="json", help="Emit JSON to stdout.")

    sp = sub.add_parser("projects", help="List registered projects.")
    sp.add_argument("--json", action="store_true", dest="json", help="Emit JSON to stdout.")

    sp = sub.add_parser("use", help="Set the current project (default when cwd matches nothing).")
    sp.add_argument("slug", help="Project slug to make current.")
    sp.add_argument("--json", action="store_true", dest="json", help="Emit JSON to stdout.")

    sp = sub.add_parser("which", help="Show the active project chozo resolved.")
    sp.add_argument("--json", action="store_true", dest="json", help="Emit JSON to stdout.")

    return p


_HANDLERS = {
    "init": cmd_init,
    "new": cmd_new,
    "exec": cmd_exec,
    "up": cmd_up,
    "down": cmd_down,
    "rollback": cmd_down,
    "status": cmd_status,
    "mark": cmd_mark,
    "unmark": cmd_unmark,
    "history": cmd_history,
    "inspect": cmd_inspect,
    "sync": cmd_sync,
    "run": cmd_run,
    "register": cmd_register,
    "unregister": cmd_unregister,
    "projects": cmd_projects,
    "use": cmd_use,
    "which": cmd_which,
}


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.json = bool(args.json_out or getattr(args, "json", False))
    if args.command == "run" and args.history_env and not args.no_record_history:
        parser.error("run --history-env requires --no-record-history to avoid mutating another environment's history.")
    out = Output(quiet=args.json)

    def resolver() -> config.ProjectConfig:
        try:
            return registry.resolve(explicit_slug=args.project)
        except (KeyError, FileNotFoundError) as exc:
            out.error(str(exc))
            sys.exit(EXIT_FAIL)

    ctx = Ctx(out=out, resolver=resolver)

    out.banner(APP_TAGLINE)
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
