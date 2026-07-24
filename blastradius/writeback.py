"""Write Blast Radius results back into DataHub.

The analysis itself is read-only. This module is the one *optional* write path:
after an impact assessment, it tags the affected datasets in DataHub with the
verdict — `blast-radius-breaking` / `blast-radius-at-risk` — so the knowledge
Blast Radius produced lives in the catalog. The next engineer (or agent) who
opens that dataset in DataHub inherits it: "a proposed upstream change breaks
this — check before you touch it."

It's deliberately:
  * opt-in (the `--write-back` flag),
  * idempotent (stale Blast Radius tags are cleared and re-applied each run),
  * non-destructive to other metadata (existing tags are preserved).

Requires the `datahub` extra and a live DataHub (`DATAHUB_GMS_URL`).
"""

from __future__ import annotations

import os

from .models import ImpactReport, Risk

# Verdict -> tag name written onto the affected dataset.
_TAG_FOR_RISK = {
    Risk.BREAKING: "blast-radius-breaking",
    Risk.AT_RISK: "blast-radius-at-risk",
}
_TAG_DOC = {
    "blast-radius-breaking": (
        "Flagged by Blast Radius: a proposed upstream column change breaks this "
        "asset (confirmed via column-level lineage or query text)."
    ),
    "blast-radius-at-risk": (
        "Flagged by Blast Radius: possibly affected by a proposed upstream column "
        "change — no column-level evidence to confirm, verify before shipping."
    ),
}
# Every tag this tool owns, so re-runs can clear stale verdicts first.
_ALL_BR_TAGS = {f"urn:li:tag:{t}" for t in (*_TAG_DOC, "blast-radius-cleared")}


def write_back(
    report: ImpactReport,
    gms_url: str | None = None,
    token: str | None = None,
) -> list[tuple[str, str]]:
    """Tag the breaking / at-risk datasets from ``report`` in DataHub.

    Returns the list of ``(asset_name, tag)`` written. Raises RuntimeError if the
    `datahub` extra isn't installed or no live DataHub is configured.
    """
    try:
        from datahub.emitter.mcp import MetadataChangeProposalWrapper
        from datahub.emitter.rest_emitter import DatahubRestEmitter
        from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
        from datahub.metadata.schema_classes import (
            GlobalTagsClass,
            TagAssociationClass,
            TagPropertiesClass,
        )
    except ImportError as exc:
        raise RuntimeError(
            'Write-back needs the \'datahub\' extra: pip install "blast-radius[datahub]"'
        ) from exc

    gms = gms_url or os.environ.get("DATAHUB_GMS_URL")
    if not gms:
        raise RuntimeError("Write-back needs a live DataHub — set DATAHUB_GMS_URL.")
    token = token or os.environ.get("DATAHUB_GMS_TOKEN")

    emitter = DatahubRestEmitter(gms_server=gms, token=token)
    graph = DataHubGraph(DatahubClientConfig(server=gms, token=token))

    written: list[tuple[str, str]] = []
    documented: set[str] = set()

    # Snapshot semantics: every asset in the report is reconciled so the catalog
    # reflects *this* analysis — breaking/at-risk get tagged, cleared assets have
    # any stale Blast Radius tag removed.
    for item in report.items:
        tag = _TAG_FOR_RISK.get(item.risk)  # None for cleared (LOW)
        tag_urn = f"urn:li:tag:{tag}" if tag else None

        existing = graph.get_aspect(item.asset.urn, GlobalTagsClass)
        current = list(existing.tags) if existing and existing.tags else []
        had_br = any(a.tag in _ALL_BR_TAGS for a in current)

        # Nothing to do: cleared asset that never carried a Blast Radius tag.
        if tag_urn is None and not had_br:
            continue

        # Keep non-Blast-Radius tags; re-apply only the current verdict (if any).
        assocs = [a for a in current if a.tag not in _ALL_BR_TAGS]
        if tag_urn is not None:
            if tag_urn not in documented:
                emitter.emit(
                    MetadataChangeProposalWrapper(
                        entityUrn=tag_urn,
                        aspect=TagPropertiesClass(name=tag, description=_TAG_DOC[tag]),
                    )
                )
                documented.add(tag_urn)
            assocs.append(TagAssociationClass(tag=tag_urn))

        emitter.emit(
            MetadataChangeProposalWrapper(
                entityUrn=item.asset.urn, aspect=GlobalTagsClass(tags=assocs)
            )
        )
        if tag:
            written.append((item.asset.name, tag))

    return written
