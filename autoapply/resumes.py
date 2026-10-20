"""Resume PDFs: text extraction (cached next to the PDF) and per-role selection."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .config import ConfigError, Paths, Profile


def extract_text(pdf: Path) -> str:
    """Text of a resume PDF, cached as <name>.txt and refreshed when the PDF changes."""
    cache = pdf.with_suffix(".txt")
    if cache.exists() and cache.stat().st_mtime >= pdf.stat().st_mtime:
        return cache.read_text(encoding="utf-8")
    from pypdf import PdfReader
    reader = PdfReader(str(pdf))
    text = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    if not text:
        raise ConfigError(f"{pdf.name}: no extractable text (scanned PDF?). Export a text PDF.")
    cache.write_text(text, encoding="utf-8")
    return text


class Resumes:
    def __init__(self, paths: Paths, profile: Profile):
        self.paths = paths
        self.cfg = profile.resumes
        self._text: dict[str, str] = {}

    def file_for(self, role: str | None) -> str:
        return self.cfg.for_role(role)

    def path(self, name: str) -> Path:
        return self.paths.resumes / name

    def text(self, name: str) -> str:
        if name not in self._text:
            p = self.path(name)
            if not p.exists():
                raise ConfigError(f"resume {name} not found in {self.paths.resumes}")
            self._text[name] = extract_text(p) if p.suffix.lower() == ".pdf" else p.read_text(encoding="utf-8")
        return self._text[name]

    def hash(self, name: str) -> str:
        return hashlib.sha256(self.text(name).encode()).hexdigest()[:16]

    def all_files(self) -> list[str]:
        return sorted({self.cfg.default, *self.cfg.map.values()})

    def check(self) -> list[str]:
        problems = []
        for f in self.all_files():
            try:
                self.text(f)
            except ConfigError as e:
                problems.append(str(e))
        return problems
