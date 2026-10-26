# mcp-code-mode

Code mode for any MCP server, in Python. Instead of calling a server's tools one at a time, the model writes a short Python program that calls them as async functions. The program runs in a locked-down Docker container, and every call it makes goes through a host-side bridge that checks it before the server sees it.

```
MCP client --run_python(code)--> mcp-code-mode --docker run--> sandbox: runner.py + the code
                                   |      ^                            |
                                   |      +-- bridge checks each <-----+  one JSON line per call
                                   v
                       wrapped MCP server (stdio)
```

It takes any server's tools from `tools/list`, turns each into an async Python function inside the sandbox and a stub the model can read, and serves them as a single MCP tool, `run_python`. The package runs no model and needs no API key.

## Where it comes from

This is the code-mode machinery of [bank-reconciliation-agent](https://github.com/NecmiKural/bank-reconciliation-agent), without anything specific to bank reconciliation. There it ran in a pre-registered experiment ([design](https://github.com/NecmiKural/bank-reconciliation-agent/blob/main/docs/code-mode-experiment.md), [results](https://github.com/NecmiKural/bank-reconciliation-agent#code-mode)): containment held in every run and every probe, and code mode was cheaper on average, but the pre-registered efficiency hypothesis failed for both models tested. Read the numbers there; this package carries no measurements of its own.

## Quick start

Needs Python 3.13, [uv](https://docs.astral.sh/uv/) and Docker.

```bash
git clone https://github.com/NecmiKural/mcp-code-mode && cd mcp-code-mode
uv sync
uv run mcp-code-mode build-image
```

Point an MCP client at `mcp-code-mode serve`. Everything after `--` is the wrapped server's command; here it is the toy server from the tests. For example, in a `.mcp.json`:

```json
{
  "mcpServers": {
    "code-mode": {
      "command": "uv",
      "args": ["--directory", "/path/to/mcp-code-mode", "run", "mcp-code-mode", "serve",
               "--allow", "add", "--allow", "echo", "--", "uv", "run", "python", "tests/toy_server.py"],
      "env": { "DOCKER_HOST": "unix:///path/to/docker.sock" }
    }
  }
}
```

`env` is only needed when Docker is not at its default socket (Rancher Desktop, Colima). The client then lists one tool, `run_python`, whose description carries a stub per wrapped tool:

```python
async def add(params: dict) -> str:
    """
    Add two integers.

    params: a: int, b: int
    """
```

Code the model might send:

```python
import asyncio, json

sums = await asyncio.gather(*(add({"a": n, "b": 10}) for n in range(3)))
print([json.loads(s)["sum"] for s in sums])
```

It comes back as `{"stdout": "[10, 11, 12]\n", "error": null, "truncated": false, "stopped": false}`.

Options for `serve`: `--allow TOOL` (repeatable; default all tools), `--env NAME` (pass that variable from this environment to the wrapped server; repeatable), `--image`, `--deadline` in seconds per run, and `--max-calls` per run.

## Python API

The bridge takes anything `mcp.Client` accepts: `StdioServerParameters`, a URL, or a server object in the same process.

```python
import sys

from mcp import StdioServerParameters

from mcp_code_mode import Bridge, run_code

toy = StdioServerParameters(command=sys.executable, args=["tests/toy_server.py"])
with Bridge(toy, allow=["add", "echo"]) as bridge:
    print(bridge.stubs())  # for your own prompt
    cell = run_code(bridge, "print(await add({'a': 2, 'b': 3}))")
    print(cell.stdout)  # {"sum": 5}
```

For your own agent loop, with variables kept between cells, hold one `Sandbox` and pass `bridge.budget(n)` as each cell's `on_call`. `build_server(bridge)` returns the `run_python` server without starting it.

## Agent skill

[`skills/mcp-code-mode/SKILL.md`](skills/mcp-code-mode/SKILL.md) tells an agent when to reach for `run_python`, how to write the code and what the limits are. For Claude Code, copy the folder into `~/.claude/skills/` or into a project's `.claude/skills/`; agents that read `SKILL.md` files load it the same way. It is free, like the rest of this repository, and a test keeps its numbers in line with the code.

## How a call works

1. `run_python` starts a fresh container holding Python and `runner.py`, nothing else. Each wrapped tool is an async function in the code's globals. MCP names that are not Python identifiers are renamed (`get-weather` becomes `get_weather`), and a name that would shadow a keyword, a builtin or a dunder such as `__builtins__` gets a trailing underscore.
2. A tool call writes one JSON line to the runner's private copy of stdout. The host reads it as untrusted: if it is too long, not JSON or not a known shape, the run ends as a protocol violation.
3. The bridge checks the call: the tool is on the allowlist, the arguments are an object of at most 16 KiB that validates against the tool's input schema (arguments the schema does not name are refused unless it allows them), and the run has calls left. Only then does it call the wrapped server, with a 30 second timeout.
4. The result goes back to the code as a string: the structured content as JSON, else the text. A result over 64 KiB is refused, not cut. A refused or failed call raises `ToolError` in the code, with a message of at most 500 characters; a fault on the host side reaches the code only as an exception type name.
5. The run ends when the code finishes, at the deadline (90 seconds), or on a protocol violation. Only what the code printed comes back, at most 8 KiB, with the last lines of a traceback if it failed.

## Threat model

The code is written by a model that may be confused or steered by text it read, so it is treated as hostile.

### What is protected

- **The host:** its files, environment variables, credentials, network position and resources.
- **The wrapped server:** from calls outside the allowlist, malformed or oversized arguments, and call floods.
- **The model's context:** from runaway output.

### Controls

| Threat | Control | Test |
|---|---|---|
| Code reads a key or token from the host | The container gets no environment from the host, no mounts and no Docker socket. The docker client itself gets only `PATH`, `HOME`, `DOCKER_HOST` and `DOCKER_CONTEXT`. The image holds Python and `runner.py` only: no MCP client, no copy of this package | `test_no_secret_from_the_host_is_inside`, `test_neither_this_package_nor_an_mcp_client_is_installed`, `test_nothing_from_the_hosts_environment_reaches_the_code` |
| Code reaches the network | `--network none` | `test_there_is_no_network` |
| Code writes or plants files | Read-only root; `/tmp` is a 16 MiB tmpfs mounted `noexec,nosuid`; user 65534; all capabilities dropped; `no-new-privileges` | `test_the_filesystem_is_read_only_and_tmp_cannot_execute` |
| Code exhausts the host | 64 processes, 256 MiB of memory and no swap, one CPU, 64 open files, and a deadline after which the container is killed | `test_runaway_code_ends_within_the_deadline` (fork bomb, 1 GiB, endless loop), `test_runaway_code_is_stopped_and_the_next_run_starts_fresh` |
| Code floods the model's context | 8 KiB of output kept; a run that writes 1 MiB is stopped | `test_output_is_capped` |
| Code forges a protocol message | The runner keeps the protocol pipe on private descriptors and points 0, 1 and 2 at `/dev/null`; the host checks every line's size, JSON and shape, and ends the run on anything else | `test_plain_output_cannot_forge_a_protocol_message`, `test_garbage_on_the_protocol_pipe_ends_the_cell_safely` |
| Code calls a tool it should not | Only allowlisted tools exist in the sandbox, and the bridge refuses any other name before the server sees it | `test_unknown_tools_are_refused_before_the_server_sees_them`, `test_the_allowlist_keeps_other_tools_out` |
| Code sends malformed or huge arguments | JSON Schema validation, unnamed arguments refused, a 16 KiB limit | `test_arguments_outside_the_schema_are_refused`, `test_oversized_arguments_are_refused` |
| Code loops on a tool, or a tool hangs | 200 calls per run, 30 seconds per call | `test_a_run_has_a_fixed_budget_of_calls`, `test_a_slow_tool_times_out_and_the_session_survives`, `test_refusals_reach_the_code_and_the_budget_holds` |
| A tool returns a huge result | Refused over 64 KiB | `test_oversized_results_are_refused_not_cut` |
| Host details leak through errors | Only the bridge's own refusal messages reach the code, capped at 500 characters; any other exception only by its type name | `test_a_host_fault_raises_in_the_cell_without_its_details` |

The tests that need Docker skip locally without it and fail under CI, where the image is built first.

### What it does not protect

- **The container shares the host kernel.** Docker's default runtime, runc, narrows the kernel surface with seccomp, no capabilities and no network, but a kernel exploit would cross it. gVisor (`--runtime=runsc` in `docker_argv`) is the upgrade path for code or data you do not trust at all; it is not wired in or tested here.
- **The wrapped server is not sandboxed.** It runs on the host as an ordinary process, with MCP's default environment plus any `--env` names. The bridge decides which tools and which arguments, not what a tool does with them: an allowlisted tool that deletes, deletes. Allow only what the task needs, and prefer read-only servers.
- **Valid arguments can still do harm.** A schema says a string, not which string.
- **Prompt injection is labelled, not filtered.** Tool descriptions (in the `run_python` description) and tool results (through what the code prints) come from the wrapped server and reach the model. They are marked as data. Nothing is redacted: bank-reconciliation-agent fenced and redacted payer text because it knew which fields were untrusted, and a generic wrapper does not.
- **The deadline is checked between messages.** A run can overrun it by up to one call timeout, and calls to the wrapped server are made one at a time.
- **Whoever runs `mcp-code-mode` can run `docker`,** which on Linux is equivalent to root. The process itself is trusted.
- **The base image is pinned by tag, not by digest,** and the resource limits are fixed in `sandbox.py`.
- **Side channels remain:** code can time things and read CPU and kernel details inside the container.

## Development

```bash
uv sync
uv run mcp-code-mode build-image
uv run ruff check . && uv run ruff format --check . && uv run pytest
```

`tests/toy_server.py` is a small MCP server for the tests: the bridge tests run it in process, and the end-to-end tests start it over stdio behind the CLI.

## License

MIT
