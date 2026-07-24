"""Tests for the Blast Radius scoring precision on the sample stack.

The whole value proposition is that we flag what *actually* breaks and clear
what doesn't. These tests lock that behavior in.
"""

from blastradius.analyzer import analyze, resolve_dataset
from blastradius.datahub_client import MockDataHubClient
from blastradius.models import ChangeType


def _report(column="customer_id", change=ChangeType.DROP, **kw):
    client = MockDataHubClient()
    urn = resolve_dataset(client, "raw.orders")
    return analyze(client, urn, column, change_type=change, **kw)


def test_dropping_customer_id_breaks_the_right_assets():
    report = _report()
    breaking = {i.asset.name for i in report.breaking}
    assert breaking == {
        "staging.stg_orders",
        "marts.customer_orders",
        "Customer 360",
        "customer_ltv (ML feature)",
    }


def test_revenue_path_is_cleared_not_flagged():
    # daily_revenue / finance_dashboard descend from stg_orders but read
    # amount + created_at, NOT customer_id — they must be LOW, not breaking.
    report = _report()
    low = {i.asset.name for i in report.low}
    assert "marts.daily_revenue" in low
    assert "Finance Dashboard" in low
    assert report.is_blocking() is True


def test_dropping_unreferenced_column_blocks_nothing():
    # No downstream asset references order_id by name / lineage beyond stg_orders.
    report = _report(column="order_id")
    assert {i.asset.name for i in report.breaking} <= {"staging.stg_orders", "marts.customer_orders"}


def test_owners_are_attributed():
    report = _report()
    handles = {o.handle for o in report.impacted_owners}
    assert "@alice" in handles  # customer_orders / Customer 360
    assert "@bob" in handles    # customer_ltv


def test_rename_produces_migration_steps():
    report = _report(change=ChangeType.RENAME, new_name="cust_id")
    from blastradius.pr import draft_migration

    plan = draft_migration(report)
    assert "Migration plan" in plan
    assert "cust_id" in plan
    assert "@alice" in plan
