"""Contract registry. Authorization remains in the application gateway.

Registration is intentionally separate from execution: discovering a tool never
imports a worker, calls a model, or grants permission to invoke its handler.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace

from pydantic import BaseModel

from levi.domain.contracts import Recipe, ToolSpec


@dataclass(frozen=True)
class RegisteredTool:
    spec: ToolSpec
    input_type: type[BaseModel]
    output_type: type[BaseModel]
    handler: Callable[[BaseModel], BaseModel]


class ToolRegistry:
    def __init__(self):
        self._tools: dict[tuple[str, str], RegisteredTool] = {}

    def register(self, tool: RegisteredTool) -> None:
        key = (tool.spec.name, tool.spec.version)
        if key in self._tools:
            raise ValueError(f"Duplicate tool version: {key}")
        if tool.spec.input_schema != tool.input_type.model_json_schema():
            raise ValueError("Input schema does not match the registered type")
        if tool.spec.output_schema != tool.output_type.model_json_schema():
            raise ValueError("Output schema does not match the registered type")
        self._tools[key] = replace(tool, spec=tool.spec.model_copy(deep=True))

    def resolve(self, name: str, version: str) -> RegisteredTool:
        try:
            tool = self._tools[(name, version)]
            return replace(tool, spec=tool.spec.model_copy(deep=True))
        except KeyError:
            raise ValueError(f"Unknown tool version: {name}@{version}") from None

    def discover(self, *, family: str | None = None) -> list[ToolSpec]:
        return [
            entry.spec.model_copy(deep=True)
            for key, entry in sorted(self._tools.items())
            if family is None or key[0].split(".", 1)[0] == family
        ]

    def validate_recipe(self, recipe: Recipe) -> None:
        for node in recipe.nodes:
            tool = self.resolve(node.tool, node.tool_version)
            tool.input_type.model_validate(node.arguments)
