"""Load DataHub's official **healthcare** sample dataset into a local DataHub,
enriched with column-level lineage so Blast Radius can show its precision.

The healthcare datapack (https://github.com/datahub-project/static-assets/tree/main/datasets/healthcare)
is a forking pipeline:

    raw_patients -> staging_patients -> mart_billing        (finance_team)
                                     -> mart_demographics    (research_team)

DataHub's own recipe (`datahub ingest -c ingest.yaml` + `add_lineage.py`) ingests
the schema and **table-level** lineage. Blast Radius's whole point is column-level
precision, so this loader additionally emits fine-grained (column) lineage — the
mapping that lets it prove `mart_billing` breaks on `billing_amount` while
`mart_demographics` is cleared (and vice-versa for `age`).

It reads the schema straight from the bundled SQLite DB via the standard library
and writes to DataHub with the open-source `acryl-datahub` REST emitter — the only
part that writes; Blast Radius itself stays read-only.

Usage:
    # 1. download healthcare.db next to this script (once):
    #    curl -sSL https://raw.githubusercontent.com/datahub-project/static-assets/main/datasets/healthcare/healthcare.db -o healthcare.db
    # 2. point at a running DataHub and load:
    export DATAHUB_GMS_URL=http://localhost:8080
    python examples/healthcare/load_healthcare.py
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DB = Path(__file__).with_name("healthcare.db")
PLATFORM = "urn:li:dataPlatform:sqlite"
INSTANCE = "healthcare"
TABLES = ["raw_patients", "staging_patients", "mart_billing", "mart_demographics"]

OWNERS = {
    "raw_patients": "urn:li:corpGroup:clinical_team",
    "staging_patients": "urn:li:corpGroup:clinical_team",
    "mart_billing": "urn:li:corpGroup:finance_team",
    "mart_demographics": "urn:li:corpGroup:research_team",
}

# Table-level lineage (the forking pipeline).
TABLE_UPSTREAMS = {
    "staging_patients": ["raw_patients"],
    "mart_billing": ["staging_patients"],
    "mart_demographics": ["staging_patients"],
}

# Column-level lineage: downstream_col -> [upstream_col, ...] on the direct parent.
COLUMN_LINEAGE = {
    "staging_patients": {  # standardized 1:1 copy of raw + derived *_clean columns
        **{c: [c] for c in [
            "name", "age", "gender", "blood_type", "medical_condition",
            "date_of_admission", "doctor", "hospital", "insurance_provider",
            "billing_amount", "room_number", "admission_type", "discharge_date",
            "medication", "test_results",
        ]},
        "gender_clean": ["gender"],
        "blood_type_clean": ["blood_type"],
        "condition_clean": ["medical_condition"],
        "admission_type_clean": ["admission_type"],
        "test_results_clean": ["test_results"],
    },
    "mart_billing": {
        "name": ["name"],
        "hospital": ["hospital"],
        "insurance_provider": ["insurance_provider"],
        "admission_type": ["admission_type_clean"],
        "billing_amount": ["billing_amount"],
        "date_of_admission": ["date_of_admission"],
        "discharge_date": ["discharge_date"],
        "length_of_stay_days": ["date_of_admission", "discharge_date"],
        "medication": ["medication"],
    },
    "mart_demographics": {
        "name": ["name"],
        "age": ["age"],
        "gender": ["gender_clean"],
        "blood_type": ["blood_type_clean"],
        "medical_condition": ["condition_clean"],
        "hospital": ["hospital"],
        "test_results": ["test_results_clean"],
    },
}

_NUMERIC = ("age", "amount", "number", "days", "room")


def ds_urn(table: str) -> str:
    return f"urn:li:dataset:({PLATFORM},{INSTANCE}.{table},PROD)"


def field_urn(table: str, col: str) -> str:
    return f"urn:li:schemaField:({ds_urn(table)},{col})"


def main() -> None:
    if not DB.exists():
        raise SystemExit(
            f"{DB.name} not found next to this script. Download it first:\n"
            "  curl -sSL https://raw.githubusercontent.com/datahub-project/"
            "static-assets/main/datasets/healthcare/healthcare.db -o "
            f'"{DB}"'
        )
    try:
        from datahub.emitter.mcp import MetadataChangeProposalWrapper
        from datahub.emitter.rest_emitter import DatahubRestEmitter
        from datahub.metadata.schema_classes import (
            DatasetLineageTypeClass,
            FineGrainedLineageClass,
            FineGrainedLineageDownstreamTypeClass,
            FineGrainedLineageUpstreamTypeClass,
            NumberTypeClass,
            OtherSchemaClass,
            OwnerClass,
            OwnershipClass,
            OwnershipTypeClass,
            SchemaFieldClass,
            SchemaFieldDataTypeClass,
            SchemaMetadataClass,
            StringTypeClass,
            UpstreamClass,
            UpstreamLineageClass,
        )
    except ImportError:
        raise SystemExit('Install the DataHub SDK first:  pip install "blast-radius[datahub]"')

    gms = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
    emitter = DatahubRestEmitter(gms_server=gms, token=os.environ.get("DATAHUB_GMS_TOKEN"))

    con = sqlite3.connect(str(DB))
    columns = {
        t: [r[1] for r in con.execute(f"PRAGMA table_info({t})")] for t in TABLES
    }

    def emit(urn, aspect, label):
        emitter.emit(MetadataChangeProposalWrapper(entityUrn=urn, aspect=aspect))
        print(f"  + {label}")

    for table in TABLES:
        urn = ds_urn(table)
        print(table)

        # Schema
        fields = [
            SchemaFieldClass(
                fieldPath=c,
                type=SchemaFieldDataTypeClass(
                    type=NumberTypeClass() if any(n in c.lower() for n in _NUMERIC) else StringTypeClass()
                ),
                nativeDataType="number" if any(n in c.lower() for n in _NUMERIC) else "text",
            )
            for c in columns[table]
        ]
        emit(
            urn,
            SchemaMetadataClass(
                schemaName=f"{INSTANCE}.{table}",
                platform=PLATFORM,
                version=0,
                hash="",
                platformSchema=OtherSchemaClass(rawSchema=""),
                fields=fields,
            ),
            f"schema ({len(fields)} cols)",
        )

        # Ownership
        emit(
            urn,
            OwnershipClass(owners=[OwnerClass(owner=OWNERS[table], type=OwnershipTypeClass.DATAOWNER)]),
            f"owner {OWNERS[table].split(':')[-1]}",
        )

        # Lineage (table-level + fine-grained column-level)
        if table in TABLE_UPSTREAMS:
            parents = TABLE_UPSTREAMS[table]
            fine = []
            for down_col, up_cols in COLUMN_LINEAGE.get(table, {}).items():
                ups = [
                    field_urn(p, uc)
                    for p in parents
                    for uc in up_cols
                    if uc in columns.get(p, [])
                ]
                if ups:
                    fine.append(
                        FineGrainedLineageClass(
                            upstreamType=FineGrainedLineageUpstreamTypeClass.FIELD_SET,
                            upstreams=ups,
                            downstreamType=FineGrainedLineageDownstreamTypeClass.FIELD,
                            downstreams=[field_urn(table, down_col)],
                        )
                    )
            emit(
                urn,
                UpstreamLineageClass(
                    upstreams=[
                        UpstreamClass(dataset=ds_urn(p), type=DatasetLineageTypeClass.TRANSFORMED)
                        for p in parents
                    ],
                    fineGrainedLineages=fine or None,
                ),
                f"lineage ({len(parents)} table, {len(fine)} column)",
            )

    print("\nDone. Try:")
    print("  blastradius analyze healthcare.raw_patients -c billing_amount   # breaks billing, clears demographics")
    print("  blastradius analyze healthcare.raw_patients -c age              # breaks demographics, clears billing")


if __name__ == "__main__":
    main()
