# chozo-db

Agent-friendly PostgreSQL migrations + schema inspection CLI. Named after the Chozo of Metroid — the guardians of knowledge.

`chozo-db` runs plain-SQL migrations against Postgres, tracks a per-project history with a full event trail, blocks destructive operations by default, and speaks JSON so AI agents and CI can drive it without a TTY. It generalizes a battle-tested in-house runner into an installable, cross-project tool.

## Status

**In progress.** Engine port first; autogenerate and the multi-project registry land next.

## Install

```bash
uv tool install chozo-db
# or
pipx install chozo-db
```

Requires PostgreSQL. Connection strings come from environment variables (never stored in the repo).

## Quick start

```bash
chozo init                          # create chozo.toml + migrations/
chozo new add_users_table           # migrations/001_add_users_table/{up,down}.sql
chozo up --env local                # apply pending migrations
chozo rollback --env local          # undo the last applied migration
chozo status --env dev              # applied vs pending
chozo inspect --env dev --json      # live schema as JSON (agents)
```

## Agent / CI mode

Every destructive or state-changing path is non-interactive when given the flags; output is stable JSON; exit codes are machine-friendly.

```bash
chozo run all --env dev --json                          # apply pending, print JSON
chozo run 003_*.sql --env dev --dry-run --json          # ROLLBACK, no history
chozo run all --env prod --confirm-prod --yes --json    # prod needs explicit consent
```

Exit codes: `0` success · `1` failure · `2` nothing to do.

## Destructive operations

`chozo` parses migration SQL and refuses to auto-apply anything matching `DROP TABLE`, `TRUNCATE`, `DELETE FROM` (unqualified), `DROP DATABASE`, or `DROP SCHEMA`. Dry runs are always allowed (they `ROLLBACK`). To run a blocked migration you must apply it by hand — there is no `--force` for destructive ops on purpose.

## Migrations

Each migration lives in its own directory with an UP and a DOWN file:

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

## Roadmap

- [x] Engine: run / rollback / status / mark / unmark / dry-run / `--json` / destructive detection
- [ ] `chozo inspect` — reflect live schema as JSON
- [ ] `chozo analyze` / `chozo diff` — Alembic-style autogenerate from SQLAlchemy/SQLModel models
- [ ] `~/.chozo` multi-project registry with per-project history + credential isolation

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