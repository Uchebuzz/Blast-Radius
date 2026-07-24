# Skill: Assess the blast radius of a schema change

**Category:** Data change management / lineage
**Tools used:** `search`, `get_lineage`, `list_schema_fields`, `get_dataset_queries`, `get_entities`

Use this skill when a user proposes changing a column — dropping, renaming, or
retyping it — and asks "what will this break?", "who do I need to tell?", or
"is this safe to merge?".

The goal is **precision**: report only the downstream assets that *actually
reference the changed column*, not everything that happens to sit downstream of
the table. A revenue dashboard three hops below `orders` does not break when you
drop `orders.customer_id` unless it reads that column.

## Steps

1. **Resolve the target.** If the user gave a name (`raw.orders`) rather than a
   URN, call `search` to find the dataset URN. If ambiguous, ask which one.

2. **Traverse downstream lineage.** Call `get_lineage` with
   `direction=DOWNSTREAM` from the target dataset. Walk breadth-first; cap depth
   (~10) and track visited URNs to avoid cycles. Collect every reachable asset.

3. **Gather evidence per downstream asset.** For each one:
   - `list_schema_fields` — does it expose a column whose fine-grained lineage
     traces back to the changed column?
   - `get_dataset_queries` / `get_entities` — does any real query text reference
     the changed column by name, reading from an affected upstream?

4. **Classify each asset:**
   - **BREAKING** — column-level lineage or query text ties it directly to the
     changed column.
   - **AT RISK** — downstream of an affected asset, but no lineage/query text is
     available to confirm; flag for manual review. Never silently clear these.
   - **LOW** — downstream but with evidence it does *not* touch the column.

   Propagate forward: once a mart column is confirmed to derive from the changed
   column, treat *that* column as affected for assets reading it.

5. **Attribute owners.** Use `get_entities` to pull ownership for every BREAKING
   and AT RISK asset so the right people can be notified.

6. **Report.** Summarize as: change description, counts
   (breaking / at-risk / cleared), a verdict (block vs. safe to merge),
   the impacted assets with owners and the *reason* each was flagged, and a
   per-owner migration checklist.

## Judgment rules

- Do not downgrade a BREAKING classification without new tool evidence.
- Prefer under-clearing to over-clearing: when lineage is missing, use AT RISK,
  not LOW.
- Always cite *why* — the specific column lineage or query — so reviewers can
  verify the call.
