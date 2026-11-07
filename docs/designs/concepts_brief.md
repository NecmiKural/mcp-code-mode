# Concepts brief: finding tools, several servers and a private-data rule

Date: 8 November 2026. Status: agreed direction, revised after five reviews (simplicity, security design, implementation readiness, comprehension, structure). The detailed design is `design_draft.md`.

## Why

Two limits of v0.1.0 show up as soon as the wrapped server is real:

1. **Every stub goes into the `run_python` description.** A server with 80 tools makes a description of tens of thousands of characters that the model reads on every turn. pi's codemode and goose's Code Mode answer this with tool discovery from inside the code.
2. **One server per `run_python`.** Real tasks join servers: read a database, file an issue in the team's tracker. Today that needs two code-mode servers and the model in between.

Joining servers opens a path one server alone does not have: code reads private data from one server and hands it to another, whose server then holds it or sends it on. So several servers come with a rule for private data.

## Goals

- **Finding tools.** The description carries every stub while they fit the description budget; past it, one summary line per tool until the budget runs out, then "and N more". Inside the code, `TOOLS` maps each tool name to its stub and `search_tools(query)` lists the names that match. Both answer from the tool list fetched at startup: they never reach a wrapped server and use none of the run's call budget.
- **Several servers.** `serve --config FILE` reads the `mcpServers` shape MCP clients already use (stdio entries), plus `allow` and `private` per server. Tool names get the server's name as a prefix: `notes__search`.
- **A private-data rule.** The operator marks the tools that may see private data: their results may be private, and they may receive private data in their arguments. From the first call to a private tool on, only private tools can be called, until the mcp-code-mode process exits. Every other tool is a possible outlet, because its arguments reach its server. The rule works with one server too.
- Every new check comes with a test, as the threat model table requires.

## Non-goals

- No saved snippets or macros: code that outlives a run would end the "every run starts fresh" guarantee and could carry an injected instruction forward.
- No data-flow tracking inside the code. What one run printed is in the model's context, and the next run can be written from it, so the rule is coarse and lasts for the process.
- No integrity rule (a result from an untrusted tool steering a later write).
- No asking the user to approve a refused call. MCP elicitation is the upgrade path.
- No remote (`url`) servers in config yet; v0.1.0 wraps only stdio servers too.
- No measurements. The bank-reconciliation-agent experiment holds the numbers.

## Decisions

| Question | Choice | Why |
|---|---|---|
| Where search runs | In the sandbox, over a catalog sent with the runner's first message | No new protocol message, no server call, no budget to special-case |
| Discovery surface | `TOOLS` (a dict) and `search_tools(query)` | The model writes Python: a dict covers "describe" and list comprehensions cover filters |
| Search | Distinct query words, 2 points for a word in the name, 1 in the stub | Enough to find a tool by a word; stdlib only; no ranking engine to maintain |
| Description budget | 12,000 characters, a constant | The package counts characters elsewhere; about 3,000 tokens, pi's default |
| Description tiers | Full stubs, else summaries until the budget, then "and N more" | No cliff where one character over hides every tool |
| Tool names with several servers | `server__tool`, flat | Stays one naming path; no class stubs; no clash with `import time` |
| Labels | One label, `private`, per tool or `true` for a whole server | Fail-closed: an unlabelled tool counts as an outlet |
| Taint | Starts when a private call is admitted (it passed every check and is about to be sent) | Data can only come back from a call that goes out; a call that then fails still taints |
| Taint lifetime | The mcp-code-mode process, in practice the client's connection | A run's output is in the model's context, so a later run is no cleaner |
| Call budget with several servers | One budget per run across all servers | The limit is on what one program does |
| Config | `mcpServers` with stdio entries, plus `allow` and `private`; `type: "stdio"` ignored; `url`, `headers` and other keys refused by name | People can copy what they have, and nothing is half supported |
| Descriptions from servers | Cut at 2 KiB each | They are untrusted and go into every description and search |
