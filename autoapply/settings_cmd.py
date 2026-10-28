"""`autoapply settings`: every setting with the value in use, defaults included.

profile.yaml holds only what setup wrote and what you changed; everything else
takes its built-in value. This prints the whole picture as YAML you can copy
from, marking each value that comes from a default. No secrets live in
profile.yaml (they are in secrets.env), so nothing here is sensitive.
"""
from __future__ import annotations

import yaml

from .config import Paths, load_profile, read_yaml

COMMENT_AT = 44


def _scalar(v) -> str:
    text = yaml.safe_dump(v, default_flow_style=True, allow_unicode=True, width=10_000).strip()
    return text[:-4].strip() if text.endswith("\n...") else text.removesuffix("...").strip()


def _mark(line: str, mark: str) -> str:
    return line + " " * max(1, COMMENT_AT - len(line)) + mark if mark else line


def render(effective: dict, given: dict | None, indent: int = 0) -> list[str]:
    """YAML lines for `effective`, each key absent from `given` marked "# default"."""
    lines = []
    pad = "  " * indent
    for key, value in effective.items():
        mine = isinstance(given, dict) and key in given
        mark = "" if mine else "# default"
        if isinstance(value, dict) and value:
            lines.append(_mark(f"{pad}{key}:", mark))
            lines += render(value, given.get(key) if mine else None, indent + 1)
        elif isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            lines.append(_mark(f"{pad}{key}:", mark))        # sources: one entry per block
            block = yaml.safe_dump(value, sort_keys=False, allow_unicode=True, width=10_000)
            lines += [f"{pad}  {line}" for line in block.splitlines()]
        else:
            lines.append(_mark(f"{pad}{key}: {_scalar(value)}", mark))
    return [line.rstrip() for line in lines]


def settings_text(paths: Paths) -> str:
    profile = load_profile(paths)
    given = read_yaml(paths.profile) or {}
    head = [f"# Settings in use, from {paths.profile}",
            "# '# default' = not in your profile.yaml: the built-in value. To change one,",
            "# copy its line (with the section line above it) into profile.yaml.", ""]
    return "\n".join(head + render(profile.model_dump(mode="json"), given))
