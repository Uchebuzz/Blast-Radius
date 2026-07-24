"""Optional agentic narration layer.

The deterministic pipeline in analyzer.py is the safety gate. This layer wraps it
with the Claude Agent SDK so a human can *converse* with the blast-radius result:
ask "why is Customer 360 breaking?", "what if I rename instead of drop?", or
"draft the Slack message to the owners".

The DataHub MCP server is exposed to the agent as tools so it can pull extra
context on demand (more lineage hops, related queries) beyond the initial report.

Requires the `agent` extra:  pip install "blast-radius[agent]"
"""

from __future__ import annotations

import os

from .analyzer import analyze, resolve_dataset
from .datahub_client import DataHubClient
from .models import ChangeType
from .report import render_markdown
from .pr import draft_migration

SYSTEM_PROMPT = """You are Blast Radius, a data change-impact reviewer.
You are given a deterministic impact report produced from DataHub lineage plus
access to the DataHub MCP server. Your job:
- Explain, in plain language, exactly what breaks and why, citing the evidence
  (column lineage or query text) already in the report.
- Never downgrade a BREAKING item to safe without new evidence from a tool call.
- When asked, draft owner-addressed migration steps or notifications.
Be concise and specific. Reference assets by name and owners by handle.
"""


def build_report_context(
    client: DataHubClient,
    dataset_ref: str,
    column: str,
    change_type: ChangeType = ChangeType.DROP,
    **kwargs,
) -> str:
    """Produce the grounding block the agent reasons over."""
    urn = resolve_dataset(client, dataset_ref)
    report = analyze(client, urn, column, change_type=change_type, **kwargs)
    return render_markdown(report) + "\n\n" + draft_migration(report)


async def chat(dataset_ref: str, column: str, question: str) -> str:  # pragma: no cover
    """Answer a natural-language question about a change's blast radius.

    Wires the DataHub MCP server in as a tool source so the agent can fetch more
    context mid-conversation.
    """
    try:
        from claude_agent_sdk import ClaudeAgent, McpServer  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "The agent layer needs the 'agent' extra: pip install \"blast-radius[agent]\""
        ) from exc

    client = DataHubClient.from_env()
    context = build_report_context(client, dataset_ref, column)

    mcp_servers = []
    gms_url = os.environ.get("DATAHUB_GMS_URL")
    if gms_url:
        mcp_servers.append(
            McpServer(
                name="datahub",
                url=gms_url.rstrip("/") + "/mcp",
                headers=_auth_headers(),
            )
        )

    agent = ClaudeAgent(
        model="claude-opus-4-8",
        system=SYSTEM_PROMPT,
        mcp_servers=mcp_servers,
    )
    prompt = f"{context}\n\n---\nUser question: {question}"
    return await agent.run(prompt)


def _auth_headers() -> dict[str, str]:  # pragma: no cover
    token = os.environ.get("DATAHUB_GMS_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}
