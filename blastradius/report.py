"""Rendering: terminal (Rich), Markdown (for PR comments / README), and a Mermaid
impact graph that renders natively in GitHub, Devpost, and Claude artifacts.
"""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .models import ImpactReport, Risk

_RISK_STYLE = {Risk.BREAKING: "bold red", Risk.AT_RISK: "bold yellow", Risk.LOW: "green"}
_RISK_LABEL = {Risk.BREAKING: "BREAKING", Risk.AT_RISK: "AT RISK", Risk.LOW: "low"}
_RISK_EMOJI = {Risk.BREAKING: "🔴", Risk.AT_RISK: "🟡", Risk.LOW: "🟢"}


# --------------------------------------------------------------------------- #
# Terminal
# --------------------------------------------------------------------------- #
def render_console(report: ImpactReport, console: Console | None = None) -> None:
    console = console or Console()

    header = Text.assemble(
        ("💥 Blast Radius\n", "bold"),
        (report.change.describe(), "cyan"),
    )
    console.print(Panel(header, border_style="red" if report.is_blocking() else "green"))

    table = Table(show_header=True, header_style="bold", expand=True)
    table.add_column("", width=3)
    table.add_column("Downstream asset")
    table.add_column("Kind")
    table.add_column("Depth", justify="right")
    table.add_column("Owners")
    table.add_column("Why")

    for item in report.items:
        table.add_row(
            _RISK_EMOJI[item.risk],
            Text(item.asset.name, style=_RISK_STYLE[item.risk]),
            item.asset.kind.value,
            str(item.depth),
            ", ".join(item.owner_handles) or "—",
            item.reasons[0] if item.reasons else "",
        )
    console.print(table)

    summary = Text.assemble(
        (f"{len(report.breaking)} breaking  ", "bold red"),
        (f"{len(report.at_risk)} at-risk  ", "bold yellow"),
        (f"{len(report.low)} cleared", "green"),
    )
    console.print(summary)
    if report.impacted_owners:
        who = ", ".join(o.handle for o in report.impacted_owners)
        console.print(Text(f"Notify: {who}", style="cyan"))

    verdict = (
        Text("MERGE BLOCKED — breaking downstream impact", style="bold white on red")
        if report.is_blocking()
        else Text("SAFE TO MERGE — no breaking impact detected", style="bold white on green")
    )
    console.print(Panel(verdict))


# --------------------------------------------------------------------------- #
# Mermaid graph
# --------------------------------------------------------------------------- #
def render_mermaid(report: ImpactReport) -> str:
    """A flowchart with breaking assets in red, at-risk in amber, cleared in green."""
    risk_by_urn = {i.asset.urn: i.risk for i in report.items}
    name_by_urn = {report.source.urn: report.source.name}
    for i in report.items:
        name_by_urn[i.asset.urn] = i.asset.name

    def node_id(urn: str) -> str:
        # Mermaid node ids can't contain most punctuation.
        return "n" + str(abs(hash(urn)) % (10**10))

    lines = ["flowchart LR"]
    # Declare nodes.
    declared: set[str] = set()

    def declare(urn: str) -> str:
        nid = node_id(urn)
        if urn not in declared:
            label = name_by_urn.get(urn, urn.split(",")[-2] if "," in urn else urn)
            lines.append(f'    {nid}["{label}"]')
            declared.add(urn)
        return nid

    declare(report.source.urn)
    for i in report.items:
        declare(i.asset.urn)

    for up, down in report.edges:
        lines.append(f"    {node_id(up)} --> {node_id(down)}")

    # Styling.
    lines.append("    classDef root fill:#1f2937,stroke:#111827,color:#fff;")
    lines.append("    classDef breaking fill:#fee2e2,stroke:#dc2626,color:#7f1d1d;")
    lines.append("    classDef atrisk fill:#fef9c3,stroke:#ca8a04,color:#713f12;")
    lines.append("    classDef low fill:#dcfce7,stroke:#16a34a,color:#14532d;")
    lines.append(f"    class {node_id(report.source.urn)} root;")
    for urn, risk in risk_by_urn.items():
        cls = {Risk.BREAKING: "breaking", Risk.AT_RISK: "atrisk", Risk.LOW: "low"}[risk]
        lines.append(f"    class {node_id(urn)} {cls};")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Markdown (PR comments, README, Devpost)
# --------------------------------------------------------------------------- #
def render_markdown(report: ImpactReport, include_graph: bool = True) -> str:
    c = report.change
    blocking = report.is_blocking()
    verdict = (
        "### ❌ Merge blocked — breaking downstream impact"
        if blocking
        else "### ✅ Safe to merge — no breaking impact detected"
    )

    md: list[str] = [
        "## 💥 Blast Radius report",
        "",
        f"**Change:** `{c.describe()}`  ",
        f"**Downstream assets scanned:** {len(report.items)}  ",
        f"**Impact:** {len(report.breaking)} breaking · "
        f"{len(report.at_risk)} at-risk · {len(report.low)} cleared",
        "",
        verdict,
        "",
    ]

    if include_graph:
        md += ["```mermaid", render_mermaid(report), "```", ""]

    md += ["| | Asset | Kind | Depth | Owners | Why |", "|---|---|---|---|---|---|"]
    for item in report.items:
        md.append(
            f"| {_RISK_EMOJI[item.risk]} **{_RISK_LABEL[item.risk]}** "
            f"| `{item.asset.name}` | {item.asset.kind.value} | {item.depth} "
            f"| {', '.join(item.owner_handles) or '—'} "
            f"| {item.reasons[0] if item.reasons else ''} |"
        )
    md.append("")

    if report.impacted_owners:
        who = " ".join(o.handle for o in report.impacted_owners)
        md += [f"**Owners to notify:** {who}", ""]

    md += ["<sub>Generated by 💥 Blast Radius using DataHub lineage.</sub>"]
    return "\n".join(md)
