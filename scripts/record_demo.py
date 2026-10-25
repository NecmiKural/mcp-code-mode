"""Render a short demo to docs/demo.svg.

Run with Docker up and the image built: `uv run --with rich python scripts/record_demo.py`. No
model and no key: the program below stands in for code a model would send to run_python. It
calls the two allowlisted toy tools, then builds a call to `wipe` itself, through the runner's
own stub factory, since the sandbox defines no function for a tool off the allowlist. The bridge
refuses that call before the toy server sees it.
"""

import io
import re
import sys
from pathlib import Path

from rich._export_format import CONSOLE_SVG_FORMAT
from rich.console import Console
from rich.syntax import Syntax
from rich.text import Text

from mcp_code_mode import Bridge, run_code

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import toy_server  # noqa: E402

# Rich's template fetches Fira Code from cdnjs; without it the SVG makes no outside request.
SVG_FORMAT = re.sub(r"\s*@font-face \{\{.*?\}\}", "", CONSOLE_SVG_FORMAT, flags=re.S)
ALLOW = ["add", "echo"]
CODE = """\
print("add  ->", await add({"a": 2, "b": 3}))
print("echo ->", await echo({"text": "hello from the sandbox"}))

# wipe is not on the allowlist, so there is no function for it here; make one anyway
wipe = add.__globals__["stub"]("wipe")
try:
    await wipe({})
except ToolError as exc:
    print("wipe ->", exc)
"""


def main() -> None:
    toy_server.RAN.clear()
    with Bridge(toy_server.server, allow=ALLOW) as bridge:
        cell = run_code(bridge, CODE)
    assert cell.error is None, cell.error

    console = Console(record=True, width=100, file=io.StringIO())
    console.print(Text("$ uv run --with rich python scripts/record_demo.py", style="bold"))
    console.print(Text(f"  the toy MCP server from tests/, allowlist: {', '.join(ALLOW)}", "dim"))
    console.print(Text("\nrun_python, code as a model would send it:", style="bold"))
    console.print(Syntax(CODE.rstrip(), "python", theme="ansi_dark", background_color="default"))
    console.print(Text("\nwhat the code printed:", style="bold"))
    for line in cell.stdout.splitlines():
        console.print(Text(line, style="bold red" if line.startswith("wipe") else "green"))
    console.print(Text(f"\ntools the toy server ran: {', '.join(toy_server.RAN)}", style="bold"))

    path = ROOT / "docs" / "demo.svg"
    path.parent.mkdir(exist_ok=True)
    console.save_svg(str(path), title="mcp-code-mode: the bridge", code_format=SVG_FORMAT)
    assert not re.search(r"url\((?!#)", path.read_text()), "the SVG must load nothing from outside"
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
