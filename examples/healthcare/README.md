# Blast Radius on DataHub's `healthcare` sample dataset

This runs Blast Radius against **DataHub's official [healthcare datapack](https://github.com/datahub-project/static-assets/tree/main/datasets/healthcare)** — ~55k synthetic patient records modelled as a *forking* pipeline:

```
raw_patients ──▶ staging_patients ──┬──▶ mart_billing        (finance_team)
                                    └──▶ mart_demographics    (research_team)
```

It's the ideal test of Blast Radius's core claim — **precision** — because the two
marts read *different* columns:

| Drop this column of `raw_patients` | Breaks | Cleared |
|---|---|---|
| `billing_amount` | `staging_patients`, `mart_billing` | **`mart_demographics`** |
| `age` | `staging_patients`, `mart_demographics` | **`mart_billing`** |

A naive lineage tool would flag *both* marts every time. Blast Radius reads
column-level lineage and clears the mart that doesn't touch the changed column.

## Load it

DataHub's datapack ships table-level lineage; Blast Radius needs column-level, so
this loader emits schema + **table and column** lineage + ownership in one pass,
straight from the bundled SQLite DB via the open-source `acryl-datahub` emitter.

```bash
# 1. Get the sample DB (once):
curl -sSL https://raw.githubusercontent.com/datahub-project/static-assets/main/datasets/healthcare/healthcare.db \
  -o examples/healthcare/healthcare.db

# 2. Load into a running DataHub:
export DATAHUB_GMS_URL=http://localhost:8080
python examples/healthcare/load_healthcare.py
```

> DataHub's own recipe (`datahub ingest -c ingest.yaml && python add_lineage.py`)
> also works if your CLI has the `sqlalchemy` source plugin; it produces
> table-level lineage only.

## Run it

```bash
blastradius analyze healthcare.raw_patients -c billing_amount   # → breaks billing, clears demographics
blastradius analyze healthcare.raw_patients -c age              # → breaks demographics, clears billing
blastradius analyze healthcare.raw_patients -c billing_amount --explain   # + Claude narrative
```

A captured run is in [`sample-report-healthcare.md`](sample-report-healthcare.md).

```
💥 Blast Radius — DROP healthcare.raw_patients::billing_amount

 🔴 healthcare.staging_patients   dataset  1  @clinical_team  column-level lineage
 🔴 healthcare.mart_billing       dataset  2  @finance_team   column-level lineage
 🟢 healthcare.mart_demographics  dataset  2  @research_team  downstream but does not reference the changed column

 2 breaking  0 at-risk  1 cleared   ·   Notify: @clinical_team, @finance_team
 ❌ MERGE BLOCKED
```
