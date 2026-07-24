"""DataHub access layer.

`DataHubClient` is the narrow interface the rest of Blast Radius depends on. Its
methods map 1:1 onto the DataHub MCP tools so swapping the mock for a live
instance is a drop-in change:

    search              -> search
    get_lineage         -> get_lineage
    list_schema_fields  -> list_schema_fields
    get_dataset_queries -> get_dataset_queries
    get_entity          -> get_entities

Two implementations ship here:

  * MockDataHubClient  — backed by a JSON fixture, zero setup, powers the demo.
  * McpDataHubClient   — talks to a real DataHub MCP server / Agent Context Kit.

Select with `DataHubClient.from_env()`.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from pathlib import Path

from .models import Asset, AssetKind, Owner

_DEFAULT_FIXTURE = Path(__file__).resolve().parent.parent / "examples" / "sample_stack.json"


class DataHubClient(ABC):
    """Narrow read interface over DataHub metadata, mirroring the MCP tools."""

    @abstractmethod
    def get_entity(self, urn: str) -> Asset | None:
        """Return full metadata for one entity (get_entities)."""

    @abstractmethod
    def list_schema_fields(self, urn: str) -> list[str]:
        """Return column names for a dataset (list_schema_fields)."""

    @abstractmethod
    def get_downstream(self, urn: str) -> list[str]:
        """Return direct downstream entity URNs (get_lineage, direction=downstream)."""

    @abstractmethod
    def get_dataset_queries(self, urn: str) -> list[str]:
        """Return SQL/query text referencing this dataset (get_dataset_queries)."""

    def search(self, query: str) -> list[str]:  # pragma: no cover - convenience
        """Best-effort URN lookup by name (search)."""
        raise NotImplementedError

    @staticmethod
    def from_env() -> "DataHubClient":
        """Pick an implementation from environment configuration.

        Uses a live MCP client when DATAHUB_GMS_URL is set, otherwise the mock.
        """
        if os.environ.get("DATAHUB_GMS_URL"):
            return McpDataHubClient(
                gms_url=os.environ["DATAHUB_GMS_URL"],
                token=os.environ.get("DATAHUB_GMS_TOKEN"),
            )
        return MockDataHubClient()


class MockDataHubClient(DataHubClient):
    """In-memory client backed by a JSON fixture. Powers the offline demo."""

    def __init__(self, fixture: str | Path | None = None):
        path = Path(fixture) if fixture else _DEFAULT_FIXTURE
        data = json.loads(path.read_text(encoding="utf-8"))
        self._assets: dict[str, Asset] = {}
        for raw in data["assets"]:
            self._assets[raw["urn"]] = Asset(
                urn=raw["urn"],
                name=raw["name"],
                kind=AssetKind(raw["kind"]),
                platform=raw.get("platform", "unknown"),
                columns=list(raw.get("columns", [])),
                owners=[Owner(**o) for o in raw.get("owners", [])],
                queries=list(raw.get("queries", [])),
                upstream_field_refs={k: list(v) for k, v in raw.get("upstream_field_refs", {}).items()},
            )
        # downstream adjacency: upstream_urn -> [downstream_urn, ...]
        self._downstream: dict[str, list[str]] = {}
        for up, down in data.get("lineage", []):
            self._downstream.setdefault(up, []).append(down)

    def get_entity(self, urn: str) -> Asset | None:
        return self._assets.get(urn)

    def list_schema_fields(self, urn: str) -> list[str]:
        asset = self._assets.get(urn)
        return list(asset.columns) if asset else []

    def get_downstream(self, urn: str) -> list[str]:
        return list(self._downstream.get(urn, []))

    def get_dataset_queries(self, urn: str) -> list[str]:
        asset = self._assets.get(urn)
        return list(asset.queries) if asset else []

    def search(self, query: str) -> list[str]:
        q = query.lower()
        return [a.urn for a in self._assets.values() if q in a.name.lower() or q in a.urn.lower()]


class McpDataHubClient(DataHubClient):
    """Live client backed by the DataHub Agent Context Kit.

    Reads go through the Agent Context Kit's MCP tools — ``search``,
    ``get_lineage``, ``get_entities``, ``get_dataset_queries`` — with the shared
    ``DataHubGraph`` (from the same context) used for the two column-level
    signals the tools don't surface directly: dataset fine-grained lineage
    (``upstreamLineage``) and dashboard consumption (``inputFields``).

    Wire-up is lazy-imported so the package installs and the offline demo run
    without the ``datahub`` extra. Install with:

        pip install "blast-radius[datahub]"

    and set DATAHUB_GMS_URL (+ DATAHUB_GMS_TOKEN) to activate.
    """

    def __init__(self, gms_url: str, token: str | None = None):
        self.gms_url = gms_url
        self.token = token
        self._sdk = None
        self._graph = None
        self._connect()

    def _connect(self) -> None:  # pragma: no cover - requires live DataHub
        try:
            from datahub.sdk.main_client import DataHubClient as _SDKClient
            from datahub_agent_context.context import set_client
        except ImportError as exc:
            raise RuntimeError(
                "Live DataHub access needs the 'datahub' extra: "
                'pip install "blast-radius[datahub]"'
            ) from exc
        self._sdk = _SDKClient(server=self.gms_url, token=self.token)
        # Register the client in the Agent Context Kit context so the MCP tools
        # (which read it via contextvars) work for the rest of this process.
        set_client(self._sdk)
        self._graph = self._sdk._graph

    # -- URN parsing helpers -------------------------------------------------
    @staticmethod
    def _dataset_name(ds_urn: str) -> str:
        """urn:li:dataset:(urn:li:dataPlatform:dbt,staging.stg_orders,PROD) -> staging.stg_orders"""
        prefix = "urn:li:dataset:("
        if not ds_urn.startswith(prefix):
            return ds_urn
        inner = ds_urn[len(prefix):]
        if inner.endswith(")"):
            inner = inner[:-1]
        parts = inner.split(",")
        return parts[1] if len(parts) >= 2 else ds_urn

    @classmethod
    def _field_ref(cls, schema_field_urn: str) -> str:
        """schemaField urn -> 'datasetName::column' (the analyzer's field key)."""
        prefix = "urn:li:schemaField:("
        inner = schema_field_urn[len(prefix):] if schema_field_urn.startswith(prefix) else schema_field_urn
        if inner.endswith(")"):
            inner = inner[:-1]
        ds_urn, _, col = inner.rpartition(",")
        return f"{cls._dataset_name(ds_urn)}::{col}"

    @staticmethod
    def _field_of(schema_field_urn: str) -> str:
        col = schema_field_urn.rpartition(",")[2]
        return col[:-1] if col.endswith(")") else col

    # -- DataHubClient interface --------------------------------------------
    def get_downstream(self, urn: str) -> list[str]:  # pragma: no cover
        from datahub_agent_context.mcp_tools.lineage import get_lineage

        res = get_lineage(urn=urn, upstream=False, max_hops=1)
        results = res.get("downstreams", {}).get("searchResults", [])
        return [r["entity"]["urn"] for r in results if r.get("entity", {}).get("urn")]

    def get_entity(self, urn: str) -> Asset | None:  # pragma: no cover
        from datahub_agent_context.mcp_tools.entities import get_entities

        try:
            raw = get_entities(urns=[urn])
        except Exception:
            raw = []
        entity = raw[0] if raw else {}
        kind = _infer_kind({"urn": urn})
        # Datasets expose "name"; dashboards/charts carry it under properties.
        name = (
            entity.get("name")
            or (entity.get("properties") or {}).get("name")
            or self._dataset_name(urn)
            or urn
        )
        platform = (entity.get("platform") or {}).get("name", "unknown")
        columns = [
            f["fieldPath"] for f in (entity.get("schemaMetadata") or {}).get("fields", [])
        ]
        owners = []
        for o in (entity.get("ownership") or {}).get("owners", []):
            owner_urn = (o.get("owner") or {}).get("urn")
            if owner_urn:
                otype = "group" if ":corpGroup:" in owner_urn else "user"
                owners.append(Owner(urn=owner_urn, name=owner_urn.split(":")[-1], type=otype))
        return Asset(
            urn=urn,
            name=name,
            kind=kind,
            platform=platform,
            columns=columns,
            owners=owners,
            queries=self._queries(urn, kind),
            upstream_field_refs=self._field_refs(urn, kind),
        )

    def list_schema_fields(self, urn: str) -> list[str]:  # pragma: no cover
        asset = self.get_entity(urn)
        return asset.columns if asset else []

    def get_dataset_queries(self, urn: str) -> list[str]:  # pragma: no cover
        return self._queries(urn, _infer_kind({"urn": urn}))

    def search(self, query: str) -> list[str]:  # pragma: no cover
        from datahub_agent_context.mcp_tools.search import search as _search

        try:
            res = _search(query=query)
        except Exception:
            return []
        return [
            r["entity"]["urn"]
            for r in res.get("searchResults", [])
            if r.get("entity", {}).get("urn")
        ]

    # -- Column-level signals -----------------------------------------------
    def _queries(self, urn: str, kind: AssetKind) -> list[str]:  # pragma: no cover
        # DataHub's listQueries only supports dataset subjects.
        if kind is not AssetKind.DATASET:
            return []
        from datahub_agent_context.mcp_tools.queries import get_dataset_queries

        try:
            res = get_dataset_queries(urn=urn)
        except Exception:
            return []
        out = []
        for q in res.get("queries", []):
            value = (q.get("properties") or {}).get("statement", {}).get("value")
            if value:
                out.append(value)
        return out

    def _field_refs(self, urn: str, kind: AssetKind) -> dict[str, list[str]]:  # pragma: no cover
        """Column-level upstreams, mapped to the analyzer's 'name::col' keys.

        Datasets carry fine-grained lineage on the ``upstreamLineage`` aspect;
        dashboards record consumed upstream columns on ``inputFields``.
        """
        refs: dict[str, list[str]] = {}
        try:
            if kind is AssetKind.DASHBOARD:
                from datahub.metadata.schema_classes import InputFieldsClass

                aspect = self._graph.get_aspect(urn, InputFieldsClass)
                for f in getattr(aspect, "fields", None) or []:
                    sf = f.schemaFieldUrn
                    refs.setdefault(self._field_of(sf), []).append(self._field_ref(sf))
            else:
                from datahub.metadata.schema_classes import UpstreamLineageClass

                aspect = self._graph.get_aspect(urn, UpstreamLineageClass)
                for fgl in getattr(aspect, "fineGrainedLineages", None) or []:
                    upstreams = [self._field_ref(u) for u in (fgl.upstreams or [])]
                    for down in fgl.downstreams or []:
                        refs.setdefault(self._field_of(down), []).extend(upstreams)
        except Exception:
            pass
        return refs


def _infer_kind(entity: dict) -> AssetKind:  # pragma: no cover - requires live DataHub
    urn = entity.get("urn", "")
    if "dashboard" in urn:
        return AssetKind.DASHBOARD
    if "chart" in urn:
        return AssetKind.CHART
    if "mlFeature" in urn:
        return AssetKind.ML_FEATURE
    if "mlModel" in urn:
        return AssetKind.ML_MODEL
    if "dataFlow" in urn or "dataJob" in urn:
        return AssetKind.PIPELINE
    return AssetKind.DATASET
