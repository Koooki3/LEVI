"""CPU-only contracts: no network, model imports or workspace mutations."""

import pytest
from pydantic import ValidationError

from levi.domain.contracts import Contract, Recipe, RecipeNode, Resources, ToolSpec
from levi.tools.registry import RegisteredTool, ToolRegistry


def node(identity, *dependencies):
    return RecipeNode(
        id=identity, tool="quality.inspect", tool_version="1", depends_on=dependencies
    )


def test_dag_invalidation_preserves_independent_work():
    recipe = Recipe(
        nodes=(
            node("export", "labels"),
            node("media"),
            node("labels", "media"),
            node("stats"),
        )
    )
    assert recipe.execution_order().index("media") < recipe.execution_order().index(
        "labels"
    )
    assert recipe.affected_nodes({"labels"}) == {"labels", "export"}
    assert recipe.affected_nodes(set()) == set()
    with pytest.raises(ValueError, match="Unknown changed"):
        recipe.affected_nodes({"missing"})


@pytest.mark.parametrize(
    "nodes",
    [
        (node("a", "b"), node("b", "a")),
        (node("a", "missing"),),
        (node("a"), node("a")),
        (node("a", "a"),),
        (node("a"), node("b", "a", "a")),
    ],
)
def test_invalid_graphs_rejected(nodes):
    with pytest.raises(ValidationError):
        Recipe(nodes=nodes)


class Input(Contract):
    count: int


class Output(Contract):
    total: int


def spec(**updates):
    fields = {
        "name": "quality.inspect",
        "version": "1",
        "description": "Inspect fixture",
        "input_schema": Input.model_json_schema(),
        "output_schema": Output.model_json_schema(),
        "permissions": ("read",),
        "risk": 0,
        "side_effects": (),
        "data_egress": False,
        "dry_run": True,
        "idempotency": "read_only",
        "resources": Resources(memory_bytes=1024, disk_bytes=0),
        "timeout_seconds": 10,
        "cancellation_boundary": "episode",
        "errors": ("INVALID_INPUT",),
    }
    return ToolSpec(**(fields | updates))


def test_effect_and_retry_contracts_fail_closed():
    for changes in (
        {"risk": 2},
        {"side_effects": ("write",)},
        {"retryable_errors": ("UNDECLARED",)},
    ):
        with pytest.raises(ValidationError):
            spec(**changes)


def test_registration_and_discovery_do_not_execute():
    registry = ToolRegistry()

    def forbidden(_):
        raise AssertionError("Discovery must not invoke handlers")

    tool = RegisteredTool(spec(), Input, Output, forbidden)
    registry.register(tool)
    assert len(registry.discover(family="quality")) == 1
    assert registry.discover(family="media") == []
    registry.validate_recipe(
        Recipe(
            nodes=(
                RecipeNode(
                    id="inspect",
                    tool="quality.inspect",
                    tool_version="1",
                    arguments={"count": 2},
                ),
            )
        )
    )
    with pytest.raises(ValueError, match="Duplicate"):
        registry.register(tool)
    with pytest.raises(ValueError, match="Unknown tool"):
        registry.resolve("quality.inspect", "2")
    with pytest.raises(ValidationError):
        registry.validate_recipe(Recipe(nodes=(node("inspect"),)))


def test_schema_drift_is_rejected():
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="Input schema"):
        registry.register(
            RegisteredTool(
                spec(input_schema={"type": "object"}),
                Input,
                Output,
                lambda value: value,
            )
        )


def test_public_contract_snapshot_matches_python_source():
    from pathlib import Path

    from levi.domain.schema_catalog import render

    root = Path(__file__).resolve().parents[1]
    assert (root / "docs/architecture/contracts.json").read_text() == render()


def test_registry_metadata_cannot_be_mutated_through_public_references():
    registry = ToolRegistry()
    contract = spec()
    registry.register(
        RegisteredTool(contract, Input, Output, lambda value: Output(total=value.count))
    )
    contract.input_schema["type"] = "string"
    exposed = registry.resolve("quality.inspect", "1")
    assert exposed.spec.input_schema["type"] == "object"
    exposed.spec.output_schema.clear()
    assert (
        registry.resolve("quality.inspect", "1").spec.output_schema
        == Output.model_json_schema()
    )
