# Design: finding tools, several servers and a private-data rule

Date: 8 November 2026, revised the same day after five reviews. Follows `concepts_brief.md`, whose decisions table this design implements. Target release: v0.2.0, only with the owner's yes.

Terms used below:

- **Init:** the first JSON line the host writes to the runner when a sandbox starts.
- **Description budget:** characters of the `run_python` description given to stubs. **Call budget:** tool calls per run.
- **Private tool:** a tool the operator marked `private`. **Taint:** the state that starts with the first admitted call to a private tool and lasts until the mcp-code-mode process exits.

## 1. Finding tools

### Names

`function_name` reserves `TOOLS` and `search_tools` the way it reserves `ToolError`: a wrapped tool with either name gets a trailing underscore. A v0.1.0 user whose server had a tool called `search_tools` sees it renamed; the release notes say so.

`load_tools` cuts each description at 2,048 characters (`MAX_DESCRIPTION`), since descriptions are untrusted and go into every description and search.

### The catalog

`Bridge.catalog()` returns, for each Python name, `{"stub": ..., "summary": ...}`, built once at startup. The summary is the name and its parameters, then the description up to its first sentence end (`". "` or a newline), the whole line cut at 120 characters: `add(a, b): Add two integers.`

`Sandbox.start(tools, catalog=None)` adds `"catalog"` to init when given. The runner reads `init.get("catalog", {})`, so `tests/test_sandbox.py` keeps working. `start` catches `OSError` from writing init (docker exited before reading it) and raises `SandboxError`. The image tag goes to `mcp-code-mode-sandbox:2`, because init changed.

### In the code

The runner adds two globals:

- `TOOLS`: a dict from Python name to stub. `print(TOOLS["add"])` describes a tool; an unknown name raises `KeyError`.
- `search_tools(query: str) -> list[str]`: the names whose name or stub contains a query word, best first. Words are lowercase runs of letters and digits (`get_weather` is `get` and `weather`). Each distinct query word scores 2 if it is one of the name's words and 1 if it is one of the stub's words; ties sort by name; names scoring 0 are left out.

Both are plain functions over data already in the sandbox: no message to the host, no call budget.

### The description

`render_tools(stubs, summaries, budget)` returns the tool part of the description:

1. Every stub, when they fit the budget.
2. Otherwise one summary line per tool, in name order, while they fit, then "and N more tools: search_tools(query) finds them and TOOLS[name] shows a stub."

`DESCRIPTION` always mentions `TOOLS` and `search_tools`. The budget is the constant `INLINE_BUDGET = 12_000`.

NOTE: word overlap, not BM25. BM25 over the same words is the upgrade if ranking turns out to matter.

## 2. The private-data rule

### Rule

- A private tool may see private data: its results may hold some, and its arguments may carry some to its server.
- Before the taint, every allowed tool can be called. From the first admitted private call on, only private tools can be called. Every other tool, labelled or not, is refused: its arguments would reach a server that may send them on.
- The taint lasts until the mcp-code-mode process exits. Over stdio that is the client's connection; restarting a wrapped server does not clear it.
- The refusal reaches the code as `ToolError`: "refused: {tool} is not private, and this session already called the private tool {first}; only private tools can be called until mcp-code-mode restarts". It stays under 500 characters.
- The taint is logged at info level, so the operator can see why later calls are refused.

### Labels

- `--private TOOL` (repeatable) with `serve -- COMMAND`; `"private": true` or a list of tool names per config entry.
- Labels name MCP tools, as `--allow` does, and are resolved in `load_tools` onto `Tool.private`. A label that names a tool the server does not list, or one that was left out (bad schema, name taken), stops startup with an error.
- Stubs of private tools carry the line "private: once called, only private tools can be called", so the model knows before it is refused.

### Where it is checked

`Flow` holds the taint (the first private tool's name, or `None`) behind a `threading.Lock`: two `run_python` calls can run at once. `Bridge._call` calls `flow.admit(tool)` after the allowlist, size and schema checks and right before `_send`. In one step under the lock, `admit` refuses a non-private tool once tainted, and otherwise sets the taint if the tool is private. A call that fails after `admit` (an error from the server, a timeout) still taints, since its error text can carry data.

`admit` is in `Bridge`, after the schema check, on purpose: moving it earlier would taint on calls the schema refused, and nothing came back from those.

### Limits (for the README's "What it does not protect")

- Labels are the operator's claim. A tool marked private that sends data out defeats the rule.
- Private data also arrives without a call: private servers' tool descriptions and schemas (enums, examples) are in the description and the catalog, and nothing taints on them.
- What a run prints (8 KiB) reaches the model, which may hold other tools in its client. The rule assumes `run_python` is the model's only outlet.
- One private call, even one an injected description talked the model into, blocks every non-private tool until restart. That is availability lost on purpose.
- It covers confidentiality only: a result from an untrusted tool can still steer a later write.
- Upgrade path, not a limit: ask the user through MCP elicitation instead of refusing, where the client supports it.

## 3. Several servers

### Config

```json
{
  "mcpServers": {
    "notes":   {"command": "uv", "args": ["run", "notes-server"], "allow": ["search", "read"], "private": true},
    "tracker": {"command": "tracker-mcp", "env": {"TRACKER_TOKEN": "..."}, "allow": ["create_issue"], "private": ["create_issue"]}
  }
}
```

`load_config(path) -> dict[str, Entry]`, with `Entry(params: StdioServerParameters, allow: list[str] | None, private: list[str] | bool)`:

- Each entry needs `command`; `args` (list of strings) and `env` (object of strings) are optional. `env` values are passed as written: `${VAR}` is not expanded.
- `type` is ignored when it is `"stdio"`. `url`, `headers`, any other `type` and any other key stop startup with an error naming the entry and the key.
- `--config` cannot be combined with `--allow`, `--env`, `--private` or a command after `--`.

### Names and calls

- Tool names are `function_name(f"{server}__{tool}")`: `notes__search`. Server names need no rule of their own. Two tools from different servers that end with one name stop startup.
- `Bridge(server, allow=None, private=(), *, prefix=None, flow=None, ...)`: `prefix` sets the name prefix, `flow` is shared.
- `Servers(entries)` opens one `Bridge` per entry under an `ExitStack`, all sharing one `Flow`. If one fails to start, the ones already open are closed and the error names the entry.
- `Servers` offers `tools`, `stubs()`, `catalog()`, `call(name, params)` (a lookup from name to bridge) and `budget(max_calls)`: one counter per run across every server, built on a shared `count_calls(call, max_calls)` helper that `Bridge.budget` uses too. A refused call still uses budget, as today.
- `server.py` always works on a `Servers`: `serve -- COMMAND` builds a one-entry `Servers` with no prefix, so v0.1.0 names do not change. `run_code` and `build_server` take a `Servers` or a `Bridge`, which offer the same methods.

## 4. Tests

No Docker unless marked. The bridge tests run toy servers in process; `tests/toy_notes.py` is a second toy server with its own `RAN` list, as a private source.

| Behaviour | Test |
|---|---|
| Reserved names get an underscore | extend the `function_name` cases with `TOOLS` and `search_tools` |
| Long descriptions are cut | `test_long_descriptions_are_cut` |
| Full stubs fit, else summaries then "and N more", at the exact boundary | `test_the_description_falls_back_to_summaries` |
| `TOOLS` and `search_tools` work, rank and use no budget (Docker) | `test_discovery_never_reaches_the_server` |
| The tools list in the sandbox includes the two globals (Docker) | update `test_code_calls_the_tools_through_the_bridge` |
| A non-private call after a private one is refused, with its message | `test_only_private_tools_after_a_private_call` |
| Before the taint everything allowed works | same test, first half |
| A private call refused by its schema does not taint | `test_a_refused_private_call_does_not_taint` |
| A private call that fails after admit still taints | `test_a_failed_private_call_still_taints` |
| The taint outlives the run | `test_the_taint_lasts_for_the_process` |
| A non-private call racing a private one is either admitted first or refused | `test_admit_is_atomic` (threads) |
| A label naming an unknown or left-out tool stops startup | `test_labels_must_name_kept_tools` |
| Private stubs say so | `test_private_stubs_say_so` |
| Config errors name the entry and key | `test_config_errors_name_the_entry` (parametrised: no command, url, headers, unknown key, bad args) |
| `type: "stdio"` is accepted | same file, `test_config_accepts_a_stdio_type` |
| Prefixed names, one shared budget, per-server allow | `test_two_servers_share_one_budget` |
| A private call on one server refuses a tool on another | `test_private_on_one_server_refuses_another` |
| A name clash across servers stops startup | `test_a_name_clash_across_servers_stops_startup` |
| A server that fails to start closes the open ones | `test_a_failing_entry_closes_the_others` |
| `serve -- COMMAND` keeps flat names | `test_flat_names_without_a_config` |
| `--config` excludes the single-server options | `test_config_excludes_single_server_options` |
| Config `env` reaches the wrapped server, not the sandbox (Docker) | `test_config_env_stays_on_the_host` |
| `serve --config` end to end with two stdio servers (Docker) | `test_config_serves_two_servers` |

Quality gates on the changed code: `crapper`, `dryer` and `mutator`.

## 5. Docs

- README: "Finding tools", "Several servers" and "Private data" sections; threat model rows with their tests; the limits in section 2.
- `SKILL.md`: `TOOLS` and `search_tools`; prefixed names; a private-data refusal is final, so tell the user instead of looking for another route. `test_skill.py` keeps its numbers in line.

## 6. Order of work

1. Names, description cut, catalog, `TOOLS`, `search_tools`, tiers, image tag, with tests.
2. `Flow`, labels and `--private` with one server, with tests.
3. `Servers`, config and `--config`, with tests.
4. README, skill, quality gates; then the owner decides on v0.2.0.
