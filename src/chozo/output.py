"""Output layer (Rich).

`--json` sets `quiet=True`, suppressing every human helper and routing errors to
stderr so agents get clean JSON on stdout. The interactive helpers (confirm,
prompt) are no-ops when quiet — non-interactive callers must pass decisions via
flags, never by relying on a prompt.
"""

from __future__ import annotations

import json
import sys

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.tree import Tree


class Output:
    def __init__(self, quiet: bool = False) -> None:
        self.quiet = quiet
        self.console = Console()

    def success(self, msg: str) -> None:
        if not self.quiet:
            self.console.print(f"[green]✓[/green] {msg}")

    def error(self, msg: str) -> None:
        if self.quiet:
            print(f"ERROR: {msg}", file=sys.stderr)
        else:
            self.console.print(f"[red]✗[/red] {msg}")

    def warning(self, msg: str) -> None:
        if not self.quiet:
            self.console.print(f"[yellow]![/yellow] {msg}")

    def info(self, msg: str) -> None:
        if not self.quiet:
            self.console.print(f"[blue]→[/blue] {msg}")

    def note(self, msg: str) -> None:
        if not self.quiet:
            self.console.print(f"[dim]{msg}[/dim]")

    def banner(self, project_name: str) -> None:
        if self.quiet:
            return
        text = Text()
        text.append("chozo", style="bold cyan")
        text.append(" — ", style="dim")
        text.append(project_name, style="bold white")
        self.console.print(Panel(text, box=box.ROUNDED, border_style="cyan", padding=(0, 2)))

    def env_badge(self, env: str, env_var: str, target: str) -> None:
        if self.quiet:
            return
        color = "red" if env == "prod" else "blue"
        badge = Text()
        badge.append("ENV ", style="dim")
        badge.append(env.upper(), style=f"bold {color}")
        badge.append(f"  ({env_var} -> {target})", style="dim")
        self.console.print(Panel(badge, box=box.ROUNDED, border_style=color, padding=(0, 2)))

    def sql_preview(self, sql: str) -> None:
        if not self.quiet:
            self.console.print(Syntax(sql, "sql", theme="monokai", line_numbers=True))

    def pending_table(self, rows: list[tuple[str, int]]) -> None:
        if self.quiet:
            return
        table = Table(box=box.SIMPLE_HEAVY, border_style="dim")
        table.add_column("#", style="dim", justify="right", width=4)
        table.add_column("Migration", style="cyan")
        table.add_column("Size", style="dim", justify="right")
        for i, (name, size) in enumerate(rows, 1):
            size_str = f"{size} B" if size < 1024 else f"{size / 1024:.1f} KB"
            table.add_row(str(i), name, size_str)
        self.console.print(table)

    def status_tree(self, env: str, names: list[str], applied: dict, pending: list[str]) -> None:
        if self.quiet:
            return
        tree = Tree(Text.assemble(("Migration Status — ", "bold"), (env, "bold cyan")))
        applied_count = len(names) - len(pending)
        ap = tree.add(Text.assemble(("Applied", "bold green"), (f" ({applied_count})", "dim")))
        for name in names:
            if name in pending:
                continue
            entry = applied.get(name, {})
            at = (entry.get("applied_at") or "")[:19].replace("T", " ")
            label = Text()
            label.append("● ", style="green")
            label.append(name, style="cyan")
            if at:
                label.append(f"  {at}", style="dim")
            ap.add(label)
        pd = tree.add(Text.assemble(("Pending", "bold yellow"), (f" ({len(pending)})", "dim")))
        for name in pending:
            pd.add(Text.assemble(("○ ", "yellow"), (name, "cyan")))
        self.console.print(tree)

    def summary(self, completed: int, total: int, env: str, verb: str = "applied", skipped: int = 0) -> None:
        if self.quiet:
            return
        skip_str = f", {skipped} skipped" if skipped > 0 else ""
        color = "green" if completed == total and not skipped else "yellow"
        text = Text()
        text.append(str(completed), style=f"bold {color}")
        text.append(f"/{total} migrations {verb}{skip_str}", style="white")
        text.append(f" ({env})", style="dim")
        self.console.print(Panel(text, box=box.ROUNDED, border_style=color, padding=(0, 2)))

    def confirm(self, msg: str, default: bool = False) -> bool:
        if self.quiet:
            return False
        return Confirm.ask(msg, default=default, console=self.console)

    def prompt(self, msg: str, default: str | None = None) -> str:
        if self.quiet:
            return default or ""
        result = Prompt.ask(msg, default=default, console=self.console)
        return result if result is not None else (default or "")

    def emit_json(self, data: dict) -> None:
        print(json.dumps(data, indent=2))
