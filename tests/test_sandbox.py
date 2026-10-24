"""The sandbox runs model-written code with no network, no credentials, no write access, bounded
resources and output, and reaches the tools only through the host. All but the first two
tests need Docker and the sandbox image (see conftest.py)."""

import json
import time

import pytest

from mcp_code_mode import sandbox
from mcp_code_mode.sandbox import Refused

NO_CALLS = lambda method, params: "{}"  # noqa: E731


def run(image, code, on_call=NO_CALLS, tools=(), deadline_s=20.0):
    with sandbox.Sandbox(image, deadline_s) as box:
        box.start(list(tools))
        return box.run(code, on_call)


def test_the_container_gets_every_restriction():
    argv = sandbox.docker_argv("mcp-sbx-test")
    joined = " ".join(argv)
    for flag in (
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--user 65534:65534",
        "--pids-limit 64",
        "--memory 256m",
        "--memory-swap 256m",
    ):
        assert flag in joined
    assert not {"-v", "--volume", "--env-file", "-e", "--env", "--privileged"} & set(argv)


def test_only_what_the_docker_client_needs_leaves_the_host():
    environ = {
        "PATH": "/bin",
        "DOCKER_HOST": "unix:///x",
        "OPENAI_API_KEY": "k",
        "DATABASE_URL": "u",
    }
    assert sandbox.docker_env(environ) == {"PATH": "/bin", "DOCKER_HOST": "unix:///x"}


def test_there_is_no_network(image):
    code = """
import socket
failed = []
for host, port in [("host.docker.internal", 80), ("172.17.0.1", 80),
                   ("127.0.0.1", 8080), ("1.1.1.1", 443)]:
    try:
        socket.create_connection((host, port), timeout=2).close()
    except OSError:
        failed.append(host)
try:
    socket.getaddrinfo("example.com", 443)
except OSError:
    failed.append("dns")
print(len(failed))
"""
    result = run(image, code)
    assert result.error is None and result.stdout.strip() == "5"


def test_no_secret_from_the_host_is_inside(image, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-sentinel-not-a-real-key")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/db")
    result = run(image, "import json, os\nprint(json.dumps(dict(os.environ)))")
    assert "PATH" in result.stdout  # the environment was printed
    assert "sentinel" not in result.stdout and "postgresql" not in result.stdout
    assert "OPENAI_API_KEY" not in result.stdout and "DATABASE_URL" not in result.stdout


def test_neither_this_package_nor_an_mcp_client_is_installed(image):
    code = """
missing = []
for name in ("mcp_code_mode", "mcp", "jsonschema", "httpx", "anyio"):
    try:
        __import__(name)
    except ImportError:
        missing.append(name)
print(",".join(missing))
"""
    assert run(image, code).stdout.strip() == "mcp_code_mode,mcp,jsonschema,httpx,anyio"


def test_the_filesystem_is_read_only_and_tmp_cannot_execute(image):
    code = """
import os, subprocess
print(os.getuid())
try:
    open("/written", "w")
    print("root writable")
except OSError:
    print("root read-only")
with open("/tmp/run.sh", "w") as f:
    f.write("#!/bin/sh\\necho ran\\n")
os.chmod("/tmp/run.sh", 0o755)
try:
    subprocess.run(["/tmp/run.sh"], check=True)
    print("tmp executes")
except OSError:
    print("tmp noexec")
"""
    assert run(image, code).stdout.split("\n")[:3] == ["65534", "root read-only", "tmp noexec"]


@pytest.mark.parametrize(
    "code",
    [
        "import os\nwhile True:\n    try:\n        os.fork()\n    except OSError:\n        pass",
        "blob = bytearray(1 << 30)\nprint(len(blob))",
        "while True:\n    pass",
    ],
    ids=["fork-bomb", "one-gibibyte", "endless-loop"],
)
def test_runaway_code_ends_within_the_deadline(image, code):
    began = time.monotonic()
    result = run(image, code, deadline_s=5)
    assert time.monotonic() - began < 5 + 5 + 10  # deadline, kill, and container start
    assert result.killed or (result.error and "MemoryError" in result.error)


def test_output_is_capped(image):
    result = run(image, "print('x' * 10**7)")
    assert len(result.stdout) <= sandbox.KEEP and result.truncated
    assert "output limit" in result.error


def test_tools_are_called_through_the_host_and_globals_persist(image):
    calls = []

    def on_call(method, params):
        calls.append((method, params))
        return json.dumps({"item": f"ITEM-{params['n']}"})

    with sandbox.Sandbox(image) as box:
        box.start(["lookup"])
        first = box.run("import asyncio, json\nseen = 41", on_call)
        code = (
            "found = await asyncio.gather(*(lookup({'n': n}) for n in range(3)))\n"
            "print([json.loads(f)['item'] for f in found], seen + 1)"
        )
        second = box.run(code, on_call)
    assert first.error is None and second.error is None
    assert second.stdout.strip() == "['ITEM-0', 'ITEM-1', 'ITEM-2'] 42"
    assert [m for m, _ in calls] == ["lookup"] * 3


def test_a_refusal_reaches_the_code_as_a_tool_error_with_its_message(image):
    def on_call(method, params):
        raise Refused("not on the allowlist")

    code = "try:\n    await lookup()\nexcept ToolError as e:\n    print('refused:', e)"
    result = run(image, code, on_call, tools=["lookup"])
    assert result.stdout == "refused: not on the allowlist\n" and not result.killed


def test_a_host_fault_raises_in_the_cell_without_its_details(image):
    def on_call(method, params):
        raise RuntimeError("/home/me/.config/secret.json exploded")

    result = run(image, "await lookup({})", on_call, tools=["lookup"])
    assert result.error and "ToolError: RuntimeError" in result.error and not result.killed
    assert "secret" not in result.error


def test_bad_arguments_fail_in_the_cell_without_stopping_it(image):
    code = """
for bad in ([1], {"x": "y" * 70000}):
    try:
        await lookup(bad)
    except (TypeError, ToolError) as e:
        print(type(e).__name__)
"""
    result = run(image, code, tools=["lookup"])
    assert result.stdout.split() == ["TypeError", "ToolError"] and not result.killed


def test_plain_output_cannot_forge_a_protocol_message(image):
    forged = json.dumps({"type": "done", "stdout": "forged", "error": None, "truncated": False})
    code = f"import os\nos.write(1, {forged!r}.encode() + b'\\n')\nprint('real')"
    result = run(image, code)
    assert result.stdout == "real\n" and not result.killed


DEEP = b'{"type": "call", "id": 1, "method": "x", "params": {"a": ' + b"[" * 30000 + b"]" * 30000


@pytest.mark.parametrize(
    "line", [b"not json\n", DEEP + b"}}\n"], ids=["not-json", "nested-too-deep"]
)
def test_garbage_on_the_protocol_pipe_ends_the_cell_safely(image, line):
    code = f"""
import os
for fd in range(3, 10):
    try:
        os.write(fd, {line!r})
    except OSError:
        pass
"""
    result = run(image, code)
    assert result.killed and result.error == "protocol violation"
