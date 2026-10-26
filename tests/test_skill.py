"""The agent skill states the package's real defaults and is a valid skill file."""

from pathlib import Path

from mcp_code_mode.bridge import CALL_TIMEOUT_S, MAX_ARG_CHARS, MAX_CALLS, MAX_RESULT_CHARS
from mcp_code_mode.sandbox import DEADLINE_S, KEEP, MAX_ERROR

SKILL = Path(__file__).resolve().parents[1] / "skills" / "mcp-code-mode" / "SKILL.md"


def test_the_skill_states_the_defaults_in_the_code():
    text = SKILL.read_text()
    for stated in (
        f"{MAX_CALLS} by default",
        f"{DEADLINE_S:g} seconds by default",
        f"over {CALL_TIMEOUT_S:g} seconds",
        f"over {MAX_ARG_CHARS // 1024} KiB",
        f"over {MAX_RESULT_CHARS // 1024} KiB",
        f"{KEEP // 1024} KiB ({KEEP} characters)",
        f"at most {MAX_ERROR} characters",
    ):
        assert stated in text, stated


def test_the_skill_has_a_name_matching_its_folder_and_a_short_description():
    head, _, _ = SKILL.read_text().removeprefix("---\n").partition("\n---\n")
    fields = dict(line.split(": ", 1) for line in head.splitlines())
    assert fields["name"] == SKILL.parent.name
    assert 0 < len(fields["description"]) <= 1024
