"""Tiny, dependency-free Markdown block parser shared by this skill's three
converters (`md_to_docx.py` / `md_to_pdf.py` / `md_to_xlsx.py`).

Not a general CommonMark implementation — Issue #21 only needs enough of
Markdown to round-trip an agent-authored document into Docx/PDF/xlsx:
headings (`#`..`######`), paragraphs, `-`/`*`/`+` bullet lists, `1.` ordered
lists, fenced code blocks (```), blockquotes (`>`), pipe tables, and `---`
horizontal rules. No inline HTML, no nested lists, no footnotes. A construct
outside this set is treated as a plain paragraph rather than raising — this
skill's job is "produce an openable file", not "reject unusual input" (DEV.md
工程原则 #4 is about failures the caller must know about, e.g. a missing
output directory; a stray HTML paragraph the agent's own Markdown ends up
with is not that kind of failure).

Run standalone (`uv run md_to_docx.py ...`) — a script's own directory is on
`sys.path`, so the three converters `import _shared_md` directly without a
package install; this module itself has no third-party dependency, so it
never needs a PEP 723 header of its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

Block = tuple  # (kind, payload) — see parse_blocks() for the shapes


@dataclass
class InlineRun:
    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False


_INLINE_TOKEN = re.compile(r"(\*\*.+?\*\*|\*.+?\*|`.+?`)")


def parse_inline(text: str) -> list[InlineRun]:
    """Split *text* on `**bold**` / `*italic*` / `` `code` `` — no nesting."""
    runs: list[InlineRun] = []
    for chunk in _INLINE_TOKEN.split(text):
        if not chunk:
            continue
        if chunk.startswith("**") and chunk.endswith("**") and len(chunk) >= 4:
            runs.append(InlineRun(chunk[2:-2], bold=True))
        elif chunk.startswith("`") and chunk.endswith("`") and len(chunk) >= 2:
            runs.append(InlineRun(chunk[1:-1], code=True))
        elif chunk.startswith("*") and chunk.endswith("*") and len(chunk) >= 2:
            runs.append(InlineRun(chunk[1:-1], italic=True))
        else:
            runs.append(InlineRun(chunk))
    return runs


def plain_text(text: str) -> str:
    """Inline text with the `**`/`*`/`` ` `` markers stripped, no styling kept
    — for callers (xlsx cells) that just want the readable string."""
    return "".join(r.text for r in parse_inline(text))


_TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
_TABLE_SEP_CELL = re.compile(r"^:?-{1,}:?$")


def _split_table_row(line: str) -> list[str]:
    inner = _TABLE_ROW.match(line).group(1)
    return [cell.strip() for cell in inner.split("|")]


def _is_table_separator(line: str) -> bool:
    m = _TABLE_ROW.match(line)
    if not m:
        return False
    cells = _split_table_row(line)
    return bool(cells) and all(_TABLE_SEP_CELL.match(c) for c in cells)


def parse_blocks(text: str) -> list[Block]:
    """Top-level block sequence. Each block is `(kind, payload)`:
    - `("heading", (level, text))`
    - `("paragraph", text)`
    - `("bullet_list", [text, ...])`
    - `("ordered_list", [text, ...])`
    - `("code", text)`  (fence language, if any, discarded — no highlighting
      in either output format)
    - `("blockquote", text)`
    - `("table", [[cell, ...], ...])`  (first row is the header row)
    - `("hr", None)`
    """
    lines = text.splitlines()
    blocks: list[Block] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        if stripped.startswith("```"):
            i += 1
            body: list[str] = []
            while i < n and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1  # consume closing fence (or EOF — unterminated fence is still honest content)
            blocks.append(("code", "\n".join(body)))
            continue

        heading_m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading_m:
            level = len(heading_m.group(1))
            blocks.append(("heading", (level, heading_m.group(2).strip())))
            i += 1
            continue

        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            blocks.append(("hr", None))
            i += 1
            continue

        if _TABLE_ROW.match(line) and i + 1 < n and _is_table_separator(lines[i + 1]):
            rows = [_split_table_row(line)]
            i += 2
            while i < n and _TABLE_ROW.match(lines[i]):
                rows.append(_split_table_row(lines[i]))
                i += 1
            blocks.append(("table", rows))
            continue

        if stripped.startswith(">"):
            quote: list[str] = []
            while i < n and lines[i].strip().startswith(">"):
                quote.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            blocks.append(("blockquote", "\n".join(quote)))
            continue

        bullet_m = re.match(r"^[-*+]\s+(.*)$", stripped)
        if bullet_m:
            items = [bullet_m.group(1)]
            i += 1
            while i < n:
                m2 = re.match(r"^[-*+]\s+(.*)$", lines[i].strip())
                if not m2 or not lines[i].strip():
                    break
                items.append(m2.group(1))
                i += 1
            blocks.append(("bullet_list", items))
            continue

        ordered_m = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if ordered_m:
            items = [ordered_m.group(1)]
            i += 1
            while i < n:
                m2 = re.match(r"^\d+[.)]\s+(.*)$", lines[i].strip())
                if not m2 or not lines[i].strip():
                    break
                items.append(m2.group(1))
                i += 1
            blocks.append(("ordered_list", items))
            continue

        # Paragraph: consume until a blank line or the start of another block kind.
        para_lines = [stripped]
        i += 1
        while i < n and lines[i].strip() and not re.match(
            r"^(#{1,6}\s|[-*+]\s|\d+[.)]\s|>|```|\|)", lines[i].strip()
        ):
            para_lines.append(lines[i].strip())
            i += 1
        blocks.append(("paragraph", " ".join(para_lines)))

    return blocks
