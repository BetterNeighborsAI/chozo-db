# chozo-db

Agent-friendly PostgreSQL + SQLite migrations + schema inspection CLI. Named after the Chozo of Metroid — the guardians of knowledge.

`chozo-db` runs plain-SQL migrations against Postgres **or** SQLite, tracks a per-project history with a full event trail, blocks destructive operations by default, and speaks JSON so AI agents and CI can drive it without a TTY. It generalizes a battle-tested in-house runner into an installable, cross-project tool. SQLite support (Python's stdlib, no server) makes it usable for local, serverless projects.

## Status

**In progress.** The migracli runner surface and multi-project registry are
available, including legacy flat-file compatibility and optional GCS history
sync. ORM-aware autogeneration lands next.

## Install

```bash
uv tool install chozo-db
# or
pipx install chozo-db
```

Requires PostgreSQL for the Postgres backend; SQLite needs nothing extra (Python's stdlib). Connection strings come from environment variables (never stored in the repo).

## Quick start

```bash
chozo init                          # create chozo.toml + migrations/
chozo new add_users_table           # migrations/001_add_users_table/{up,down}.sql
chozo new backfill_slugs --oneoff   # migrations/002_backfill_slugs.sql (one-way data fix)
chozo up --env local                # apply pending migrations
chozo exec ./scripts/fix.sql --env dev   # run an arbitrary script once, tracked in history
chozo rollback --env local          # undo the last applied migration
chozo status --env dev              # applied vs pending
chozo inspect --env dev --json      # live schema as JSON (agents)
```

## Agent / CI mode

Every destructive or state-changing path is non-interactive when given the flags; output is stable JSON; exit codes are machine-friendly.

```bash
chozo run all --env dev --json                          # apply pending, print JSON
chozo run '003_*' --env dev --dry-run --json             # ROLLBACK, no history
chozo run all --env prod --confirm-prod --yes --json    # prod needs explicit consent
chozo run all --env local --history-env dev --no-record-history --json  # disposable clone
```

Exit codes: `0` success · `1` failure · `2` nothing to do.

## Destructive operations

`chozo` parses migration SQL and refuses to auto-apply anything matching `DROP TABLE`, `TRUNCATE`, `DELETE FROM` (unqualified), `DROP DATABASE`, or `DROP SCHEMA`. Dry runs are always allowed (they `ROLLBACK`). To run a blocked migration you must apply it by hand — there is no `--force` for destructive ops on purpose.

## SQLite: local, serverless projects

`chozo` also speaks SQLite (Python's stdlib, so there's no server to run). Point any env at a file URL and the same `up` / `rollback` / `status` / `inspect` commands work unchanged — the runner wraps each migration in a single transaction on both backends, and `--dry-run` rolls back identically (a failed multi-statement migration rolls back atomically).

```bash
export DATABASE_URL_LOCAL=sqlite:///./dev.db      # relative path
# or an absolute path (note the four slashes):
export DATABASE_URL_LOCAL=sqlite:////abs/path/dev.db

chozo up --env local --yes
chozo inspect --env local --json
```

Notes:
- Foreign keys are enforced (`PRAGMA foreign_keys = ON`), matching PostgreSQL semantics.
- `:memory:` is **not** supported for the apply → inspect lifecycle: each command opens a fresh connection, and an in-memory DB dies with its connection. Use a file path instead.
- Destructive detection (`DROP TABLE`, unqualified `DELETE FROM`, …) applies to SQLite too; `DROP DATABASE` / `DROP SCHEMA` simply never appear in SQLite.

## Migrations

Native Chozo migrations live in directories with UP and DOWN files:

```
migrations/
  001_add_users_table/
    up.sql
    down.sql
  002_add_email_index/
    up.sql
    down.sql
```

The runner owns transactions: it strips standalone `BEGIN/COMMIT/ROLLBACK` and wraps each migration in a single transaction, so every migration uses one code path.

Existing migracli-style `NNN_description.sql` files are discovered alongside
native migrations. They are treated as one-way migrations because they do not
have a `down.sql`; applying, status, marking, history, dry-run, and glob
selection all work without converting the repository. If an existing
`migration_history.json` is present, Chozo reuses it rather than creating a
separate `_history.json`. With no `chozo.toml`, the migracli
`sql/migrations/` layout is discovered automatically when walking up from the
current directory.

## One-offs and inserts

Data fixes and insert scripts are first-class, tracked citizens:

- `chozo new <name> --oneoff` scaffolds a one-way flat `NNN_<name>.sql` with a
  `-- Rollback:` comment convention for documenting manual undo.
- `chozo exec <file.sql> --env <env>` applies an arbitrary SQL file once. The
  run is recorded in history with **who** (`applied_by` + per-event `by`),
  **when** (`applied_at` + per-event `at`), duration, and a content hash.
  Re-running the same script — or the same content under a different file name —
  is blocked unless you pass `--allow-rerun`.

Both paths go through the destructive-operation gate and the prod gate, and
support `--dry-run` and `--json`.

## Configuration

`chozo.toml` in the project root:

```toml
[project]
name = "my-app"
migrations_dir = "migrations"

[envs.local]
url_var = "DATABASE_URL_LOCAL"
[envs.dev]
url_var = "DATABASE_URL_DEV"
[envs.prod]
url_var = "DATABASE_URL"
```

Without a `chozo.toml`, `chozo` falls back to `local`/`dev`/`prod` reading `DATABASE_URL_LOCAL`/`DATABASE_URL_DEV`/`DATABASE_URL`.

### Shared history sync (`[sync]`)

One shared GCS bucket holds every project's history, isolated per project:

```toml
[sync]
bucket = "chozo-migrations"
# path is optional — defaults to "<project-slug>/history.json"
```

With that, `pd-intelligence` syncs to `gs://chozo-migrations/pd-intelligence/history.json`
and every other registered project gets its own path in the same bucket. The
`GCS_MIGRATIONS_BUCKET`/`GCS_MIGRATIONS_PATH` env vars remain supported and
override `chozo.toml` (migracli-compatible, handy in CI).

## Multi-project registry (`~/.chozo`)

`chozo` can manage many projects on one machine, with history and credentials isolated per project. Registering a project moves its history into the machine registry (source files are never touched).

```bash
chozo register                          # register the current project
chozo register --path ~/work/api        # register another directory
chozo projects                          # list registered projects
chozo use api                           # set a default for outside any project
chozo which                             # show the project chozo resolved
chozo unregister api                    # remove from registry (source untouched)
```

Layout:

```
~/.chozo/
  registry.json                # index: slug -> {name, root, ...}, plus "current"
  projects/
    <slug>/
      project.json             # registered metadata
      history.json             # that project's migration history (v2)
```

**Isolation is structural.** Every command resolves exactly one project and reads only that project's history, env mapping, and migrations dir. Nothing crosses projects. Resolution order (first hit wins):

1. `--project <slug>` — explicit, what agents should use
2. cwd matches a registered root (longest match, like git discovers `.git`)
3. the `current` marker (set by `chozo use <slug>`)
4. a local, unregistered project (its own `chozo.toml` / defaults, history in `migrations/_history.json`)

Set `CHOZO_HOME` to relocate the registry (used by tests).

## Shared history sync

Chozo retains migracli's optional GCS state synchronization, configured via
`[sync]` in `chozo.toml` (see above) or the same environment variables:

```bash
uv tool install 'chozo-db[gcs]'
export GCS_MIGRATIONS_BUCKET=chozo-migrations   # overrides chozo.toml
chozo sync --json
```

When configured, history is merged from GCS on load and pushed after local
saves — automatically, on every command. A cloud outage never discards a
successful local history write. `chozo status` shows when and by whom the
history was last synced, and `chozo which` shows the resolved `gs://` target.

## Roadmap

- [x] Engine: run / rollback / status / mark / unmark / dry-run / `--json` / destructive detection
- [x] Migracli compatibility: flat SQL files / GCS sync / clone history mode
- [x] `chozo inspect` — reflect live schema as JSON
- [x] `~/.chozo` multi-project registry with per-project history + credential isolation
- [x] SQLite backend (stdlib `sqlite3`) for serverless local projects
- [x] One-offs & inserts: `chozo new --oneoff` + `chozo exec` with run-once content-hash tracking
- [x] `[sync]` bucket config with per-project remote paths (`<slug>/history.json`)
- [ ] `chozo analyze` / `chozo diff` — Alembic-style autogenerate from SQLAlchemy/SQLModel models

## Development

```bash
uv sync
uv run chozo --help
uv run ruff check .
uv run pyright
uv run pytest
```

## License

MIT — see [LICENSE](LICENSE).
