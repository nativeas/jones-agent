"""Issue #21 (04-w5-interfaces.md §3, PRD 12.3 FR12): the two bundled Skills
(`office-docs`, `media-gen`) shipped under `skills/bundled/`.

Split by cost/what's being proven:
- `TestListing` — fast, no subprocess: `skills/service.py::list_skills()`
  (K's scanner, unchanged by this branch) sees both real directories and
  parses their frontmatter, same as it would any user/project Skill.
- `TestOfficeDocsScripts` — real acceptance proof for PRD 12.3's "办公文档
  Skill 产出四种格式各一份可打开": each script is invoked EXACTLY the way an
  Agent's `terminal` tool would (`uv run <script> <src> <dst>`, letting PEP
  723 resolve each script's own declared dependencies — no daemon pyproject
  dependency added for this, per 04-w5-interfaces.md §3), and the produced
  file is read back and verified — docx via a second `uv run` invocation of
  python-docx (kept out of the daemon's own test dependencies the same way
  the generator script is), pdf via its `%PDF` header + a real page marker,
  xlsx via a second `uv run` invocation of openpyxl. Needs `uv` + network on
  first run (PEP 723 dependency resolution) — no model/provider Key, so NOT
  behind `JONES_E2E` (see docs/design/04-w5-interfaces.md §3: only the media
  formats that need a Key are gated that way).
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from jones_daemon.skills import service

BUNDLED_DIR = service.BUNDLED_SKILLS_DIR
OFFICE_DOCS_SCRIPTS = BUNDLED_DIR / "office-docs" / "scripts"

SAMPLE_MD = """# Sample Report

A paragraph with **bold**, *italic*, and `code`.

## Section

- first item
- second item

1. step one
2. step two

> a blockquote

```
plain code block
```

## Numbers

| Name | Score |
| --- | --- |
| Alice | 90 |
| Bob | 85 |

---

Done.
"""


class TestListing:
    def test_both_bundled_skills_are_listed_valid_in_the_builtin_tier(self):
        result = service.list_skills(project_path=None)
        by_name = {e["name"]: e for e in result}
        for name in ("office-docs", "media-gen"):
            assert name in by_name, f"{name} missing from list_skills(); saw {sorted(by_name)}"
            entry = by_name[name]
            assert entry["tier"] == "builtin"
            assert entry["valid"] is True, entry["error"]
            assert entry["description"]  # frontmatter actually parsed, not just a dir-name fallback

    def test_office_docs_scripts_directory_exists_with_all_three_converters(self):
        for script in ("md_to_docx.py", "md_to_pdf.py", "md_to_xlsx.py"):
            assert (OFFICE_DOCS_SCRIPTS / script).is_file()

    def test_media_gen_skill_names_the_three_hermes_tools_and_their_key_env_vars(self):
        text = (BUNDLED_DIR / "media-gen" / "SKILL.md").read_text(encoding="utf-8")
        for tool_name in ("image_generate", "text_to_speech", "video_generate"):
            assert tool_name in text
        for env_var in ("FAL_KEY", "ELEVENLABS_API_KEY", "XAI_API_KEY"):
            assert env_var in text


def _uv_run(script: Path, *args: str, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["uv", "run", str(script), *args],
        capture_output=True, text=True, timeout=timeout,
    )


def _uv_run_inline(
    code: str, *, deps: list[str], tmp_path: Path, timeout: int = 180
) -> subprocess.CompletedProcess:
    """Run `code` (a small readback/assertion script) as a real PEP 723 `.py`
    file with the given inline `dependencies`, via `uv run <file>` — the same
    "PEP 723, not a daemon dependency" isolation `md_to_docx.py`/
    `md_to_xlsx.py` themselves use, kept on the VERIFICATION side too so this
    test file adds zero new imports to the daemon's own test environment.

    A real file, not `uv run -` (stdin): `uv run -` has no stable script
    identity for uv to key its resolved-environment cache on, so it re-
    resolves (and re-downloads every dependency, `lxml` included — ~8MiB,
    observed to blow past a 180s timeout on this branch's dev machine) on
    EVERY call even with identical `dependencies`, unlike a named `.py` file
    uv run has already resolved once before."""
    header = "# /// script\n# dependencies = " + json.dumps(deps) + "\n# ///\n"
    script = tmp_path / "_inline_readback.py"
    script.write_text(header + code, encoding="utf-8")
    return subprocess.run(
        ["uv", "run", str(script)], capture_output=True, text=True, timeout=timeout,
    )


@pytest.fixture
def sample_md(tmp_path) -> Path:
    p = tmp_path / "sample.md"
    p.write_text(SAMPLE_MD, encoding="utf-8")
    return p


class TestOfficeDocsScripts:
    """PRD 12.3 FR12 acceptance: each of the three converters produces a real
    file that a real, independent reader (not this script's own writer)
    opens back successfully."""

    def test_md_to_docx_produces_a_docx_python_docx_can_read_back(self, sample_md, tmp_path):
        out = tmp_path / "out.docx"
        result = _uv_run(OFFICE_DOCS_SCRIPTS / "md_to_docx.py", str(sample_md), str(out))
        assert result.returncode == 0, result.stderr
        assert out.is_file() and out.stat().st_size > 0

        readback = _uv_run_inline(
            f"""
import docx
d = docx.Document({str(out)!r})
texts = [p.text for p in d.paragraphs]
assert any("Sample Report" in t for t in texts), texts
assert any("bold" in t for t in texts), texts
assert len(d.tables) == 1, d.tables
assert d.tables[0].cell(0, 0).text == "Name"
assert d.tables[0].cell(1, 0).text == "Alice"
print("READBACK_OK")
""",
            deps=["python-docx>=1.1,<2"],
            tmp_path=tmp_path,
        )
        assert readback.returncode == 0, readback.stderr
        assert "READBACK_OK" in readback.stdout

    def test_md_to_xlsx_produces_an_xlsx_openpyxl_can_read_back(self, sample_md, tmp_path):
        out = tmp_path / "out.xlsx"
        result = _uv_run(OFFICE_DOCS_SCRIPTS / "md_to_xlsx.py", str(sample_md), str(out))
        assert result.returncode == 0, result.stderr
        assert out.is_file() and out.stat().st_size > 0

        readback = _uv_run_inline(
            f"""
import openpyxl
wb = openpyxl.load_workbook({str(out)!r})
assert "Numbers" in wb.sheetnames, wb.sheetnames
ws = wb["Numbers"]
rows = [tuple(r) for r in ws.iter_rows(values_only=True)]
assert rows[0] == ("Name", "Score"), rows
assert rows[1] == ("Alice", "90"), rows
print("READBACK_OK")
""",
            deps=["openpyxl>=3.1,<4"],
            tmp_path=tmp_path,
        )
        assert readback.returncode == 0, readback.stderr
        assert "READBACK_OK" in readback.stdout

    def test_md_to_pdf_produces_a_pdf_with_a_real_page(self, sample_md, tmp_path):
        out = tmp_path / "out.pdf"
        result = _uv_run(OFFICE_DOCS_SCRIPTS / "md_to_pdf.py", str(sample_md), str(out))
        assert result.returncode == 0, result.stderr
        assert out.is_file()
        raw = out.read_bytes()
        assert raw[:5] == b"%PDF-", raw[:16]
        # At least one real page object (not just a `/Pages` container) —
        # works for reportlab's own layout, LibreOffice's, and pandoc's.
        assert re.search(rb"/Type\s*/Page(?!s)", raw), "no /Type /Page object found in output"
        # Which tier actually ran on this machine — informational, not an
        # assertion: see md_to_pdf.py's own docstring for the fallback order.
        assert "wrote" in result.stdout

    def test_a_missing_source_file_fails_honestly_and_writes_nothing(self, tmp_path):
        """DEV.md 工程原则 #4: a real failure (bad source path) must exit
        non-zero and must not leave a claimed-but-empty/partial output file
        behind."""
        out = tmp_path / "out.docx"
        result = _uv_run(
            OFFICE_DOCS_SCRIPTS / "md_to_docx.py", str(tmp_path / "does-not-exist.md"), str(out)
        )
        assert result.returncode != 0
        assert not out.exists()
