"""Downstream lineage traversal.

Breadth-first walk from the changed dataset over DataHub downstream edges, with a
depth cap and a cycle guard so pathological graphs (or metadata loops) can't hang
the agent. Returns every reachable asset annotated with the shortest depth at
which it was found, plus the edge list for graph rendering.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .datahub_client import DataHubClient
from .models import Asset


@dataclass
class LineageNode:
    asset: Asset
    depth: int


@dataclass
class LineageResult:
    root: Asset
    nodes: list[LineageNode]           # excludes the root
    edges: list[tuple[str, str]]       # (upstream_urn, downstream_urn)


def traverse_downstream(
    client: DataHubClient,
    root_urn: str,
    max_depth: int = 10,
) -> LineageResult:
    """Walk all downstream dependencies of ``root_urn``.

    Args:
        client: metadata source.
        root_urn: URN of the dataset whose column is changing.
        max_depth: hard cap on traversal depth (defends against deep/cyclic graphs).
    """
    root = client.get_entity(root_urn)
    if root is None:
        raise ValueError(f"Root entity not found in DataHub: {root_urn}")

    visited: set[str] = {root_urn}
    nodes: list[LineageNode] = []
    edges: list[tuple[str, str]] = []
    queue: deque[tuple[str, int]] = deque([(root_urn, 0)])

    while queue:
        urn, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for down_urn in client.get_downstream(urn):
            edges.append((urn, down_urn))
            if down_urn in visited:
                continue  # cycle / diamond guard — record the edge, don't recurse again
            visited.add(down_urn)
            asset = client.get_entity(down_urn)
            if asset is None:
                # Dangling lineage edge — surface it as a stub rather than crashing.
                continue
            nodes.append(LineageNode(asset=asset, depth=depth + 1))
            queue.append((down_urn, depth + 1))

    nodes.sort(key=lambda n: (n.depth, n.asset.name))
    return LineageResult(root=root, nodes=nodes, edges=edges)
