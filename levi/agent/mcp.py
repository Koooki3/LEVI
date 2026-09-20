"""MCP facade over the same REST capability dispatcher, never a second executor."""

import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
from urllib.parse import quote


def request(path, payload=None, *, binary=False):
    from .core import ensure
    from .core import request as core_request

    token = os.getenv("LEVI_AGENT_TOKEN")
    grant_file = os.getenv("LEVI_AGENT_GRANT_FILE")
    if grant_file:
        token = Path(grant_file).read_text().strip()
    if not token:
        raise ValueError("Configure a scoped connection with levi agent connect first")
    ensure()
    return core_request(
        "/api/levi/agent/v1/" + path, payload, token=token, binary=binary
    )


def build_server():
    from mcp import types
    from mcp.server.lowlevel import Server

    server = Server(
        "levi-workbench",
        instructions="Discover scoped capabilities, read the approved plan, and use exact evidence. Names use double underscores for dots. Stop for human plan/pilot/commit approval; never invoke the human control CLI yourself. Use runs__result for artifact and review status.",
    )

    @server.list_tools()
    async def list_tools():
        value = await asyncio.to_thread(request, "capabilities")
        return [
            types.Tool(
                name=t["name"].replace(".", "__"),
                description=t["description"],
                inputSchema=t["inputSchema"],
            )
            for t in value["tools"]
            if t["available"]
        ]

    @server.call_tool()
    async def call_tool(name, arguments):
        name = name.replace("__", ".")
        value = await asyncio.to_thread(
            request, "tools", {"name": name, "arguments": arguments}
        )
        content = [types.TextContent(type="text", text=json.dumps(value))]
        if value.get("mosaic"):
            # One sheet for the whole page; individual frames stay addressable
            # by evidence id through a single-layout read.
            path = (
                "runs/"
                + quote(arguments["run_id"], safe="")
                + "/artifacts/"
                + quote(value["mosaic"]["artifact"], safe="")
            )
            image = await asyncio.to_thread(request, path, binary=True)
            content.append(
                types.ImageContent(
                    type="image",
                    mimeType="image/png",
                    data=base64.b64encode(image).decode(),
                )
            )
        elif name in {"media.sample", "evidence.read"}:
            # Return actual images through MCP, not filenames an external Agent
            # cannot resolve. Scope was already checked by the REST dispatcher.
            for item in value["items"]:
                # A run whose evidence was cleaned up still returns its ledger;
                # its rows say so, and there is no image to fetch for them.
                if item.get("artifact") and item.get("image_available", True):
                    path = (
                        "runs/"
                        + quote(arguments["run_id"], safe="")
                        + "/artifacts/"
                        + quote(item["artifact"], safe="")
                    )
                    image = await asyncio.to_thread(request, path, binary=True)
                    content.append(
                        types.ImageContent(
                            type="image",
                            mimeType="image/png",
                            data=base64.b64encode(image).decode(),
                        )
                    )
        return content

    skill_paths = {
        p.parent.name: p for p in (Path(__file__).parent / "skills").glob("*/SKILL.md")
    }

    @server.list_resources()
    async def list_resources():
        return [
            types.Resource(
                uri=f"levi://skills/{name}", name=name, mimeType="text/markdown"
            )
            for name in sorted(skill_paths)
        ]

    @server.read_resource()
    async def read_resource(uri):
        name = str(uri).removeprefix("levi://skills/")
        if name not in skill_paths:
            raise ValueError("Unknown skill resource")
        return skill_paths[name].read_text()

    @server.list_prompts()
    async def list_prompts():
        return [
            types.Prompt(
                name="review-dataset",
                description="Evidence-grounded LEVI draft workflow",
            )
        ]

    @server.get_prompt()
    async def get_prompt(name, arguments):
        if name != "review-dataset":
            raise ValueError("Unknown prompt")
        return types.GetPromptResult(
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(
                        type="text", text=skill_paths["levi-overview"].read_text()
                    ),
                )
            ]
        )

    return server


def main():
    parser = argparse.ArgumentParser(description="LEVI scoped MCP bridge (stdio)")
    parser.parse_args()
    try:
        from mcp.server.stdio import stdio_server

        server = build_server()
    except ImportError as exc:
        raise SystemExit("Install the agent extra: uv sync --extra agent") from exc

    async def serve():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(serve())
