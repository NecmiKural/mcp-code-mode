"""Several wrapped servers behind one run_python.

`load_config` reads the `mcpServers` shape MCP clients already use, stdio entries only, plus
`allow` and `private` per server. `Servers` opens one bridge per entry; each tool is called
by its server's name and its own (`notes__search`), every server shares one private-data rule,
and one call budget covers a run across all of them. It offers what `run_code` and
`build_server` use from a bridge: `tools`, `stubs()`, `call()` and `budget()`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Any

from mcp import StdioServerParameters

from mcp_code_mode.bridge import MAX_CALLS, Bridge, Flow, Tool, count_calls, render_stubs
from mcp_code_mode.sandbox import Refused

KEYS = {"type", "command", "args", "env", "allow", "private"}


@dataclass(frozen=True)
class Entry:
    server: Any  # anything mcp.Client accepts; load_config gives StdioServerParameters
    allow: list[str] | None = None
    private: Iterable[str] | bool = ()


def _strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _entry(name: str, raw: Any) -> Entry:
    def fail(problem: str) -> ValueError:
        return ValueError(f"server {name!r}: {problem}")

    if not isinstance(raw, dict):
        raise fail("must be an object")
    if extra := sorted(set(raw) - KEYS):
        raise fail(
            f"{extra[0]!r} is not supported; an entry is a stdio server with command, args,"
            " env, allow and private"
        )
    if raw.get("type", "stdio") != "stdio":
        raise fail("'type' must be 'stdio'")
    if not isinstance(command := raw.get("command"), str) or not command:
        raise fail("'command' is required")
    args, env = raw.get("args", []), raw.get("env", {})
    if not _strings(args):
        raise fail("'args' must be a list of strings")
    if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
        raise fail("'env' must map strings to strings")  # passed as written: no ${VAR}
    allow, private = raw.get("allow"), raw.get("private", ())
    if allow is not None and not _strings(allow):
        raise fail("'allow' must be a list of strings")
    if not isinstance(private, bool | tuple) and not _strings(private):
        raise fail("'private' must be true, false or a list of tool names")
    params = StdioServerParameters(command=command, args=args, env=env or None)
    return Entry(params, allow, private)


def load_config(path: str) -> dict[str, Entry]:
    """The entries of a config file, checked strictly: an unsupported key stops startup with
    the entry and the key in the message, rather than being half supported."""
    try:
        with open(path, encoding="utf-8") as file:
            config = json.load(file)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not JSON with an mcpServers object: {exc}") from exc
    servers = config.get("mcpServers") if isinstance(config, dict) else None
    if not isinstance(servers, dict) or not servers:
        raise ValueError(f"{path} needs an mcpServers object with at least one server")
    return {name: _entry(name, raw) for name, raw in servers.items()}


class Servers:
    """The entries' tools, checked, as one set. Use as a context manager: it holds a session
    per server."""

    def __init__(self, entries: dict[str, Entry], **options: Any) -> None:
        self.entries, self.options = entries, options
        self.tools: dict[str, Tool] = {}
        self._owner: dict[str, Bridge] = {}

    def __enter__(self) -> Servers:
        flow = Flow()
        with ExitStack() as stack:
            for name, entry in self.entries.items():
                bridge = Bridge(
                    entry.server, entry.allow, entry.private, prefix=name, flow=flow, **self.options
                )
                try:
                    stack.enter_context(bridge)
                except BaseException as exc:
                    exc.add_note(f"in server {name!r}")
                    raise
                self._add(name, bridge)
            self._stack = stack.pop_all()
        return self

    def _add(self, name: str, bridge: Bridge) -> None:
        for function, tool in bridge.tools.items():
            if (owner := self._owner.get(function)) is not None:
                raise ValueError(
                    f"{function} is a tool of both {owner.prefix!r} and {name!r}; rename a server"
                )
            self.tools[function], self._owner[function] = tool, bridge

    def __exit__(self, *exc: object) -> None:
        self._stack.close()

    def stubs(self) -> str:
        return render_stubs(self.tools.values())

    def call(self, function: str, params: Any) -> str:
        bridge = self._owner.get(function)
        if bridge is None:
            raise Refused(f"unknown tool {str(function)[:40]!r}")
        return bridge.call(function, params)

    def budget(self, max_calls: int = MAX_CALLS) -> Callable[[str, Any], str]:
        """An on_call for one run: each server's checks, plus at most max_calls calls across
        all of them."""
        return count_calls(self.call, max_calls)
