"""Workflow templates: discovery + copy + INDEX generation.

Templates live under ``automaton/templates/<category>/<name>.yaml``
(installed into the package via ``[tool.setuptools.package-data]``).
Each template starts with a comment block:

    # First line: short title.
    # Description: paragraph explaining what it does.
    # Required secrets: GITHUB_TOKEN, ...
    # Required env: tools that need to be on PATH
    # Cron: suggested cron expression (or "trigger manually")
    # Last verified: YYYY-MM-DD

This module parses that block (purely from the comments - we don't run
the YAML through Jinja or anything) so the INDEX, the ``automaton init``
output, and tests stay in sync without a separate metadata file.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

TEMPLATES_ROOT = Path(__file__).parent.parent / "templates"


@dataclass(frozen=True)
class TemplateMeta:
    category: str
    name: str           # filename without .yaml
    path: Path
    title: str          # first non-shebang comment line, stripped
    description: str    # everything after "Description:" until the next "Required" line
    requires_secrets: str
    requires_env: str
    cron: str
    last_verified: str

    @property
    def slug(self) -> str:
        """Stable identifier: '<category>/<name>'."""
        return f"{self.category}/{self.name}"


_HEADER_RE = re.compile(r"^#\s?(.*)$")


def _parse_header(text: str) -> Dict[str, str]:
    """Pull the leading comment block out of a template file."""
    lines = []
    for raw in text.splitlines():
        m = _HEADER_RE.match(raw)
        if not m:
            break
        lines.append(m.group(1))

    out: Dict[str, str] = {
        "title": "", "description": "",
        "requires_secrets": "", "requires_env": "",
        "cron": "", "last_verified": "",
    }
    if not lines:
        return out
    # First non-empty line is the title.
    for i, l in enumerate(lines):
        if l.strip():
            out["title"] = l.strip()
            break

    current = None
    for l in lines:
        s = l.strip()
        for key in ("Description", "Required secrets", "Required env",
                    "Cron", "Last verified"):
            prefix = key + ":"
            if s.startswith(prefix):
                current = key
                val = s[len(prefix):].strip()
                slot = {"Description": "description",
                        "Required secrets": "requires_secrets",
                        "Required env": "requires_env",
                        "Cron": "cron",
                        "Last verified": "last_verified"}[key]
                out[slot] = val
                break
        else:
            # Continuation of the previous field, if any.
            if current == "Description" and s:
                if out["description"]:
                    out["description"] += " " + s
                else:
                    out["description"] = s
    return out


def discover() -> List[TemplateMeta]:
    """Return every template found under TEMPLATES_ROOT, sorted by slug."""
    if not TEMPLATES_ROOT.exists():
        return []
    metas = []
    for path in sorted(TEMPLATES_ROOT.glob("*/*.yaml")):
        category = path.parent.name
        name = path.stem
        header = _parse_header(path.read_text(encoding="utf-8"))
        metas.append(TemplateMeta(
            category=category,
            name=name,
            path=path,
            **header,
        ))
    return metas


def by_slug(slug: str) -> TemplateMeta:
    """Find a template by ``<category>/<name>`` or just ``<name>``."""
    metas = discover()
    by_full = {m.slug: m for m in metas}
    if slug in by_full:
        return by_full[slug]
    # Fallback: match by name alone if it's unique.
    matches = [m for m in metas if m.name == slug]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        opts = ", ".join(m.slug for m in matches)
        raise KeyError(
            f"template name {slug!r} is ambiguous; pick one of: {opts}"
        )
    available = ", ".join(m.slug for m in metas)
    raise KeyError(
        f"no template named {slug!r}; available: {available}"
    )


def copy(slug: str, dest: Path) -> Path:
    """Copy template ``slug`` to ``dest``. Refuses to overwrite existing files."""
    meta = by_slug(slug)
    dest = Path(os.fspath(dest))
    if dest.exists():
        raise FileExistsError(f"refusing to overwrite {dest}")
    shutil.copy2(meta.path, dest)
    return dest


def render_index() -> str:
    """Build templates/INDEX.md content from the comment headers."""
    metas = discover()
    by_cat: Dict[str, List[TemplateMeta]] = {}
    for m in metas:
        by_cat.setdefault(m.category, []).append(m)

    lines = [
        "# Workflow templates",
        "",
        "Curated starter workflows. Copy one into your own repo with"
        " `automaton init <name> [--template <category>/<name>]`, then"
        " edit the payload defaults to taste.",
        "",
        "Each template ships with a comment block explaining what it does,"
        " what secrets/env it needs, and when it was last manually verified.",
        " CI runs every template through `validate_spec` on every PR so the"
        " catalog never drifts from the engine's schema.",
        "",
    ]
    for cat in sorted(by_cat):
        lines.append(f"## {cat}")
        lines.append("")
        for m in by_cat[cat]:
            short = m.description or m.title or "(no description)"
            # Trim to a reasonable preview length
            if len(short) > 110:
                short = short[:107] + "..."
            lines.append(f"- **`{m.slug}`** — {short}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
