"""Several wrapped servers behind one run_python: prefixed names, one call budget per run, one
private-data rule across servers, and a config file read strictly. No Docker: the toy servers
run in process."""

import json

import pytest
import toy_notes
import toy_server

from mcp_code_mode.bridge import Bridge
from mcp_code_mode.sandbox import Refused
from mcp_code_mode.server import main
from mcp_code_mode.servers import Entry, Servers, load_config


@pytest.fixture
def two():
    toy_server.RAN.clear()
    toy_notes.RAN.clear()
    entries = {
        "toy": Entry(toy_server.server, allow=["add", "echo"]),
        "notes": Entry(toy_notes.server, private=True),
    }
    with Servers(entries) as servers:
        yield servers


def test_two_servers_share_one_budget(two):
    assert set(two.tools) == {"toy__add", "toy__echo", "notes__search", "notes__token_seen"}
    assert "async def toy__add(params: dict) -> str:" in two.stubs()
    on_call = two.budget(2)
    assert json.loads(on_call("toy__add", {"a": 1, "b": 2})) == {"sum": 3}
    on_call("toy__echo", {"text": "hi"})
    with pytest.raises(Refused, match="at most 2 tool calls per run"):
        on_call("notes__search", {"query": "lunch"})
    assert toy_notes.RAN == []


def test_each_server_keeps_its_own_allowlist(two):
    with pytest.raises(Refused, match="unknown tool"):
        two.call("toy__wipe", {})
    assert toy_server.RAN == []


def test_private_on_one_server_refuses_another(two):
    assert two.call("toy__echo", {"text": "before"}) == "before"
    assert two.call("notes__search", {"query": "salary"}) == "salary review on Friday"
    with pytest.raises(Refused, match="already called the private tool notes__search"):
        two.call("toy__echo", {"text": "salary review on Friday"})
    assert toy_server.RAN == ["echo"]


def test_a_name_clash_across_servers_stops_startup():
    entries = {"x-y": Entry(toy_server.server), "x_y": Entry(toy_server.server)}
    with pytest.raises(ValueError, match="x_y__add is a tool of both"), Servers(entries):
        pass


def test_a_failing_entry_closes_the_others(monkeypatch):
    closed = []
    exit_bridge = Bridge.__exit__

    def spy(self, *exc):
        closed.append(self.prefix)
        return exit_bridge(self, *exc)

    monkeypatch.setattr(Bridge, "__exit__", spy)
    entries = {"toy": Entry(toy_server.server), "bad": Entry(toy_notes.server, allow=["nope"])}
    with (
        pytest.raises(ValueError, match="not tools of the wrapped server") as caught,
        Servers(entries),
    ):
        pass
    assert "in server 'bad'" in caught.value.__notes__
    assert "toy" in closed


def write(tmp_path, servers) -> str:
    path = tmp_path / "servers.json"
    path.write_text(json.dumps({"mcpServers": servers}))
    return str(path)


def test_the_config_is_read_as_stdio_entries(tmp_path):
    path = write(
        tmp_path,
        {
            "notes": {
                "type": "stdio",
                "command": "uv",
                "args": ["run", "notes"],
                "env": {"NOTES_TOKEN": "t"},
                "allow": ["search"],
                "private": ["search"],
            },
            "toy": {"command": "toy-server"},
        },
    )
    entries = load_config(path)
    notes, toy = entries["notes"], entries["toy"]
    assert notes.server.command == "uv" and notes.server.args == ["run", "notes"]
    assert notes.server.env == {"NOTES_TOKEN": "t"}
    assert notes.allow == ["search"] and notes.private == ["search"]
    assert toy.allow is None and toy.private == () and toy.server.args == []


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({}, "'command' is required"),
        ({"command": ""}, "'command' is required"),
        ({"url": "https://example.test/mcp"}, "'url' is not supported"),
        ({"command": "x", "headers": {"Authorization": "t"}}, "'headers' is not supported"),
        ({"command": "x", "type": "http"}, "'type' must be 'stdio'"),
        ({"command": "x", "cwd": "/tmp"}, "'cwd' is not supported"),
        ({"command": "x", "args": "run"}, "'args' must be a list of strings"),
        ({"command": "x", "env": {"A": 1}}, "'env' must map strings to strings"),
        ({"command": "x", "allow": "add"}, "'allow' must be a list of strings"),
        ({"command": "x", "private": "yes"}, "'private' must be true, false or a list"),
    ],
)
def test_config_errors_name_the_entry_and_key(tmp_path, entry, message):
    with pytest.raises(ValueError, match=f"server 'notes': {message}"):
        load_config(write(tmp_path, {"notes": entry}))


@pytest.mark.parametrize("body", ["{", "[]", '{"mcpServers": {}}', '{"servers": {}}'])
def test_a_config_without_servers_is_refused(tmp_path, body):
    path = tmp_path / "servers.json"
    path.write_text(body)
    with pytest.raises(ValueError, match="mcpServers"):
        load_config(str(path))


@pytest.mark.parametrize(
    "extra",
    [["--allow", "add"], ["--env", "A"], ["--private", "add"], ["--", "toy-server"]],
)
def test_config_excludes_single_server_options(tmp_path, capsys, extra):
    path = write(tmp_path, {"toy": {"command": "toy-server"}})
    with pytest.raises(SystemExit):
        main(["serve", "--config", path, *extra])
    assert "--config cannot be combined" in capsys.readouterr().err
