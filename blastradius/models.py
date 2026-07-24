"""Core data models for Blast Radius.

These are deliberately framework-agnostic dataclasses so the traversal, scoring,
and reporting layers stay independent of how metadata was fetched (mock fixture,
DataHub MCP server, or the Agent Context Kit SDK).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ChangeType(str, Enum):
    """The kind of schema change being assessed."""

    DROP = "drop"
    RENAME = "rename"
    RETYPE = "retype"


class AssetKind(str, Enum):
    """Coarse category of a downstream asset, used for reporting and risk weighting."""

    DATASET = "dataset"
    DASHBOARD = "dashboard"
    CHART = "chart"
    ML_FEATURE = "ml_feature"
    ML_MODEL = "ml_model"
    PIPELINE = "pipeline"


class Risk(str, Enum):
    """Confidence that a downstream asset is actually affected by the change."""

    BREAKING = "breaking"  # references the changed column directly
    AT_RISK = "at_risk"    # downstream and column-lineage suggests possible reference
    LOW = "low"            # downstream but no evidence it touches the column

    @property
    def rank(self) -> int:
        return {"breaking": 3, "at_risk": 2, "low": 1}[self.value]


@dataclass(frozen=True)
class Owner:
    """An owner of a downstream asset, as recorded in DataHub."""

    urn: str
    name: str
    type: str = "user"  # "user" | "group"

    @property
    def handle(self) -> str:
        return "@" + self.name.split("@")[0].replace(" ", ".").lower()


@dataclass
class Asset:
    """A DataHub entity (dataset, dashboard, ML feature, ...)."""

    urn: str
    name: str
    kind: AssetKind
    platform: str = "unknown"
    columns: list[str] = field(default_factory=list)
    owners: list[Owner] = field(default_factory=list)
    # Raw SQL / query text known to reference this asset (from get_dataset_queries).
    queries: list[str] = field(default_factory=list)
    # Column-level upstream references: {this_column: [upstream_column, ...]}.
    upstream_field_refs: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class ChangeSpec:
    """The proposed schema change under evaluation."""

    dataset_urn: str
    column: str
    change_type: ChangeType = ChangeType.DROP
    new_name: str | None = None      # for RENAME
    new_type: str | None = None      # for RETYPE

    def describe(self) -> str:
        col = f"{self.dataset_urn}::{self.column}"
        if self.change_type is ChangeType.DROP:
            return f"DROP column {col}"
        if self.change_type is ChangeType.RENAME:
            return f"RENAME column {col} -> {self.new_name}"
        return f"RETYPE column {col} -> {self.new_type}"


@dataclass
class ImpactItem:
    """A single downstream asset and why it is (or isn't) affected."""

    asset: Asset
    depth: int
    risk: Risk
    reasons: list[str] = field(default_factory=list)
    referencing_columns: list[str] = field(default_factory=list)

    @property
    def owner_handles(self) -> list[str]:
        return [o.handle for o in self.asset.owners]


@dataclass
class ImpactReport:
    """Full result of a blast-radius analysis."""

    change: ChangeSpec
    source: Asset
    items: list[ImpactItem] = field(default_factory=list)
    edges: list[tuple[str, str]] = field(default_factory=list)  # (upstream_urn, downstream_urn)

    @property
    def breaking(self) -> list[ImpactItem]:
        return [i for i in self.items if i.risk is Risk.BREAKING]

    @property
    def at_risk(self) -> list[ImpactItem]:
        return [i for i in self.items if i.risk is Risk.AT_RISK]

    @property
    def low(self) -> list[ImpactItem]:
        return [i for i in self.items if i.risk is Risk.LOW]

    @property
    def impacted_owners(self) -> list[Owner]:
        seen: dict[str, Owner] = {}
        for item in self.breaking + self.at_risk:
            for owner in item.asset.owners:
                seen.setdefault(owner.urn, owner)
        return list(seen.values())

    def is_blocking(self) -> bool:
        """Whether this change should block a merge (used by the GitHub Action)."""
        return len(self.breaking) > 0
