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
    """Live client backed by the DataHub MCP server / Agent Context Kit.

    Wire-up is intentionally lazy-imported so the package installs and the demo
    runs without the `datahub` extra. Install with:

        pip install "blast-radius[datahub]"

    and set DATAHUB_GMS_URL (+ DATAHUB_GMS_TOKEN) to activate.
    """

    def __init__(self, gms_url: str, token: str | None = None):
        self.gms_url = gms_url
        self.token = token
        self._client = self._connect()

    def _connect(self):  # pragma: no cover - requires live DataHub
        try:
            # The Agent Context Kit exposes the MCP tools through a Python client.
            from datahub_agent_context import DataHubContextClient  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Live DataHub access needs the 'datahub' extra: "
                "pip install \"blast-radius[datahub]\""
            ) from exc
        return DataHubContextClient(server=self.gms_url, token=self.token)

    def get_entity(self, urn: str) -> Asset | None:  # pragma: no cover - requires live DataHub
        raw = self._client.get_entities(urns=[urn])
        if not raw:
            return None
        entity = raw[0]
        return Asset(
            urn=entity["urn"],
            name=entity.get("name", entity["urn"]),
            kind=_infer_kind(entity),
            platform=entity.get("platform", "unknown"),
            columns=[f["fieldPath"] for f in entity.get("schemaFields", [])],
            owners=[
                Owner(urn=o["urn"], name=o.get("name", o["urn"]), type=o.get("type", "user"))
                for o in entity.get("owners", [])
            ],
            queries=[q["text"] for q in entity.get("queries", [])],
            upstream_field_refs=entity.get("fineGrainedUpstreams", {}),
        )

    def list_schema_fields(self, urn: str) -> list[str]:  # pragma: no cover
        return [f["fieldPath"] for f in self._client.list_schema_fields(urn=urn)]

    def get_downstream(self, urn: str) -> list[str]:  # pragma: no cover
        result = self._client.get_lineage(urn=urn, direction="DOWNSTREAM", degree=1)
        return [e["urn"] for e in result.get("entities", [])]

    def get_dataset_queries(self, urn: str) -> list[str]:  # pragma: no cover
        return [q["text"] for q in self._client.get_dataset_queries(urn=urn)]

    def search(self, query: str) -> list[str]:  # pragma: no cover
        return [e["urn"] for e in self._client.search(query=query).get("entities", [])]


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
