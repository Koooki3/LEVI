"""Versioned execution contracts; no HTTP, model or accelerator dependencies."""

from enum import IntEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


class Risk(IntEnum):
    READ = 0
    DRAFT = 1
    DERIVE = 2
    PUBLISH = 3
    PRIVILEGED = 4


class Resources(Contract):
    cpu_slots: int = Field(default=1, ge=1)
    memory_bytes: int = Field(ge=1)
    disk_bytes: int = Field(ge=0)
    accelerator_slots: int = Field(default=0, ge=0)


class ToolSpec(Contract):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_.]*$")
    version: str = Field(min_length=1)
    description: str = Field(min_length=1)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    permissions: tuple[str, ...] = Field(min_length=1)
    risk: Risk
    side_effects: tuple[str, ...]
    data_egress: bool
    dry_run: bool
    idempotency: Literal["read_only", "receipt_required"]
    resources: Resources
    timeout_seconds: int = Field(gt=0)
    cancellation_boundary: str = Field(min_length=1)
    errors: tuple[str, ...] = Field(min_length=1)
    retryable_errors: tuple[str, ...] = ()
    preconditions: tuple[str, ...] = ()
    postconditions: tuple[str, ...] = ()
    cache_dependencies: tuple[str, ...] = ()

    @model_validator(mode="after")
    def safe_contract(self):
        if not set(self.retryable_errors).issubset(self.errors):
            raise ValueError("Retryable errors must be declared errors")
        if self.risk > Risk.READ and self.idempotency != "receipt_required":
            raise ValueError("Side effects require persisted receipts")
        if self.risk == Risk.READ and self.side_effects:
            raise ValueError("Read-only tools cannot declare side effects")
        for schema in (self.input_schema, self.output_schema):
            if schema.get("type") != "object":
                raise ValueError("Tool envelopes must have object schemas")
        return self


class RecipeNode(Contract):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
    tool: str
    tool_version: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    depends_on: tuple[str, ...] = ()


class Recipe(Contract):
    schema_version: Literal["1"] = "1"
    nodes: tuple[RecipeNode, ...] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def valid_graph(self):
        ids = {node.id for node in self.nodes}
        if len(ids) != len(self.nodes):
            raise ValueError("Duplicate node identity")
        for node in self.nodes:
            if len(set(node.depends_on)) != len(node.depends_on):
                raise ValueError("Duplicate dependency")
            if not set(node.depends_on).issubset(ids):
                raise ValueError("Unknown dependency")
        self.execution_order()
        return self

    def execution_order(self) -> tuple[str, ...]:
        pending = {node.id: set(node.depends_on) for node in self.nodes}
        result = []
        while pending:
            ready = sorted(
                key for key, dependencies in pending.items() if not dependencies
            )
            if not ready:
                raise ValueError("Recipe contains a dependency cycle")
            result.extend(ready)
            for key in ready:
                del pending[key]
            for dependencies in pending.values():
                dependencies.difference_update(ready)
        return tuple(result)

    def affected_nodes(self, changed: set[str]) -> frozenset[str]:
        if not changed.issubset({node.id for node in self.nodes}):
            raise ValueError("Unknown changed node")
        affected = set(changed)
        by_id = {node.id: node for node in self.nodes}
        for node_id in self.execution_order():
            if affected.intersection(by_id[node_id].depends_on):
                affected.add(node_id)
        return frozenset(affected)
