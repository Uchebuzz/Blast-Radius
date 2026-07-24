"""Blast Radius — interactive web demo (mock mode, no DataHub required).

A thin UI over the exact same analysis engine the CLI uses (`analyze` over the
bundled `examples/sample_stack.json`). Deployed so judges can test the impact
logic from a URL without installing anything or standing up DataHub.

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

FIXTURE = Path(__file__).parent / "examples" / "sample_stack.json"

RISK_STYLE = {
    Risk.BREAKING: ("🔴", "BREAKING"),
    Risk.AT_RISK: ("🟡", "AT RISK"),
    Risk.LOW: ("🟢", "CLEARED"),
}

st.set_page_config(page_title="Blast Radius", page_icon="💥", layout="wide")


@st.cache_data
def load_stack() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


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


stack = load_stack()
datasets = {a["name"]: a for a in stack["assets"] if a["kind"] == "dataset"}

st.title("💥 Blast Radius")
st.caption(
    "See what breaks **before** you ship a schema change. This demo runs the real "
    "analysis engine over a bundled sample data stack — no DataHub setup required. "
    "The full tool reads **live DataHub** lineage via the Agent Context Kit."
)


def load_example() -> None:
    """One-click canonical scenario: drop raw.orders.customer_id."""
    st.session_state["ds_name"] = "raw.orders"
    st.session_state["column"] = "customer_id"
    st.session_state["change"] = ChangeType.DROP.value


st.button(
    "▶️  Run the example — drop `raw.orders.customer_id`",
    type="primary",
    on_click=load_example,
    help="Loads the classic scenario and shows the impact below.",
)

# -- Inputs (results recompute automatically on any change) ------------------
ds_names = list(datasets)
st.session_state.setdefault("ds_name", "raw.orders" if "raw.orders" in datasets else ds_names[0])
st.session_state.setdefault("change", ChangeType.DROP.value)

with st.sidebar:
    st.header("Proposed change")
    ds_name = st.selectbox("Dataset", ds_names, key="ds_name")
    dataset = datasets[ds_name]
    cols = dataset["columns"]
    # Reset the column when it doesn't belong to the newly-selected dataset.
    if st.session_state.get("column") not in cols:
        st.session_state["column"] = "customer_id" if "customer_id" in cols else cols[0]
    column = st.selectbox("Column", cols, key="column")
    change = st.radio("Change type", [c.value for c in ChangeType], horizontal=True, key="change")
    new_name = st.text_input("New name", value=f"{column}_v2") if change == ChangeType.RENAME.value else None
    new_type = st.text_input("New type", value="string") if change == ChangeType.RETYPE.value else None
    st.caption("Results update automatically as you change the inputs.")
    st.divider()
    st.caption("[Source & setup →](https://github.com/Uchebuzz/Blast-Radius)")

# -- Analysis ----------------------------------------------------------------
report = analyze(
    MockDataHubClient(str(FIXTURE)),
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


# -- Optional: plain-English narrative from Claude ---------------------------
def _anthropic_key() -> str | None:
    try:
        key = st.secrets.get("ANTHROPIC_API_KEY")
    except Exception:
        key = None
    return key or os.environ.get("ANTHROPIC_API_KEY")


st.divider()
st.subheader("🧠 Plain-English explanation (Claude)")
_key = _anthropic_key()
if not _key:
    st.caption(
        "Set `ANTHROPIC_API_KEY` in the app's Secrets to enable the AI narrative. "
        "Claude only *explains* the deterministic report — it can't change the verdict."
    )
elif st.button("Explain this report with Claude"):
    with st.spinner("Asking Claude…"):
        try:
            from blastradius.explain import explain_report

            st.markdown(explain_report(report, api_key=_key))
        except Exception as exc:  # surface API/network errors instead of crashing
            st.error(f"Claude call failed: {exc}")
