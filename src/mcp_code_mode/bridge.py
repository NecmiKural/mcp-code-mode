"""The host side of code mode: the only way sandboxed code reaches the wrapped server's tools.

The bridge holds one MCP client session to the wrapped server (a stdio command, a URL or an
in-process server) and lists its tools once; each becomes a Python name the code can call.
Every call is checked before it goes out: the tool is on the allowlist, the arguments are a
JSON object within the size limit that matches the tool's input schema, and the run's call
budget is not spent. A result comes back as a string, refused when it is over the size limit.
Nothing in it is trusted: it is data for the code, and what the code prints goes back to the
model as data too.
"""

from __future__ import annotations

import asyncio
import builtins
import json
import keyword
import logging
import re
import textwrap
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any

from jsonschema import exceptions, validators
from mcp import Client, MCPError

from mcp_code_mode.sandbox import Refused

MAX_ARG_CHARS = 16 * 1024
MAX_RESULT_CHARS = 64 * 1024
MAX_CALLS = 200  # tool calls per run
CALL_TIMEOUT_S = 30.0
MAX_DESCRIPTION = 2048  # characters kept of each tool description: they are untrusted
INLINE_BUDGET = 12_000  # characters of stubs in the run_python description
MAX_SUMMARY = 120
MAX_DEPTH = 4  # levels of a schema shown in a stub type; deeper is Any
MAX_PARAM = 64  # characters shown of a parameter name
RESERVED = {"ToolError", "TOOLS", "search_tools"}  # names the runner defines
TYPES = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "array": "list",
    "object": "dict",
    "null": "None",
}

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tool:
    name: str  # as the wrapped server knows it
    function: str  # as the code calls it
    description: str
    input_schema: dict[str, Any]
    validator: Any
    private: bool = False  # may see private data: see Flow


def function_name(name: str) -> str:
    """A Python identifier for an MCP tool name: get-weather becomes get_weather. A name that
    would shadow a keyword, a builtin, a dunder such as __builtins__ or a name the runner
    defines (ToolError, TOOLS, search_tools) gets a trailing underscore."""
    ident = re.sub(r"\W", "_", name, flags=re.ASCII)
    if not ident or ident[0].isdigit():
        ident = "_" + ident
    shadows = keyword.iskeyword(ident) or ident in vars(builtins) or ident in RESERVED
    if shadows or ident.startswith("__"):
        ident += "_"
    return ident


def load_tools(
    listed: Iterable[Any],
    allow: Iterable[str] | None = None,
    private: Iterable[str] | bool = (),
    prefix: str | None = None,
) -> dict[str, Tool]:
    """The wrapped server's tools by Python name, only those on the allowlist (None allows
    all), with the tools named in `private` (True for all) marked private. A tool whose input
    schema is not valid JSON Schema, or whose Python name an earlier tool already took, is left
    out: a call to it could not be checked or named. A private label on a tool that was not
    kept is an error, so a typo cannot quietly switch the rule off. With a prefix, a tool
    `search` of server `notes` is called `notes__search`."""
    listed = list(listed)
    names = {spec.name for spec in listed}
    wanted = names if allow is None else set(allow)
    labelled = names if private is True else set(private or ())
    tools: dict[str, Tool] = {}
    for spec in listed:
        if spec.name in wanted and (tool := _tool(spec, prefix, spec.name in labelled)):
            _keep(tools, tool)
    _require(wanted, names, "not tools of the wrapped server")
    kept = {tool.name for tool in tools.values()}
    _require(labelled, kept, "labelled private but not tools this bridge kept")
    return tools


def _tool(spec: Any, prefix: str | None, private: bool) -> Tool | None:
    """One listed tool, or None when its input schema is not valid JSON Schema."""
    schema = dict(spec.input_schema)
    # NOTE: arguments the schema does not name are refused unless it says otherwise; a
    # server that quietly ignores extras would otherwise receive whatever the code adds.
    if "properties" in schema:
        schema = {"additionalProperties": False, **schema}
    checker = validators.validator_for(schema)
    try:
        checker.check_schema(schema)
    except (exceptions.SchemaError, RecursionError):  # a server can nest without end
        log.warning("left out %s: its input schema is not valid JSON Schema", spec.name)
        return None
    function = function_name(f"{prefix}__{spec.name}" if prefix else spec.name)
    description = (spec.description or "")[:MAX_DESCRIPTION]
    return Tool(spec.name, function, description, schema, checker(schema), private)


def _keep(tools: dict[str, Tool], tool: Tool) -> None:
    if (taken := tools.get(tool.function)) is not None:
        log.warning("left out %s: %s is taken by %s", tool.name, tool.function, taken.name)
        return
    tools[tool.function] = tool


def _require(names: set[str], found: set[str], problem: str) -> None:
    if missing := sorted(names - found):
        raise ValueError(f"{problem}: {missing}")


def _type(prop: Any, depth: int = 0) -> str:
    if not isinstance(prop, dict) or depth > MAX_DEPTH:  # a server can nest without end
        return "Any"
    kind = prop.get("type")
    if kind is None and isinstance(prop.get("anyOf"), list):
        return " | ".join(_type(p, depth + 1) for p in prop["anyOf"])
    if isinstance(kind, list):
        return " | ".join(_type({"type": k}, depth + 1) for k in kind)
    if kind == "array" and isinstance(prop.get("items"), dict):
        return f"list[{_type(prop['items'], depth + 1)}]"
    return TYPES.get(kind, "Any") if isinstance(kind, str) else "Any"


def _param(name: str) -> str:
    """A parameter name as shown: plain when it is a short identifier, else quoted, so a
    server cannot break a stub or a summary line with it."""
    return name if name.isidentifier() and len(name) <= MAX_PARAM else repr(name[:MAX_PARAM])


def render_stub(tool: Tool) -> str:
    """One tool as a Python stub for the model to read: it takes one dict of the arguments its
    input schema names and returns a string."""
    schema = tool.input_schema
    required = set(schema.get("required", []))
    params = ", ".join(
        f"{_param(name)}: {_type(prop)}{'' if name in required else ' (optional)'}"
        for name, prop in schema.get("properties", {}).items()
    )
    text = f"{tool.description.strip()}\n\nparams: {params or 'none'}"
    if tool.private:
        text += "\nprivate: once called, only private tools can be called"
    doc = textwrap.indent(text, "    ")
    return f'async def {tool.function}(params: dict) -> str:\n    """\n{doc}\n    """'


def render_stubs(tools: Iterable[Tool]) -> str:
    return "\n\n".join(render_stub(tool) for tool in tools)


def catalog(tools: Iterable[Tool]) -> dict[str, str]:
    """Each tool's stub by Python name, for TOOLS and search_tools inside the sandbox."""
    return {tool.function: render_stub(tool) for tool in tools}


def summary(tool: Tool) -> str:
    """One line: the name, the argument names and the description's first sentence."""
    first = re.split(r"\. |\n", tool.description.strip(), maxsplit=1)[0]
    params = ", ".join(_param(name) for name in tool.input_schema.get("properties", {}))
    label = " [private]" if tool.private else ""
    return f"{tool.function}({params}){label}: {first}"[:MAX_SUMMARY]


def render_tools(tools: Iterable[Tool], budget: int = INLINE_BUDGET) -> str:
    """The tools for the run_python description: every stub when they fit `budget`
    characters, else one summary line per tool, by name, while they fit."""
    tools = list(tools)
    if len(full := render_stubs(tools)) <= budget:
        return full
    lines: list[str] = []
    used = 0
    for line in sorted(summary(tool) for tool in tools):
        used += len(line) + (1 if lines else 0)
        if used > budget:
            break
        lines.append(line)
    rest = "search_tools(query) finds the rest and " if len(lines) < len(tools) else ""
    lines.append(
        f"{len(lines)} of {len(tools)} tools shown in one line each; "
        f"{rest}TOOLS[name] shows a stub."
    )
    return "\n".join(lines)


class Flow:
    """The private-data rule for one mcp-code-mode process. A private tool may see private
    data: its results may hold some and its arguments may carry some to its server. From the
    first admitted call to a private tool on, only private tools can be called: any other
    tool's arguments reach a server that may send them on. What one run printed is in the
    model's context, so the rule lasts for the process, not the run. Shared by every bridge of
    the process."""

    def __init__(self) -> None:
        self.first: str | None = None  # the private tool that started the rule
        self._lock = threading.Lock()  # two run_python calls can run at once

    def admit(self, tool: Tool) -> None:
        """Refuse a tool that is not private once the rule has started; start it on the first
        private tool. Called right before a call goes out, after every other check, so a call
        refused earlier starts nothing and a call that then fails still does."""
        with self._lock:
            if self.first is not None and not tool.private:
                raise Refused(
                    f"refused: {tool.function} is not private, and this session already called"
                    f" the private tool {self.first}; only private tools can be called until"
                    " mcp-code-mode restarts"
                )
            if tool.private and self.first is None:
                self.first = tool.function
                log.info("%s is private: only private tools can be called from now on", self.first)


def _text(result: Any) -> str:
    return "\n".join(getattr(c, "text", f"[{c.type} content left out]") for c in result.content)


class Bridge:
    """The wrapped server's tools, checked. Use as a context manager: it holds the session.

    `server` is anything mcp.Client accepts: StdioServerParameters, a URL, or a server object.
    """

    def __init__(
        self,
        server: Any,
        allow: Iterable[str] | None = None,
        private: Iterable[str] | bool = (),
        *,
        prefix: str | None = None,
        flow: Flow | None = None,
        max_arg_chars: int = MAX_ARG_CHARS,
        max_result_chars: int = MAX_RESULT_CHARS,
        call_timeout_s: float = CALL_TIMEOUT_S,
    ) -> None:
        self.server, self.allow, self.private = server, allow, private
        self.prefix, self.flow = prefix, flow or Flow()
        self.max_arg_chars, self.max_result_chars = max_arg_chars, max_result_chars
        self.call_timeout_s = call_timeout_s
        self.tools: dict[str, Tool] = {}

    def __enter__(self) -> Bridge:
        # The MCP session lives in one task on its own event loop thread (anyio needs it
        # opened and closed in the same task); calls reach it through a queue, so the bridge
        # works the same from a plain thread and from inside another event loop.
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._jobs: asyncio.Queue = asyncio.Queue()
        ready: Future = Future()
        self._session = asyncio.run_coroutine_threadsafe(self._serve(ready), self._loop)
        try:
            listed = ready.result()
        except BaseException:
            self._stop()
            raise
        try:
            self.tools = load_tools(listed, self.allow, self.private, self.prefix)
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        self._loop.call_soon_threadsafe(self._jobs.put_nowait, None)
        try:
            self._session.result()
        finally:
            self._stop()

    def _stop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()
        self._loop.close()

    async def _serve(self, ready: Future) -> None:
        try:
            async with Client(self.server) as client:
                listed, cursor = [], None
                while True:
                    page = await client.list_tools(cursor=cursor)
                    listed += page.tools
                    if not (cursor := page.next_cursor):
                        break
                ready.set_result(listed)
                while (job := await self._jobs.get()) is not None:
                    name, args, done = job
                    try:
                        done.set_result(
                            await client.call_tool(
                                name, args, read_timeout_seconds=self.call_timeout_s
                            )
                        )
                    except Exception as exc:
                        done.set_exception(exc)
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            raise

    def stubs(self) -> str:
        return render_stubs(self.tools.values())

    def call(self, function: str, params: Any) -> str:
        """Check one call from code, make it, and return the result as a string: the tool's
        structured content as JSON, else its text. Raises Refused for a call the code may not
        make and for a tool that failed."""
        try:
            text = self._call(function, params)
        except Refused as exc:
            log.info("refused %s: %s", str(function)[:40], exc)
            raise
        log.info("called %s", self.tools[function].name)
        return text

    def _call(self, function: str, params: Any) -> str:
        tool = self.tools.get(function)
        if tool is None:
            raise Refused(f"unknown tool {str(function)[:40]!r}")
        if not isinstance(params, dict):
            raise Refused("arguments must be an object")
        size = len(json.dumps(params))
        if size > self.max_arg_chars:
            raise Refused(f"arguments are {size} characters; the limit is {self.max_arg_chars}")
        if (error := exceptions.best_match(tool.validator.iter_errors(params))) is not None:
            raise Refused(f"invalid arguments for {function}: {error.message}")
        self.flow.admit(tool)
        result = self._send(tool.name, params)
        if result.is_error:
            raise Refused(_text(result) or f"{function} failed")
        if result.structured_content is not None:
            text = json.dumps(result.structured_content)
        else:
            text = _text(result)
        if len(text) > self.max_result_chars:
            raise Refused(
                f"the result is {len(text)} characters; the limit is {self.max_result_chars},"
                " so ask for less"
            )
        return text

    def _send(self, name: str, params: dict[str, Any]) -> Any:
        if self._session.done():
            raise Refused("the wrapped server has stopped")
        done: Future = Future()
        self._loop.call_soon_threadsafe(self._jobs.put_nowait, (name, params, done))
        try:
            return done.result(timeout=self.call_timeout_s + 5)
        except MCPError as exc:  # an error the wrapped server sent, or a timeout
            raise Refused(f"{name} failed: {exc.error.message}") from exc
        except Exception as exc:  # a fault on this side: its details stay here
            raise Refused(f"{name} failed: {type(exc).__name__}") from exc

    def budget(self, max_calls: int = MAX_CALLS) -> Callable[[str, Any], str]:
        """An on_call for one run: the bridge's checks, plus at most max_calls calls."""
        return count_calls(self.call, max_calls)


def count_calls(call: Callable[[str, Any], str], max_calls: int) -> Callable[[str, Any], str]:
    """An on_call for one run that refuses every call after the first max_calls, refused ones
    included."""
    calls = 0

    def on_call(function: str, params: Any) -> str:
        nonlocal calls
        calls += 1
        if calls > max_calls:
            raise Refused(f"at most {max_calls} tool calls per run")
        return call(function, params)

    return on_call
