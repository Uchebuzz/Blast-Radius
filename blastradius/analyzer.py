"""The Blast Radius pipeline: change -> lineage -> impact -> report.

This is the deterministic core the CLI, GitHub Action, and agent layer all call.
Keeping it separate from any LLM means the safety gate is reproducible and
testable; the agent layer (agent.py) only *narrates* and orchestrates it.
"""

from __future__ import annotations

from .datahub_client import DataHubClient
from .impact import score_impact
from .lineage import traverse_downstream
from .models import ChangeSpec, ChangeType, ImpactReport


def analyze(
    client: DataHubClient,
    dataset_urn: str,
    column: str,
    change_type: ChangeType = ChangeType.DROP,
    new_name: str | None = None,
    new_type: str | None = None,
    max_depth: int = 10,
) -> ImpactReport:
    """Run the full blast-radius analysis for one column change."""
    change = ChangeSpec(
        dataset_urn=dataset_urn,
        column=column,
        change_type=change_type,
        new_name=new_name,
        new_type=new_type,
    )
    lineage = traverse_downstream(client, dataset_urn, max_depth=max_depth)
    return score_impact(change, lineage)


def resolve_dataset(client: DataHubClient, ref: str) -> str:
    """Accept either a full URN or a friendly name and return a URN.

    Lets users type `blastradius analyze raw.orders --column customer_id` instead
    of pasting a full `urn:li:dataset:(...)`.
    """
    if ref.startswith("urn:li:"):
        return ref
    matches = client.search(ref)
    if not matches:
        raise ValueError(f"No DataHub entity matches '{ref}'")
    if len(matches) > 1:
        # Prefer an exact-ish dataset match; otherwise report the ambiguity.
        exact = [m for m in matches if ref in m and "dataset" in m]
        if len(exact) == 1:
            return exact[0]
        raise ValueError(
            f"'{ref}' is ambiguous ({len(matches)} matches). Pass a full URN. "
            f"Candidates: {', '.join(matches[:5])}"
        )
    return matches[0]
