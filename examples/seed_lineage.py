"""Seed a live DataHub instance with the full Blast Radius demo stack.

Pushes the same mini stack described in `sample_stack.json` (raw.orders ->
staging -> marts -> dashboards + ML feature) into a running DataHub, including
the *rich* metadata Blast Radius reasons over:

  * dataset schemas (columns)
  * table-level lineage
  * column-level (fine-grained) lineage
  * ownership
  * SQL queries (as Query entities)
  * dashboards (consuming datasets)
  * an ML feature table

so the live demo (`blastradius analyze raw.orders -c customer_id` against
DATAHUB_GMS_URL) reproduces the offline mock result against real DataHub lineage.

Prereqs:
    datahub docker quickstart          # spin up local DataHub
    pip install "blast-radius[datahub]"
    export DATAHUB_GMS_URL=http://localhost:8080
    export DATAHUB_GMS_TOKEN=<personal access token>   # optional for local
    python examples/seed_lineage.py

This uses the open-source `acryl-datahub` emitter. It is intentionally the only
part of the project that *writes* to DataHub; everything else is read-only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

FIXTURE = Path(__file__).with_name("sample_stack.json")

# ---------------------------------------------------------------------------
# URN helpers
# ---------------------------------------------------------------------------

_NUMERIC_HINTS = ("id", "amount", "count", "revenue", "ltv", "num", "qty")


def _schema_field_urn(dataset_urn: str, column: str) -> str:
    return f"urn:li:schemaField:({dataset_urn},{column})"


def _now_ms() -> int:
    return int(time.time() * 1000)


def main() -> None:
    try:
        from datahub.emitter.mcp import MetadataChangeProposalWrapper
        from datahub.emitter.rest_emitter import DatahubRestEmitter
        from datahub.metadata.schema_classes import (
            AuditStampClass,
            ChangeAuditStampsClass,
            DashboardInfoClass,
            DatasetLineageTypeClass,
            EdgeClass,
            FineGrainedLineageClass,
            InputFieldClass,
            InputFieldsClass,
            FineGrainedLineageDownstreamTypeClass,
            FineGrainedLineageUpstreamTypeClass,
            MLFeaturePropertiesClass,
            MLFeatureTablePropertiesClass,
            NumberTypeClass,
            OtherSchemaClass,
            OwnerClass,
            OwnershipClass,
            OwnershipTypeClass,
            QueryLanguageClass,
            QueryPropertiesClass,
            QuerySourceClass,
            QueryStatementClass,
            QuerySubjectClass,
            QuerySubjectsClass,
            SchemaFieldClass,
            SchemaFieldDataTypeClass,
            SchemaMetadataClass,
            StringTypeClass,
            UpstreamClass,
            UpstreamLineageClass,
        )
    except ImportError:
        raise SystemExit(
            'Install the DataHub SDK first:  pip install "blast-radius[datahub]"'
        )

    gms = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
    token = os.environ.get("DATAHUB_GMS_TOKEN")
    emitter = DatahubRestEmitter(gms_server=gms, token=token)
    actor = "urn:li:corpuser:blast-radius-seed"
    now = _now_ms()

    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assets = {a["urn"]: a for a in data["assets"]}
    name_to_urn = {a["name"]: a["urn"] for a in data["assets"]}

    ok = 0
    fail = 0

    def emit(entity_urn: str, aspect, label: str) -> None:
        nonlocal ok, fail
        try:
            emitter.emit(MetadataChangeProposalWrapper(entityUrn=entity_urn, aspect=aspect))
            print(f"  + {label}")
            ok += 1
        except Exception as exc:  # keep going; surface what worked
            print(f"  ! FAILED {label}: {exc}")
            fail += 1

    def field_type(col: str) -> SchemaFieldDataTypeClass:
        is_num = any(h in col.lower() for h in _NUMERIC_HINTS)
        return SchemaFieldDataTypeClass(
            type=NumberTypeClass() if is_num else StringTypeClass()
        )

    def owners_aspect(asset: dict):
        owners = [
            OwnerClass(owner=o["urn"], type=OwnershipTypeClass.TECHNICAL_OWNER)
            for o in asset.get("owners", [])
        ]
        return OwnershipClass(owners=owners) if owners else None

    def parse_ref(ref: str) -> tuple[str, str] | None:
        """'raw.orders::customer_id' -> (dataset_urn, column)."""
        if "::" not in ref:
            return None
        name, col = ref.split("::", 1)
        urn = name_to_urn.get(name)
        return (urn, col) if urn else None

    # Table-level downstream adjacency (upstream -> [downstream]).
    downstream_of: dict[str, list[str]] = {}
    upstreams_of: dict[str, list[str]] = {}
    for up, down in data["lineage"]:
        downstream_of.setdefault(up, []).append(down)
        upstreams_of.setdefault(down, []).append(up)

    # -- Datasets: schema + ownership + table & column lineage ---------------
    for urn, asset in assets.items():
        if asset["kind"] != "dataset":
            continue
        print(f"dataset {asset['name']}")
        platform_urn = f"urn:li:dataPlatform:{asset['platform']}"

        # Schema
        fields = [
            SchemaFieldClass(
                fieldPath=c,
                type=field_type(c),
                nativeDataType="number" if any(h in c.lower() for h in _NUMERIC_HINTS) else "string",
            )
            for c in asset.get("columns", [])
        ]
        if fields:
            emit(
                urn,
                SchemaMetadataClass(
                    schemaName=asset["name"],
                    platform=platform_urn,
                    version=0,
                    hash="",
                    platformSchema=OtherSchemaClass(rawSchema=""),
                    fields=fields,
                ),
                "schema",
            )

        # Ownership
        own = owners_aspect(asset)
        if own:
            emit(urn, own, "ownership")

        # Lineage (table-level upstreams + fine-grained column lineage)
        up_urns = [u for u in upstreams_of.get(urn, []) if u.startswith("urn:li:dataset:")]
        fine: list = []
        for down_col, refs in asset.get("upstream_field_refs", {}).items():
            up_fields = []
            for ref in refs:
                parsed = parse_ref(ref)
                if parsed and parsed[0].startswith("urn:li:dataset:"):
                    up_fields.append(_schema_field_urn(parsed[0], parsed[1]))
            if up_fields:
                fine.append(
                    FineGrainedLineageClass(
                        upstreamType=FineGrainedLineageUpstreamTypeClass.FIELD_SET,
                        upstreams=up_fields,
                        downstreamType=FineGrainedLineageDownstreamTypeClass.FIELD,
                        downstreams=[_schema_field_urn(urn, down_col)],
                    )
                )
        if up_urns or fine:
            emit(
                urn,
                UpstreamLineageClass(
                    upstreams=[
                        UpstreamClass(dataset=u, type=DatasetLineageTypeClass.TRANSFORMED)
                        for u in up_urns
                    ],
                    fineGrainedLineages=fine or None,
                ),
                f"lineage ({len(up_urns)} table, {len(fine)} column)",
            )

    # -- Dashboards: info + ownership + dataset edges ------------------------
    for urn, asset in assets.items():
        if asset["kind"] != "dashboard":
            continue
        print(f"dashboard {asset['name']}")
        ds_edges = [
            EdgeClass(destinationUrn=u)
            for u in upstreams_of.get(urn, [])
            if u.startswith("urn:li:dataset:")
        ]
        emit(
            urn,
            DashboardInfoClass(
                title=asset["name"],
                description=f"{asset['name']} (seeded by Blast Radius demo)",
                lastModified=ChangeAuditStampsClass(
                    created=AuditStampClass(time=now, actor=actor),
                    lastModified=AuditStampClass(time=now, actor=actor),
                ),
                datasetEdges=ds_edges or None,
            ),
            f"dashboardInfo ({len(ds_edges)} dataset edges)",
        )
        own = owners_aspect(asset)
        if own:
            emit(urn, own, "ownership")

        # Column-level consumption: which upstream columns the dashboard's
        # queries reference. This is what lets Blast Radius tell a truly
        # affected dashboard from one that merely sits downstream.
        query_text = " ".join(asset.get("queries", []))
        input_fields = []
        for up_urn in upstreams_of.get(urn, []):
            up = assets.get(up_urn)
            if not up:
                continue
            for col in up.get("columns", []):
                if re.search(rf"\b{re.escape(col)}\b", query_text, flags=re.IGNORECASE):
                    input_fields.append(
                        InputFieldClass(schemaFieldUrn=_schema_field_urn(up_urn, col))
                    )
        if input_fields:
            emit(
                urn,
                InputFieldsClass(fields=input_fields),
                f"inputFields ({len(input_fields)} consumed columns)",
            )

    # -- ML feature table: features whose SOURCES are upstream columns -------
    # mlFeatureTable does not support the upstreamLineage aspect; ML lineage in
    # DataHub flows through MLFeature.sources (dataset / schemaField urns). We
    # create one MLFeature per column and point its sources at the source
    # mart's schema fields, so dropping that column surfaces the broken feature.
    for urn, asset in assets.items():
        if asset["kind"] != "ml_feature":
            continue
        print(f"ml_feature {asset['name']}")
        # Derive a clean feature-namespace from the table urn (…,customer_ltv).
        namespace = urn.rstrip(")").rsplit(",", 1)[-1]

        feature_urns = []
        for down_col, refs in asset.get("upstream_field_refs", {}).items():
            # MLFeature.sources accepts dataset urns only (not schemaField), so
            # link each feature to its source dataset at table level.
            sources = []
            for ref in refs:
                parsed = parse_ref(ref)
                if parsed and parsed[0].startswith("urn:li:dataset:") and parsed[0] not in sources:
                    sources.append(parsed[0])
            feat_urn = f"urn:li:mlFeature:({namespace},{down_col})"
            emit(
                feat_urn,
                MLFeaturePropertiesClass(
                    description=f"{down_col} feature of {namespace}",
                    sources=sources or None,
                ),
                f"mlFeature {down_col} (sources={len(sources)})",
            )
            feat_own = owners_aspect(asset)
            if feat_own:
                emit(feat_urn, feat_own, f"ownership {down_col}")
            feature_urns.append(feat_urn)

        emit(
            urn,
            MLFeatureTablePropertiesClass(
                description=f"{asset['name']} (seeded by Blast Radius demo)",
                mlFeatures=feature_urns or None,
                mlPrimaryKeys=[],
            ),
            f"mlFeatureTableProperties ({len(feature_urns)} features)",
        )
        own = owners_aspect(asset)
        if own:
            emit(urn, own, "ownership")

    # -- Queries: one Query entity per SQL string, subject = its dataset -----
    # DataHub's listQueries resolver only supports dataset subjects; attaching
    # a query to a dashboard/ML urn breaks that API, so seed dataset queries only.
    print("queries")
    for urn, asset in assets.items():
        if asset["kind"] != "dataset":
            continue
        for i, sql in enumerate(asset.get("queries", [])):
            qid = hashlib.md5(f"{urn}:{i}:{sql}".encode()).hexdigest()[:20]
            query_urn = f"urn:li:query:{qid}"
            emit(
                query_urn,
                QueryPropertiesClass(
                    statement=QueryStatementClass(value=sql, language=QueryLanguageClass.SQL),
                    source=QuerySourceClass.SYSTEM,
                    name=f"{asset['name']} query {i + 1}",
                    created=AuditStampClass(time=now, actor=actor),
                    lastModified=AuditStampClass(time=now, actor=actor),
                ),
                f"query -> {asset['name']}",
            )
            emit(
                query_urn,
                QuerySubjectsClass(subjects=[QuerySubjectClass(entity=urn)]),
                f"query subjects -> {asset['name']}",
            )

    print(f"\nDone. emitted={ok} failed={fail}")
    print("Try:  DATAHUB_GMS_URL=http://localhost:8080 blastradius analyze raw.orders -c customer_id")


if __name__ == "__main__":
    main()
