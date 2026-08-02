# chozo-db

Agent-friendly PostgreSQL migration and schema inspection CLI.

Named after the Chozo of Metroid — the guardians of knowledge.

## Status

Scaffold only. No implementation yet.

## Planned commands

```
chozo-db init        Create config + migrations directory
chozo-db new <name>  Create a timestamped UP/DOWN SQL migration
chozo-db up          Apply pending migrations
chozo-db down        Roll back the last batch
chozo-db status      Show applied vs pending migrations
chozo-db inspect     Dump tables/columns/indexes as JSON
```

## Roadmap

- PostgreSQL migrations with timestamped UP/DOWN SQL files
- Safety checks on destructive operations (DROP, TRUNCATE, DELETE)
- Schema inspection (`inspect`) as machine-readable JSON for agents
- Non-interactive, stable-exit-code output so AI agents can drive it
- More database dialects as needed

## Development

Requires [Bun](https://bun.sh) (runtime + test runner) and TypeScript.

```bash
bun install
bun run dev -- help
bun test
bun run typecheck
```

## License

MIT — see [LICENSE](LICENSE).
