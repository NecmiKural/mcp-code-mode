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


def function_name(name: str) -> str:
    """A Python identifier for an MCP tool name: get-weather becomes get_weather. A name that
    would shadow a keyword, a builtin, a dunder such as __builtins__ or ToolError gets a
    trailing underscore."""
    ident = re.sub(r"\W", "_", name, flags=re.ASCII)
    if not ident or ident[0].isdigit():
        ident = "_" + ident
    shadows = keyword.iskeyword(ident) or ident in vars(builtins) or ident == "ToolError"
    if shadows or ident.startswith("__"):
        ident += "_"
    return ident


def load_tools(listed: Iterable[Any], allow: Iterable[str] | None = None) -> dict[str, Tool]:
    """The wrapped server's tools by Python name, only those on the allowlist (None allows
    all). A tool whose input schema is not valid JSON Schema, or whose Python name an earlier
    tool already took, is left out: a call to it could not be checked or named."""
    wanted = None if allow is None else set(allow)
    tools: dict[str, Tool] = {}
    seen = set()
    for spec in listed:
        seen.add(spec.name)
        if wanted is not None and spec.name not in wanted:
            continue
        function = function_name(spec.name)
        schema = dict(spec.input_schema)
        # NOTE: arguments the schema does not name are refused unless it says otherwise; a
        # server that quietly ignores extras would otherwise receive whatever the code adds.
        if "properties" in schema:
            schema = {"additionalProperties": False, **schema}
        checker = validators.validator_for(schema)
        try:
            checker.check_schema(schema)
        except exceptions.SchemaError:
            log.warning("left out %s: its input schema is not valid JSON Schema", spec.name)
            continue
        if function in tools:
            log.warning("left out %s: %s is taken by %s", spec.name, function, tools[function].name)
            continue
        tools[function] = Tool(spec.name, function, spec.description or "", schema, checker(schema))
    if wanted is not None and (missing := sorted(wanted - seen)):
        raise ValueError(f"not tools of the wrapped server: {missing}")
    return tools


def _type(prop: Any) -> str:
    if not isinstance(prop, dict):
        return "Any"
    kind = prop.get("type")
    if kind is None and isinstance(prop.get("anyOf"), list):
        return " | ".join(_type(p) for p in prop["anyOf"])
    if isinstance(kind, list):
        return " | ".join(_type({"type": k}) for k in kind)
    if kind == "array" and isinstance(prop.get("items"), dict):
        return f"list[{_type(prop['items'])}]"
    return TYPES.get(kind, "Any") if isinstance(kind, str) else "Any"


def render_stubs(tools: Iterable[Tool]) -> str:
    """The tools as Python stubs for the model to read: each takes one dict of the arguments
    its input schema names and returns a string."""
    blocks = []
    for tool in tools:
        schema = tool.input_schema
        required = set(schema.get("required", []))
        params = ", ".join(
            f"{name}: {_type(prop)}{'' if name in required else ' (optional)'}"
            for name, prop in schema.get("properties", {}).items()
        )
        doc = textwrap.indent(f"{tool.description.strip()}\n\nparams: {params or 'none'}", "    ")
        blocks.append(f'async def {tool.function}(params: dict) -> str:\n    """\n{doc}\n    """')
    return "\n\n".join(blocks)


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
        *,
        max_arg_chars: int = MAX_ARG_CHARS,
        max_result_chars: int = MAX_RESULT_CHARS,
        call_timeout_s: float = CALL_TIMEOUT_S,
    ) -> None:
        self.server, self.allow = server, allow
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
            self.tools = load_tools(listed, self.allow)
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
        calls = 0

        def on_call(function: str, params: Any) -> str:
            nonlocal calls
            calls += 1
            if calls > max_calls:
                raise Refused(f"at most {max_calls} tool calls per run")
            return self.call(function, params)

        return on_call
