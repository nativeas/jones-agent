---
name: office-docs
description: "Turn Markdown into Docx, PDF, or an xlsx spreadsheet using bundled scripts (python-docx/openpyxl/reportlab, plus pandoc or LibreOffice if present)."
version: 0.1.0
author: Jones
license: MIT
---

# Office Documents

Convert an agent-authored Markdown document into a real office file the user can open — Word (`.docx`), PDF, or a spreadsheet (`.xlsx`) — without writing any rendering code yourself. Three standalone scripts ship with this skill under `scripts/`; run them with the `terminal` tool via `uv run`, which resolves each script's own inline dependencies (PEP 723) on first use, no separate install step.

## When to use

- The user asks for a Word document, a PDF, a report, a spreadsheet, or "export this as a file" for something you'd otherwise just print as chat text.
- You already have (or can write) the content as Markdown — headings, paragraphs, bullet/numbered lists, a fenced code block, a blockquote, or a pipe table (`| a | b |`).

Don't use for: editing an *existing* Docx/PDF/xlsx the user gives you (these scripts only go Markdown → office format, one direction) — read the file with the appropriate tool instead and describe or summarize it directly.

## Procedure

1. **Write the Markdown first**, as its own `.md` file in the current Project directory (the `write_file` tool) — not inline in a shell string. This also gives the user a plain-text copy of the content, which some of them will prefer anyway.
2. **Pick the target format(s)** from what the user asked for. All three scripts take the same two positional arguments: the source `.md` path, then the destination path. Run them with `terminal`:
   - Word: `uv run <this skill's dir>/scripts/md_to_docx.py report.md report.docx`
   - PDF: `uv run <this skill's dir>/scripts/md_to_pdf.py report.md report.pdf`
   - Spreadsheet: `uv run <this skill's dir>/scripts/md_to_xlsx.py report.md report.xlsx`

   (`<this skill's dir>` is wherever this `SKILL.md` sits — resolve it the same way you'd resolve any other bundled-skill asset path, e.g. relative to `skill_view`'s reported location.)
3. **Write output into the current Project's directory**, not a temp path — pass an absolute destination path under the Project so the user's file browser / Finder actually shows it. Both scripts create missing parent directories for you.
4. **Report the real path** back to the user once the script prints `wrote <path>` — don't claim success without that line; a non-zero exit or a missing `wrote` line means the conversion failed and you should show the script's stderr, not a generic "done".

## What each script does

- **`md_to_docx.py`** — python-docx. Headings → Word heading styles, bullet/numbered lists → Word list styles, a pipe table → a real Word table (`Light Grid Accent 1`), `**bold**`/`*italic*`/`` `code` `` → run-level formatting, a fenced code block → monospace paragraph.
- **`md_to_xlsx.py`** — openpyxl. Every pipe table in the document becomes its own worksheet (named after the heading immediately above it, or `Table N`). A document with **no** table still produces an openable workbook — one `Document` sheet with the headings/paragraphs/list items as plain rows — this script never refuses non-tabular input.
- **`md_to_pdf.py`** — three-tier fallback, tries each in order and prints which one it used:
  1. **pandoc**, if on `PATH` (best fidelity — but pandoc's own PDF writer needs a LaTeX engine installed too; if that's missing this script detects the failure and falls through rather than reporting a false success).
  2. **LibreOffice** (`soffice`/`libreoffice` on `PATH`) — builds an intermediate `.docx` via the same converter `md_to_docx.py` uses, then `soffice --headless --convert-to pdf` renders it.
  3. **reportlab**, always available (it's this script's own declared dependency, not something merely detected on the system) — a plain but real PDF built straight from the parsed Markdown. Every machine that can run `uv` at all can reach this tier, even with neither pandoc nor LibreOffice installed.

## Format coverage

Headings (`#`…`######`), paragraphs, `-`/`*`/`+` bullet lists, `1.` ordered lists, fenced code blocks, `>` blockquotes, `---` horizontal rules, pipe tables, and inline `**bold**`/`*italic*`/`` `code` ``. Not a full CommonMark implementation (no nested lists, no inline HTML, no footnotes/images) — anything outside this set renders as a plain paragraph rather than erroring, since the point is a document the user can open, not a spec-perfect parser.

## Failure

A script exits non-zero and writes nothing on a real failure (bad source path, unwritable destination). That is the honest outcome — report it to the user with the actual stderr text, don't retry silently or fabricate a path that wasn't written.
