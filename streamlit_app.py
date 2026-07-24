"""Blast Radius — interactive web demo (mock mode, no DataHub required).

A thin UI over the exact same analysis engine the CLI uses (`analyze`). It runs
over bundled fixtures so judges can test the impact logic from a URL without
installing anything or standing up DataHub. Two stacks ship:

  * DataHub's official **healthcare** sample datapack (forking pipeline)
  * a small orders → marts demo stack

The full tool reads **live DataHub** via the Agent Context Kit and can write the
verdict back into the catalog (`--write-back`); this page previews that write-back.

Run locally:   streamlit run streamlit_app.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from blastradius.analyzer import analyze
from blastradius.datahub_client import MockDataHubClient
from blastradius.models import ChangeType, Risk
from blastradius.pr import draft_migration
from blastradius.report import render_mermaid

_HERE = Path(__file__).parent

# Bundled stacks. Each has a canonical "example" scenario for the one-click button.
STACKS = {
    "DataHub healthcare sample": {
        "path": _HERE / "examples" / "healthcare" / "healthcare_stack.json",
        "dataset": "healthcare.raw_patients",
        "column": "billing_amount",
        "blurb": "DataHub's official healthcare datapack — a forking pipeline "
        "(raw → staging → billing + demographics). Drop `billing_amount` to break "
        "billing and clear demographics; drop `age` for the opposite.",
    },
    "Demo stack (orders → marts)": {
        "path": _HERE / "examples" / "sample_stack.json",
        "dataset": "raw.orders",
        "column": "customer_id",
        "blurb": "A small orders pipeline feeding two dashboards and an ML feature.",
    },
}

RISK_STYLE = {
    Risk.BREAKING: ("🔴", "BREAKING"),
    Risk.AT_RISK: ("🟡", "AT RISK"),
    Risk.LOW: ("🟢", "CLEARED"),
}
WRITEBACK_TAG = {Risk.BREAKING: "blast-radius-breaking", Risk.AT_RISK: "blast-radius-at-risk"}

st.set_page_config(page_title="Blast Radius", page_icon="💥", layout="wide")


@st.cache_data
def load_stack(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def mermaid(diagram: str, height: int = 340) -> None:
    components.html(
        f"""
        <div class="mermaid" style="background:transparent">{diagram}</div>
        <script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
        <script>mermaid.initialize({{startOnLoad:true, theme:'neutral'}});</script>
        """,
        height=height,
        scrolling=True,
    )


st.title("💥 Blast Radius")
st.caption(
    "See what breaks **before** you ship a schema change. This demo runs the real "
    "analysis engine over bundled sample data — no DataHub setup required. The full "
    "tool reads **live DataHub** via the Agent Context Kit, and can tag the verdict "
    "back into the catalog (`--write-back`)."
)


def load_example() -> None:
    """One-click canonical scenario for the currently-selected stack."""
    cfg = STACKS[st.session_state.get("stack", next(iter(STACKS)))]
    st.session_state["ds_name"] = cfg["dataset"]
    st.session_state["column"] = cfg["column"]
    st.session_state["change"] = ChangeType.DROP.value


# -- Inputs ------------------------------------------------------------------
st.session_state.setdefault("stack", next(iter(STACKS)))
with st.sidebar:
    st.header("Data stack")
    stack_name = st.selectbox("Stack", list(STACKS), key="stack")
    cfg = STACKS[stack_name]
    st.caption(cfg["blurb"])

    stack = load_stack(str(cfg["path"]))
    datasets = {a["name"]: a for a in stack["assets"] if a["kind"] == "dataset"}
    ds_names = list(datasets)

    st.header("Proposed change")
    # Reset dataset/column when they don't belong to the selected stack.
    if st.session_state.get("ds_name") not in datasets:
        st.session_state["ds_name"] = cfg["dataset"] if cfg["dataset"] in datasets else ds_names[0]
    ds_name = st.selectbox("Dataset", ds_names, key="ds_name")
    dataset = datasets[ds_name]
    cols = dataset["columns"]
    if st.session_state.get("column") not in cols:
        st.session_state["column"] = cfg["column"] if cfg["column"] in cols else cols[0]
    column = st.selectbox("Column", cols, key="column")

    st.session_state.setdefault("change", ChangeType.DROP.value)
    change = st.radio("Change type", [c.value for c in ChangeType], horizontal=True, key="change")
    new_name = st.text_input("New name", value=f"{column}_v2") if change == ChangeType.RENAME.value else None
    new_type = st.text_input("New type", value="string") if change == ChangeType.RETYPE.value else None
    st.caption("Results update automatically as you change the inputs.")
    st.divider()
    st.caption("[Source & setup →](https://github.com/Uchebuzz/Blast-Radius)")

st.button(
    f"▶️  Run the example — drop `{cfg['dataset']}.{cfg['column']}`",
    type="primary",
    on_click=load_example,
    help="Loads the canonical scenario for the selected stack and shows the impact below.",
)

# -- Analysis ----------------------------------------------------------------
report = analyze(
    MockDataHubClient(str(cfg["path"])),
    dataset["urn"],
    column,
    change_type=ChangeType(change),
    new_name=new_name,
    new_type=new_type,
)

if report.is_blocking():
    st.error(f"### ❌ MERGE BLOCKED — {len(report.breaking)} breaking downstream asset(s)")
else:
    st.success("### ✅ Safe to merge — no breaking downstream impact")

c1, c2, c3 = st.columns(3)
c1.metric("Breaking", len(report.breaking))
c2.metric("At risk", len(report.at_risk))
c3.metric("Cleared", len(report.low))

owners = ", ".join(o.handle for o in report.impacted_owners) or "—"
st.markdown(f"**Change:** `{report.change.describe()}`  \n**Notify:** {owners}")

if report.breaking:
    top = ", ".join(f"`{i.asset.name}`" for i in report.breaking[:3])
    more = " …" if len(report.breaking) > 3 else ""
    st.info(
        f"**Direct answer:** this change breaks **{len(report.breaking)}** downstream "
        f"asset(s) — {top}{more}. Notify {owners}."
    )
else:
    st.info("**Direct answer:** no breaking downstream impact — safe to merge.")

left, right = st.columns([3, 2])
with left:
    st.subheader("Downstream impact")
    rows = [
        {
            "": RISK_STYLE[item.risk][0],
            "Risk": RISK_STYLE[item.risk][1],
            "Asset": item.asset.name,
            "Kind": item.asset.kind.value,
            "Depth": item.depth,
            "Owners": ", ".join(item.owner_handles) or "—",
            "Why": " ".join(item.reasons),
        }
        for item in report.items
    ]
    st.dataframe(rows, width="stretch", hide_index=True)
with right:
    st.subheader("Lineage")
    mermaid(render_mermaid(report))

st.subheader("🛠️ Migration plan (draft)")
st.markdown(draft_migration(report))

# -- Write-back preview ------------------------------------------------------
st.divider()
st.subheader("🏷️ Write-back to DataHub")
writeback = [(i.asset.name, WRITEBACK_TAG[i.risk]) for i in report.breaking + report.at_risk]
if writeback:
    st.caption(
        "In live mode, `blastradius analyze … --write-back` tags these datasets in "
        "DataHub so the next person or agent inherits the verdict:"
    )
    for name, tag in writeback:
        st.markdown(f"- `{name}` → **{tag}**")
else:
    st.caption("Nothing to tag — no breaking or at-risk assets for this change.")


# -- Optional: plain-English narrative from Claude ---------------------------
def _anthropic_key() -> str | None:
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_KEY"):
        try:
            val = st.secrets.get(name)
        except Exception:
            val = None
        if val:
            return val
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_KEY"):
        if os.environ.get(name):
            return os.environ[name]
    return None


st.divider()
st.subheader("🧠 Plain-English explanation (Claude)")
_key = _anthropic_key()
if not _key:
    st.caption(
        "Set `ANTHROPIC_API_KEY` (or `ANTHROPIC_KEY`) in the app's Secrets as a "
        "top-level key — `ANTHROPIC_API_KEY = \"sk-ant-…\"` — to enable the AI "
        "narrative. Claude only *explains* the deterministic report — it can't "
        "change the verdict."
    )
elif st.button("Explain this report with Claude"):
    with st.spinner("Asking Claude…"):
        try:
            from blastradius.explain import explain_report

            st.markdown(explain_report(report, api_key=_key))
        except Exception as exc:  # surface API/network errors instead of crashing
            st.error(f"Claude call failed: {exc}")
