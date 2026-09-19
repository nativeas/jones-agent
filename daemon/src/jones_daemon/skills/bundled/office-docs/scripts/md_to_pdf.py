# /// script
# requires-python = ">=3.11"
# dependencies = ["python-docx>=1.1,<2", "reportlab>=4.0,<5"]
# ///
"""Markdown -> PDF. Three-tier fallback chain, in priority order (docs/design/
04-w5-interfaces.md §3: "PDF（优先系统可用路径：优先 pandoc/LibreOffice 若存在，
否则 reportlab 简版；报告写清选择）"):

1. **pandoc**, if on PATH — converts the Markdown directly. Best fidelity,
   but pandoc's default PDF writer needs a LaTeX engine (`pdflatex`/`xelatex`)
   installed too; a pandoc found without one fails at this step and this
   script falls through rather than reporting a false negative for "pandoc
   available".
2. **LibreOffice** (`soffice`/`libreoffice` on PATH), if pandoc isn't usable —
   LibreOffice has no native Markdown importer, so this script first builds
   an intermediate .docx (reusing `md_to_docx.py`'s own converter, same
   directory) and asks `soffice --headless --convert-to pdf` to render that.
3. **reportlab**, always available (declared as this script's own PEP 723
   dependency, unlike pandoc/LibreOffice which are external system tools this
   script only *detects*) — a plain, unstyled-but-real PDF built directly
   from the parsed Markdown blocks. This is the "简版" floor: every machine
   that can run this script at all can produce a PDF, even with neither
   external tool installed.

Usage:
    uv run md_to_pdf.py <input.md> <output.pdf>

Prints which tier it used to stdout — the PR report names which tier a bare
CI/dev machine (no pandoc, no LibreOffice) actually exercises.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from _shared_md import parse_blocks, plain_text


def _try_pandoc(src: Path, dst: Path) -> bool:
    pandoc = shutil.which("pandoc")
    if not pandoc:
        return False
    result = subprocess.run(
        [pandoc, str(src), "-o", str(dst)], capture_output=True, text=True
    )
    if result.returncode != 0 or not dst.is_file():
        print(f"pandoc found but failed ({result.returncode}): {result.stderr.strip()[:400]}",
              file=sys.stderr)
        return False
    return True


def _try_libreoffice(src: Path, dst: Path) -> bool:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return False
    from md_to_docx import build_docx

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        intermediate_docx = tmp_path / "intermediate.docx"
        build_docx(src.read_text(encoding="utf-8")).save(str(intermediate_docx))
        result = subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(tmp_path),
             str(intermediate_docx)],
            capture_output=True, text=True, timeout=120,
        )
        produced = tmp_path / "intermediate.pdf"
        if result.returncode != 0 or not produced.is_file():
            print(f"LibreOffice found but failed ({result.returncode}): "
                  f"{result.stderr.strip()[:400]}", file=sys.stderr)
            return False
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(produced, dst)
    return True


def _build_reportlab_pdf(markdown_text: str, dst: Path) -> None:
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import (
        ListFlowable,
        ListItem,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
    )

    styles = getSampleStyleSheet()
    heading_styles = [styles["Title"]] + [styles[f"Heading{n}"] for n in range(1, 5)]
    story = []
    for kind, payload in parse_blocks(markdown_text):
        if kind == "heading":
            level, text = payload
            style = heading_styles[min(level, len(heading_styles) - 1)]
            story.append(Paragraph(plain_text(text), style))
        elif kind == "paragraph":
            story.append(Paragraph(plain_text(payload), styles["BodyText"]))
        elif kind == "bullet_list":
            story.append(ListFlowable(
                [ListItem(Paragraph(plain_text(i), styles["BodyText"])) for i in payload],
                bulletType="bullet",
            ))
        elif kind == "ordered_list":
            story.append(ListFlowable(
                [ListItem(Paragraph(plain_text(i), styles["BodyText"])) for i in payload],
                bulletType="1",
            ))
        elif kind == "code":
            story.append(Paragraph(plain_text(payload).replace("\n", "<br/>"), styles["Code"]))
        elif kind == "blockquote":
            story.append(Paragraph(plain_text(payload), styles["Italic"]))
        elif kind == "table":
            story.append(Table([[plain_text(c) for c in row] for row in payload]))
        elif kind == "hr":
            story.append(Paragraph("_" * 40, styles["Normal"]))
        story.append(Spacer(1, 8))

    dst.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(str(dst), pagesize=LETTER).build(story or [Paragraph("", styles["Normal"])])


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {argv[0]} <input.md> <output.pdf>", file=sys.stderr)
        return 2
    src, dst = Path(argv[1]), Path(argv[2])

    if _try_pandoc(src, dst):
        print(f"wrote {dst} (via pandoc)")
        return 0
    if _try_libreoffice(src, dst):
        print(f"wrote {dst} (via LibreOffice)")
        return 0
    _build_reportlab_pdf(src.read_text(encoding="utf-8"), dst)
    print(f"wrote {dst} (via reportlab fallback)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
