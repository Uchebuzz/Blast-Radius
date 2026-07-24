"""`blastradius` command-line interface."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import typer
from rich.console import Console

from .analyzer import analyze, resolve_dataset
from .datahub_client import DataHubClient, MockDataHubClient
from .models import ChangeType
from .pr import draft_migration
from .report import render_console, render_markdown, render_mermaid

# Windows terminals default to cp1252, which can't encode the emoji / box chars
# used in reports. Force UTF-8 (replacing anything truly unencodable) so output
# never crashes regardless of the host console.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # pragma: no cover - non-reconfigurable stream
        pass


def _load_dotenv() -> None:
    """Load a nearby .env so `--explain` and the live DataHub client find their keys.

    Walks up from the current directory to the first `.env`, loads any KEY=value
    pairs that aren't already set in the environment, and aliases the common
    ANTHROPIC_KEY -> ANTHROPIC_API_KEY (the name the Anthropic SDK expects).
    Real environment variables always win over the file.
    """
    for directory in (Path.cwd(), *Path.cwd().parents):
        env_path = directory / ".env"
        if env_path.is_file():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
            break
    if "ANTHROPIC_API_KEY" not in os.environ and "ANTHROPIC_KEY" in os.environ:
        os.environ["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_KEY"]


_load_dotenv()

app = typer.Typer(
    add_completion=False,
    help="💥 Blast Radius — see what breaks before you ship a schema change.",
)
console = Console()


def _client(fixture: str | None) -> DataHubClient:
    if fixture:
        return MockDataHubClient(fixture)
    return DataHubClient.from_env()


def _print_explanation(report) -> None:
    """Render the optional Claude narrative, degrading gracefully on any failure."""
    from .explain import explain_report

    console.rule("[bold]AI explanation[/bold]")
    try:
        console.print(explain_report(report))
    except RuntimeError as exc:  # anthropic not installed
        console.print(f"[yellow]{exc}[/yellow]")
    except Exception as exc:  # noqa: BLE001 - surface any API/auth/network error, don't crash
        console.print(
            f"[yellow]Could not generate the AI explanation ({type(exc).__name__}: {exc}).\n"
            "The deterministic report above is unaffected. Set ANTHROPIC_API_KEY to enable it."
            "[/yellow]"
        )


def _write_back(report, client) -> None:
    """Tag impacted datasets in DataHub with the verdict, degrading gracefully."""
    from .datahub_client import McpDataHubClient
    from .writeback import write_back

    console.rule("[bold]Write-back to DataHub[/bold]")
    if not isinstance(client, McpDataHubClient):
        console.print(
            "[yellow]Write-back needs a live DataHub analysis — set DATAHUB_GMS_URL "
            "and don't pass --fixture.[/yellow]"
        )
        return
    try:
        written = write_back(report)
    except RuntimeError as exc:  # no extra / no live DataHub
        console.print(f"[yellow]{exc}[/yellow]")
        return
    except Exception as exc:  # noqa: BLE001 - never crash the report on a write error
        console.print(f"[yellow]Write-back failed ({type(exc).__name__}: {exc}).[/yellow]")
        return

    if not written:
        console.print("[green]Nothing to tag — no breaking or at-risk assets.[/green]")
        return
    for name, tag in written:
        console.print(f"  [green]tagged[/green] {name} → [bold]{tag}[/bold]")
    console.print(
        f"[green]Wrote {len(written)} verdict tag(s) back to DataHub[/green] — "
        "the next person or agent inherits the knowledge."
    )


@app.command("analyze")
def analyze_cmd(
    dataset: str = typer.Argument(..., help="Dataset URN or name, e.g. raw.orders"),
    column: str = typer.Option(..., "--column", "-c", help="Column being changed"),
    change: ChangeType = typer.Option(ChangeType.DROP, "--change", help="drop | rename | retype"),
    new_name: str = typer.Option(None, "--new-name", help="New column name (for rename)"),
    new_type: str = typer.Option(None, "--new-type", help="New column type (for retype)"),
    max_depth: int = typer.Option(10, "--max-depth", help="Max lineage traversal depth"),
    fixture: str = typer.Option(
        None, "--fixture", help="Use a local JSON stack instead of live DataHub"
    ),
    output: str = typer.Option(
        "console", "--output", "-o", help="console | markdown | mermaid"
    ),
    migration: bool = typer.Option(False, "--migration", help="Also print the migration draft"),
    explain: bool = typer.Option(
        False, "--explain", help="Add a plain-English risk narrative (needs Claude API key)"
    ),
    fail_on_breaking: bool = typer.Option(
        False, "--fail-on-breaking", help="Exit non-zero if breaking impact found (for CI)"
    ),
    write_back: bool = typer.Option(
        False,
        "--write-back",
        help="Tag impacted datasets in DataHub with the verdict (needs live DataHub)",
    ),
):
    """Analyze the downstream blast radius of a column change."""
    client = _client(fixture)
    try:
        urn = resolve_dataset(client, dataset)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)

    report = analyze(
        client,
        urn,
        column,
        change_type=change,
        new_name=new_name,
        new_type=new_type,
        max_depth=max_depth,
    )

    if output == "markdown":
        print(render_markdown(report))
    elif output == "mermaid":
        print(render_mermaid(report))
    else:
        render_console(report, console)

    if migration:
        if output == "console":
            console.print()
        print(draft_migration(report))

    if explain:
        _print_explanation(report)

    if write_back:
        _write_back(report, client)

    if fail_on_breaking and report.is_blocking():
        raise typer.Exit(1)


@app.command()
def demo():
    """Run the built-in demo: drop raw.orders.customer_id on the sample stack."""
    console.rule("[bold]Blast Radius demo[/bold] — DROP raw.orders.customer_id")
    client = MockDataHubClient()
    report = analyze(client, resolve_dataset(client, "raw.orders"), "customer_id")
    render_console(report, console)
    console.print()
    print(draft_migration(report))


@app.command()
def report(
    dataset: str = typer.Argument(...),
    column: str = typer.Option(..., "--column", "-c"),
    out: Path = typer.Option(Path("blast-radius-report.md"), "--out", help="Output markdown file"),
    fixture: str = typer.Option(None, "--fixture"),
):
    """Write a full Markdown report (graph + table + migration) to a file."""
    client = _client(fixture)
    urn = resolve_dataset(client, dataset)
    rep = analyze(client, urn, column)
    body = render_markdown(rep) + "\n\n" + draft_migration(rep)
    out.write_text(body, encoding="utf-8")
    console.print(f"[green]Wrote[/green] {out}")


def main():  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
