"""A toy MCP server for the tests: tools with known answers, and a log of which ones ran.

Run it as a script to serve it over stdio: `python tests/toy_server.py`."""

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

server = MCPServer("toy")
RAN: list[str] = []


@server.tool()
def add(a: int, b: int) -> dict[str, int]:
    """Add two integers."""
    RAN.append("add")
    return {"sum": a + b}


@server.tool(structured_output=False)
def echo(text: str) -> str:
    """Return the text unchanged."""
    RAN.append("echo")
    return text


@server.tool(name="get-answer", structured_output=False)
def get_answer() -> str:
    """The answer, under a name that is not a Python identifier."""
    RAN.append("get-answer")
    return "42"


@server.tool(structured_output=False)
def fail(reason: str) -> str:
    """Always fails, with the given reason."""
    RAN.append("fail")
    raise ToolError(reason)


@server.tool(structured_output=False)
def big(size: int) -> str:
    """A string of `size` characters."""
    RAN.append("big")
    return "x" * size


@server.tool(structured_output=False)
async def slow(seconds: float) -> str:
    """Answers after the given number of seconds."""
    RAN.append("slow")
    await anyio.sleep(seconds)
    return "done"


@server.tool(structured_output=False)
def wipe() -> str:
    """Deletes everything; the allowlist tests keep it out."""
    RAN.append("wipe")
    return "wiped"


if __name__ == "__main__":
    server.run()
