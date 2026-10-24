"""The bridge is the only path from sandboxed code to the wrapped server: it refuses anything
outside the allowlist, the tools' schemas, the size limits and the run's budget before the
server sees it. These tests need no Docker: the toy server runs in process."""

import json

import pytest
import toy_server
from mcp.types import Tool as McpTool

from mcp_code_mode.bridge import Bridge, function_name, load_tools, render_stubs
from mcp_code_mode.sandbox import MAX_ERROR, Refused


@pytest.fixture
def bridge():
    toy_server.RAN.clear()
    with Bridge(toy_server.server) as b:
        yield b


def test_every_tool_gets_a_python_name_and_a_stub(bridge):
    assert set(bridge.tools) == {"add", "echo", "get_answer", "fail", "big", "slow", "wipe"}
    stubs = bridge.stubs()
    assert "async def add(params: dict) -> str:" in stubs
    assert "Add two integers." in stubs and "params: a: int, b: int" in stubs
    assert "async def get_answer(params: dict) -> str:" in stubs and "params: none" in stubs


@pytest.mark.parametrize(
    ("name", "function"),
    [
        ("get-weather", "get_weather"),
        ("files.read", "files_read"),
        ("2fa", "_2fa"),
        ("class", "class_"),
        ("print", "print_"),
        ("ToolError", "ToolError_"),
        ("__builtins__", "__builtins___"),
    ],
)
def test_tool_names_become_identifiers_that_shadow_nothing(name, function):
    assert function_name(name) == function


def test_a_renamed_tool_reaches_the_original(bridge):
    assert bridge.call("get_answer", {}) == "42"
    assert toy_server.RAN == ["get-answer"]


def test_structured_results_come_back_as_json_and_text_as_text(bridge):
    assert json.loads(bridge.call("add", {"a": 2, "b": 3})) == {"sum": 5}
    assert bridge.call("echo", {"text": "hi"}) == "hi"


@pytest.mark.parametrize("function", ["run_shell", "__import__", "get-answer", ""])
def test_unknown_tools_are_refused_before_the_server_sees_them(bridge, function):
    with pytest.raises(Refused, match="unknown tool"):
        bridge.call(function, {})
    assert toy_server.RAN == []


def test_the_allowlist_keeps_other_tools_out():
    toy_server.RAN.clear()
    with Bridge(toy_server.server, allow=["add", "echo"]) as b:
        assert set(b.tools) == {"add", "echo"}
        assert "wipe" not in b.stubs()
        with pytest.raises(Refused, match="unknown tool"):
            b.call("wipe", {})
    assert toy_server.RAN == []


def test_an_allowlist_naming_a_missing_tool_fails_at_start():
    with (
        pytest.raises(ValueError, match="not tools of the wrapped server"),
        Bridge(toy_server.server, allow=["add", "drop_table"]),
    ):
        pass


@pytest.mark.parametrize(
    "params",
    [
        {"a": "1", "b": 2},
        {"a": 1.5, "b": 2},
        {"a": 1},
        {"a": 1, "b": 2, "sql": "DROP TABLE"},
    ],
    ids=["wrong-type", "float-for-int", "missing", "extra"],
)
def test_arguments_outside_the_schema_are_refused(bridge, params):
    with pytest.raises(Refused, match="invalid arguments for add"):
        bridge.call("add", params)
    assert toy_server.RAN == []


def test_arguments_that_are_not_an_object_are_refused(bridge):
    with pytest.raises(Refused, match="must be an object"):
        bridge.call("add", [1, 2])
    assert toy_server.RAN == []


def test_oversized_arguments_are_refused(bridge):
    with pytest.raises(Refused, match="the limit is"):
        bridge.call("echo", {"text": "x" * bridge.max_arg_chars})
    assert toy_server.RAN == []


def test_oversized_results_are_refused_not_cut(bridge):
    assert len(bridge.call("big", {"size": 100})) == 100
    with pytest.raises(Refused, match="ask for less"):
        bridge.call("big", {"size": bridge.max_result_chars + 1})


def test_a_failing_tool_raises_its_message_capped(bridge):
    with pytest.raises(Refused, match="no such invoice"):
        bridge.call("fail", {"reason": "no such invoice"})
    with pytest.raises(Refused) as caught:
        bridge.call("fail", {"reason": "y" * 5000})
    assert len(str(caught.value)) == MAX_ERROR


def test_a_run_has_a_fixed_budget_of_calls(bridge):
    on_call = bridge.budget(3)
    for n in range(3):
        on_call("add", {"a": n, "b": 1})
    with pytest.raises(Refused, match="at most 3 tool calls per run"):
        on_call("add", {"a": 0, "b": 0})
    assert toy_server.RAN == ["add"] * 3
    bridge.budget(3)("add", {"a": 0, "b": 0})  # the next run has its own budget


def test_a_slow_tool_times_out_and_the_session_survives():
    with Bridge(toy_server.server, call_timeout_s=0.5) as b:
        with pytest.raises(Refused, match="slow failed"):
            b.call("slow", {"seconds": 5})
        assert json.loads(b.call("add", {"a": 1, "b": 1})) == {"sum": 2}


def test_a_tool_with_an_invalid_schema_or_a_taken_name_is_left_out():
    listed = [
        McpTool(name="a_b", input_schema={"type": "object"}),
        McpTool(name="a-b", input_schema={"type": "object"}),
        McpTool(name="broken", input_schema={"type": "object", "properties": {"x": {"type": 5}}}),
    ]
    assert [t.name for t in load_tools(listed).values()] == ["a_b"]


def test_stubs_show_types_and_optional_arguments():
    schema = {
        "type": "object",
        "properties": {
            "q": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "limit": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        },
        "required": ["q"],
    }
    tools = load_tools([McpTool(name="search", description="Find things.", input_schema=schema)])
    stubs = render_stubs(tools.values())
    assert "params: q: str, tags: list[str] (optional), limit: int | None (optional)" in stubs
