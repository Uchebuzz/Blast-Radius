"""Column-level impact scoring — the core of Blast Radius.

Naive lineage tools tell you *everything* downstream of a table. That's noise: a
revenue dashboard sitting three hops below `orders` doesn't break just because you
drop `orders.customer_id` — unless it actually reads that column.

This module separates real breaks from noise by propagating the changed column
through the graph using two independent signals:

  1. Column-level lineage (`upstream_field_refs` from DataHub fine-grained lineage)
  2. Real query text referencing the column (`get_dataset_queries`)

An asset is BREAKING only when evidence links it to the changed column. Assets
that are merely downstream, with no such link, are correctly cleared to LOW.
"""

from __future__ import annotations

import re

from .models import ChangeSpec, ImpactItem, ImpactReport, Risk
from .lineage import LineageResult


def _mentions(column: str, query: str) -> bool:
    """Word-boundary, case-insensitive match of a column name in query text."""
    return re.search(rf"\b{re.escape(column)}\b", query, flags=re.IGNORECASE) is not None


def _field_key(asset_name: str, column: str) -> str:
    return f"{asset_name}::{column}"


def score_impact(change: ChangeSpec, lineage: LineageResult) -> ImpactReport:
    """Classify every downstream asset as BREAKING / AT_RISK / LOW.

    Assets are processed in increasing lineage depth so that "affected column"
    facts propagate forward: once we confirm a mart column derives from the
    changed column, anything reading *that* column is caught too.
    """
    urn_to_name = {lineage.root.urn: lineage.root.name}
    for node in lineage.nodes:
        urn_to_name[node.asset.urn] = node.asset.name

    parents: dict[str, list[str]] = {}
    for up_urn, down_urn in lineage.edges:
        parents.setdefault(down_urn, []).append(up_urn)

    changed_col = change.column
    # Sets of "assetName::column" and "assetName" known to carry the changed data.
    affected_cols: set[str] = {_field_key(lineage.root.name, changed_col)}
    affected_assets: set[str] = {lineage.root.name}

    items: list[ImpactItem] = []
    for node in lineage.nodes:  # already sorted by (depth, name)
        asset = node.asset
        reasons: list[str] = []

        # Signal 1: fine-grained column lineage.
        field_refs = [
            col
            for col, upstreams in asset.upstream_field_refs.items()
            if any(u in affected_cols for u in upstreams)
        ]

        # Is any *direct* upstream of this asset already known to be affected?
        parent_names = {urn_to_name.get(p) for p in parents.get(asset.urn, [])}
        upstream_affected = bool(parent_names & affected_assets)

        # Signal 2: real query text (used where field-level lineage is absent,
        # e.g. dashboards and ML features). Only trust it when an upstream is affected.
        query_hit = upstream_affected and any(_mentions(changed_col, q) for q in asset.queries)

        if field_refs:
            risk = Risk.BREAKING
            reasons.append(
                f"column-level lineage: {', '.join(field_refs)} derive(s) from the changed column"
            )
            referencing = field_refs
            for col in field_refs:
                affected_cols.add(_field_key(asset.name, col))
            affected_assets.add(asset.name)

        elif query_hit:
            risk = Risk.BREAKING
            reasons.append(f"query references `{changed_col}` and reads from an affected upstream")
            referencing = [changed_col]
            affected_cols.add(_field_key(asset.name, changed_col))
            affected_assets.add(asset.name)

        elif upstream_affected and not asset.upstream_field_refs and not asset.queries:
            risk = Risk.AT_RISK
            reasons.append(
                "downstream of an affected asset, but no column lineage or query text "
                "available to confirm — verify manually"
            )
            referencing = []

        elif upstream_affected:
            risk = Risk.LOW
            reasons.append(
                "downstream of an affected asset but does not reference the changed column"
            )
            referencing = []

        else:
            risk = Risk.LOW
            reasons.append("no dependency on the changed column found in lineage or queries")
            referencing = []

        items.append(
            ImpactItem(
                asset=asset,
                depth=node.depth,
                risk=risk,
                reasons=reasons,
                referencing_columns=referencing,
            )
        )

    # Sort most-severe first, then by proximity.
    items.sort(key=lambda i: (-i.risk.rank, i.depth, i.asset.name))
    return ImpactReport(change=change, source=lineage.root, items=items, edges=lineage.edges)
