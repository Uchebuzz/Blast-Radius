"""Seed a live DataHub instance with the Blast Radius demo stack.

Pushes the same mini stack described in `sample_stack.json` (raw.orders ->
staging -> marts -> dashboards + ML feature) into a running DataHub so the live
demo (`blastradius analyze raw.orders -c customer_id` against DATAHUB_GMS_URL)
shows real lineage.

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

import json
import os
from pathlib import Path

FIXTURE = Path(__file__).with_name("sample_stack.json")


def main() -> None:
    try:
        from datahub.emitter.mce_builder import make_data_platform_urn
        from datahub.emitter.rest_emitter import DatahubRestEmitter
        from datahub.metadata.schema_classes import (
            DatasetLineageTypeClass,
            UpstreamClass,
            UpstreamLineageClass,
        )
    except ImportError:
        raise SystemExit(
            "Install the DataHub SDK first:  pip install \"blast-radius[datahub]\""
        )

    gms = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
    token = os.environ.get("DATAHUB_GMS_TOKEN")
    emitter = DatahubRestEmitter(gms_server=gms, token=token)

    data = json.loads(FIXTURE.read_text(encoding="utf-8"))

    # Emit table-level lineage (edges).
    downstream_upstreams: dict[str, list[str]] = {}
    for up, down in data["lineage"]:
        downstream_upstreams.setdefault(down, []).append(up)

    for down_urn, up_urns in downstream_upstreams.items():
        if not down_urn.startswith("urn:li:dataset:"):
            continue  # UpstreamLineage aspect applies to datasets
        lineage = UpstreamLineageClass(
            upstreams=[
                UpstreamClass(dataset=u, type=DatasetLineageTypeClass.TRANSFORMED)
                for u in up_urns
                if u.startswith("urn:li:dataset:")
            ]
        )
        if not lineage.upstreams:
            continue
        from datahub.emitter.mcp import MetadataChangeProposalWrapper

        emitter.emit(
            MetadataChangeProposalWrapper(entityUrn=down_urn, aspect=lineage)
        )
        print(f"seeded lineage -> {down_urn}")

    _ = make_data_platform_urn  # referenced for clarity; platforms inferred from URNs
    print("\nDone. Try:  blastradius analyze raw.orders -c customer_id")


if __name__ == "__main__":
    main()
