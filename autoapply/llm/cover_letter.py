"""Cover letters, written only when the form requires one (§8.5.3)."""
from __future__ import annotations

from pathlib import Path

from ..config import Profile
from . import prompts
from .client import LLM, cached_system

PROMPT_VERSION = "cover-v1"


def write_cover_letter(llm: LLM, profile: Profile, resume_text: str, posting: dict, description: str) -> str:
    system = cached_system(prompts.load("cover_letter", resume=resume_text))
    user = (f"Company: {posting.get('company')}\nRole: {posting.get('title')}\n\n"
            f"<job_description>\n{description[:30000]}\n</job_description>")
    res = llm.call("cover_letter", profile.llm.write_model, system, user,
                   posting_key=posting.get("posting_key"), max_tokens=4000, effort="medium")
    return res.text.strip()


def render_pdf(text: str, out: Path) -> Path:
    """Plain, readable PDF of the letter (for file-upload cover letter fields)."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    from xml.sax.saxutils import escape

    out.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(out), pagesize=LETTER, leftMargin=72, rightMargin=72,
                            topMargin=72, bottomMargin=72)
    style = getSampleStyleSheet()["BodyText"]
    style.fontSize, style.leading = 11, 15
    story = []
    for para in [p.strip() for p in text.split("\n\n") if p.strip()]:
        story += [Paragraph(escape(para).replace("\n", "<br/>"), style), Spacer(1, 10)]
    doc.build(story)
    return out
