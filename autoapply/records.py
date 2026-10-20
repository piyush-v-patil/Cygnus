"""Saved copy of every submitted application (§12.1)."""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from .normalize import slug


def submission_dir(root: Path, posting: dict) -> Path:
    short = slug(posting["posting_key"].split(":")[-1], 12)
    name = (f"{datetime.now():%Y-%m-%d}_{slug(posting.get('company') or 'company', 30)}_"
            f"{slug(posting.get('title') or 'role', 40)}_{short}")
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_record(d: Path, posting: dict, fields_sent: list[dict], resume: Path | None,
                 generated: list[str], description: str, score: dict | None, extra: dict | None = None) -> None:
    (d / "fields.json").write_text(json.dumps(fields_sent, indent=1, ensure_ascii=False), encoding="utf-8")
    if resume and resume.exists():
        shutil.copy2(resume, d / resume.name)
    for g in generated or []:
        p = Path(g)
        if p.exists():
            shutil.copy2(p, d / p.name)
    (d / "job.txt").write_text(description or "", encoding="utf-8")
    meta = {
        "url": posting.get("apply_url"), "company": posting.get("company"), "title": posting.get("title"),
        "posting_key": posting.get("posting_key"), "location": posting.get("location"),
        "posted_at": posting.get("posted_at"), "saved_at": datetime.now().isoformat(timespec="seconds"),
        "fit_score": (score or {}).get("score"), "fit_reasons": (score or {}).get("reasons"),
    }
    meta.update(extra or {})
    (d / "meta.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
