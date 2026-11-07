"""The bridge is the only path from sandboxed code to the wrapped server: it refuses anything
outside the allowlist, the tools' schemas, the size limits and the run's budget before the
server sees it. These tests need no Docker: the toy server runs in process."""

import json

import pytest
import toy_server
from mcp.types import Tool as McpTool

from mcp_code_mode.bridge import (
    MAX_DESCRIPTION,
    Bridge,
    catalog,
    function_name,
    load_tools,
    render_stubs,
    render_tools,
)
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
        ("TOOLS", "TOOLS_"),
        ("search_tools", "search_tools_"),
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


def test_long_descriptions_are_cut():
    listed = [McpTool(name="chatty", description="y" * 5000, input_schema={"type": "object"})]
    assert len(load_tools(listed)["chatty"].description) == MAX_DESCRIPTION


def test_the_catalog_holds_each_tools_stub(bridge):
    stubs = catalog(bridge.tools.values())
    assert set(stubs) == set(bridge.tools)
    assert stubs["add"] == render_stubs([bridge.tools["add"]])


def test_the_description_falls_back_to_summaries(bridge):
    tools = bridge.tools.values()
    full = render_stubs(tools)
    assert render_tools(tools, len(full)) == full
    brief = render_tools(tools, len(full) - 1)
    assert "async def" not in brief
    assert "add(a, b): Add two integers." in brief.splitlines()
    assert "7 of 7 tools shown in one line each" in brief
    cramped = render_tools(tools, 40)
    assert cramped.splitlines()[0] == "add(a, b): Add two integers."
    assert "1 of 7 tools shown in one line each" in cramped
    assert "search_tools(query) finds the rest" in cramped
    assert render_tools(tools, 0).startswith("0 of 7 tools")


def test_summaries_are_one_line_of_at_most_120_characters():
    listed = [
        McpTool(
            name="report",
            description="Builds the report.\nIt takes a while. " + "z" * 300,
            input_schema={"type": "object", "properties": {"year": {"type": "integer"}}},
        ),
        McpTool(name="long", description="w" * 300, input_schema={"type": "object"}),
    ]
    tools = load_tools(listed).values()
    lines = render_tools(tools, 0 + 200).splitlines()
    assert lines[0] == "long(): " + "w" * 112
    assert lines[1] == "report(year): Builds the report."


@pytest.fixture
def private_add():
    toy_server.RAN.clear()
    with Bridge(toy_server.server, private=["add"]) as b:
        yield b


def test_only_private_tools_after_a_private_call(private_add):
    assert private_add.call("echo", {"text": "before"}) == "before"
    private_add.call("add", {"a": 1, "b": 2})
    with pytest.raises(Refused) as caught:
        private_add.call("echo", {"text": "after"})
    assert str(caught.value) == (
        "refused: echo is not private, and this session already called the private tool add;"
        " only private tools can be called until mcp-code-mode restarts"
    )
    private_add.call("add", {"a": 3, "b": 4})
    assert toy_server.RAN == ["echo", "add", "add"]


def test_a_refused_private_call_does_not_taint(private_add):
    with pytest.raises(Refused, match="invalid arguments"):
        private_add.call("add", {"a": "one", "b": 2})
    assert private_add.call("echo", {"text": "still fine"}) == "still fine"


def test_a_failed_private_call_still_taints():
    with Bridge(toy_server.server, private=["fail"]) as b:
        with pytest.raises(Refused, match="secret row"):
            b.call("fail", {"reason": "secret row"})
        with pytest.raises(Refused, match="already called the private tool fail"):
            b.call("echo", {"text": "secret row"})


def test_the_taint_lasts_for_the_process(private_add):
    private_add.budget(5)("add", {"a": 1, "b": 1})
    with pytest.raises(Refused, match="not private"):
        private_add.budget(5)("echo", {"text": "a later run"})


def test_labels_must_name_kept_tools():
    with (
        pytest.raises(ValueError, match="labelled private but not tools"),
        Bridge(toy_server.server, private=["drop_table"]),
    ):
        pass
    listed = [
        McpTool(name="a_b", input_schema={"type": "object"}),
        McpTool(name="a-b", input_schema={"type": "object"}),
    ]
    with pytest.raises(ValueError, match=r"\['a-b'\]"):
        load_tools(listed, private=["a-b"])


def test_private_tools_say_so_in_stubs_and_summaries(private_add):
    tools = private_add.tools
    assert "private: once called, only private tools can be called" in render_stubs([tools["add"]])
    assert "private" not in render_stubs([tools["echo"]])
    assert "add(a, b) [private]: Add two integers." in render_tools(tools.values(), 0 + 400)
