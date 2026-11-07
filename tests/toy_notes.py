"""A second toy MCP server for the several-server tests: a private source of notes.

Run it as a script to serve it over stdio: `python tests/toy_notes.py`."""

import os

from mcp.server.mcpserver import MCPServer

server = MCPServer("notes")
RAN: list[str] = []
NOTES = {"n1": "salary review on Friday", "n2": "lunch menu"}


@server.tool(structured_output=False)
def search(query: str) -> str:
    """Notes whose text holds the query."""
    RAN.append("search")
    return "\n".join(text for text in NOTES.values() if query in text)


@server.tool(structured_output=False)
def token_seen() -> str:
    """Whether this server was started with NOTES_TOKEN in its environment."""
    RAN.append("token_seen")
    return "yes" if os.environ.get("NOTES_TOKEN") else "no"


if __name__ == "__main__":
    server.run()
