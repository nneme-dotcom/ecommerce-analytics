"""Guards the project memory that every agent relies on."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MEMORY_FILES = [
    "CLAUDE.md",
    "docs/PLAN.md",
    "docs/DATA_CONTRACT.md",
    "docs/DECISIONS.md",
    "docs/PROGRESS.md",
]
AGENT_ROLES = ["data-engineer", "analytics-engineer", "backend", "frontend", "devops", "reviewer"]


def test_memory_files_exist_and_are_not_empty():
    for rel in MEMORY_FILES:
        path = ROOT / rel
        assert path.is_file(), f"missing {rel}"
        assert path.read_text(encoding="utf-8").strip(), f"{rel} is empty"


def test_claude_md_points_to_every_memory_file():
    claude_md = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    for rel in MEMORY_FILES[1:]:
        assert rel in claude_md, f"CLAUDE.md does not reference {rel}"


def test_every_agent_role_is_defined_with_frontmatter():
    for role in AGENT_ROLES:
        path = ROOT / ".claude" / "agents" / f"{role}.md"
        assert path.is_file(), f"missing agent definition: {role}"
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---\n"), f"{role}: missing frontmatter"
        assert f"name: {role}\n" in text, f"{role}: name does not match file name"
        assert "description:" in text, f"{role}: missing description"


def test_reviewer_cannot_edit_files():
    text = (ROOT / ".claude" / "agents" / "reviewer.md").read_text(encoding="utf-8")
    tools_line = next(line for line in text.splitlines() if line.startswith("tools:"))
    assert "Write" not in tools_line and "Edit" not in tools_line
