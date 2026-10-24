"""End to end with no model and no API key: the CLI wraps the toy server over stdio, and
scripted code calls its tools from the sandbox through the bridge. Needs Docker and the
sandbox image (see conftest.py)."""

import os
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

pytestmark = pytest.mark.anyio

TOY = Path(__file__).with_name("toy_server.py")
CLI = Path(sys.executable).with_name("mcp-code-mode")
SENTINEL = "sk-sentinel-not-a-real-key-000000"
ALLOW = ("--allow", "add", "--allow", "echo", "--allow", "get-answer")


def cli(*options: str) -> StdioServerParameters:
    """The CLI as an MCP client would start it, with a secret in its environment."""
    docker = {k: os.environ[k] for k in ("DOCKER_HOST", "DOCKER_CONTEXT") if k in os.environ}
    return StdioServerParameters(
        command=str(CLI),
        args=["serve", *ALLOW, *options, "--", sys.executable, str(TOY)],
        env=docker | {"SENTINEL_API_KEY": SENTINEL},
    )


async def run(client: Client, code: str) -> dict:
    result = await client.call_tool("run_python", {"code": code})
    assert not result.is_error, result
    return result.structured_content


async def test_the_wrapped_server_becomes_one_tool_listing_its_stubs(image):
    async with Client(cli()) as client:
        [tool] = (await client.list_tools()).tools
    assert tool.name == "run_python"
    assert "async def add(params: dict) -> str:" in tool.description
    assert "async def get_answer(params: dict) -> str:" in tool.description
    assert "wipe" not in tool.description


async def test_code_calls_the_tools_through_the_bridge(image):
    code = """
import asyncio, json
sums = await asyncio.gather(*(add({'a': n, 'b': 10}) for n in range(3)))
print([json.loads(s)['sum'] for s in sums], await echo({'text': 'hi'}), await get_answer())
print(sorted(name for name in globals() if not name.startswith('__')))
"""
    async with Client(cli()) as client:
        out = await run(client, code)
    assert out["error"] is None and not out["stopped"]
    assert out["stdout"].splitlines() == [
        "[10, 11, 12] hi 42",
        "['ToolError', 'add', 'asyncio', 'echo', 'get_answer', 'json', 'sums']",
    ]


async def test_refusals_reach_the_code_and_the_budget_holds(image):
    code = """
done = 0
for n in range(5):
    try:
        await add({'a': n, 'b': 'one' if n == 0 else 1})
        done += 1
    except ToolError as e:
        print(str(e)[:28])
print(done)
"""
    async with Client(cli("--max-calls", "3")) as client:
        out = await run(client, code)
    assert out["stdout"].splitlines() == [
        "invalid arguments for add: '",
        "at most 3 tool calls per run",
        "at most 3 tool calls per run",
        "2",
    ]


async def test_nothing_from_the_hosts_environment_reaches_the_code(image):
    async with Client(cli()) as client:
        out = await run(client, "import json, os\nprint(json.dumps(dict(os.environ)))")
    assert "PATH" in out["stdout"]
    assert SENTINEL not in out["stdout"] and "SENTINEL_API_KEY" not in out["stdout"]


async def test_runaway_code_is_stopped_and_the_next_run_starts_fresh(image):
    async with Client(cli("--deadline", "5")) as client:
        first = await run(client, "x = 1\nwhile True:\n    pass")
        second = await run(client, "print('x' in globals())")
    assert first["stopped"] and first["error"] == "deadline exceeded"
    assert second["stdout"] == "False\n" and not second["stopped"]
