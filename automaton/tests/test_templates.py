"""Template-library catalog tests.

These keep the templates honest on every PR:

  - Every shipped template parses as YAML.
  - Every template passes `engine.validate_spec`, so a schema change
    can't ship without bringing its templates along.
  - Every template has a complete leading comment block (title,
    description, cron, last_verified) - so `automaton init --list`
    and `templates/INDEX.md` stay informative.
  - The INDEX.md is in sync with discover().
  - `templates.copy` round-trips correctly and refuses to clobber.
  - `templates.by_slug` handles ambiguous names cleanly.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from automaton import engine
from automaton import templates as _templates


def _all_metas():
    return _templates.discover()


@pytest.mark.parametrize("meta", _all_metas(),
                          ids=lambda m: m.slug)
def test_template_parses_and_validates(meta):
    """Every shipped template parses + survives engine.validate_spec."""
    raw = meta.path.read_text(encoding="utf-8")
    spec = yaml.safe_load(raw)
    assert isinstance(spec, dict), f"{meta.slug} didn't parse to a dict"
    # validate_spec raises on any schema issue.
    engine.validate_spec(spec)


@pytest.mark.parametrize("meta", _all_metas(),
                          ids=lambda m: m.slug)
def test_template_has_header_fields(meta):
    """Each template ships with the full comment block. Missing fields
    make the catalog and CLI less useful."""
    assert meta.title, f"{meta.slug} missing title (first comment line)"
    assert meta.description, f"{meta.slug} missing 'Description:' field"
    assert meta.last_verified, f"{meta.slug} missing 'Last verified:' field"


def test_index_in_sync_with_discover():
    """templates/INDEX.md must reflect the current set of templates.
    If this fails, run: python -c 'from automaton import templates; \
       open(\"templates/INDEX.md\", \"w\").write(templates.render_index())'"""
    expected = _templates.render_index()
    actual_path = _templates.TEMPLATES_ROOT / "INDEX.md"
    assert actual_path.exists(), "templates/INDEX.md is missing"
    actual = actual_path.read_text(encoding="utf-8")
    assert actual == expected, (
        "templates/INDEX.md is stale. Regenerate with:\n"
        "  python -c 'from automaton import templates; "
        "open(\"templates/INDEX.md\", \"w\").write(templates.render_index())'"
    )


def test_copy_round_trips(tmp_path):
    """A template copied via templates.copy is byte-identical to source."""
    metas = _all_metas()
    assert metas, "no templates discovered"
    sample = metas[0]
    dest = tmp_path / f"{sample.name}.yaml"
    out = _templates.copy(sample.slug, dest)
    assert out == dest
    assert dest.read_bytes() == sample.path.read_bytes()


def test_copy_refuses_to_clobber(tmp_path):
    sample = _all_metas()[0]
    dest = tmp_path / "x.yaml"
    dest.write_text("existing", encoding="utf-8")
    with pytest.raises(FileExistsError):
        _templates.copy(sample.slug, dest)


def test_by_slug_finds_by_full_slug():
    meta = _templates.by_slug("health/website-up")
    assert meta.slug == "health/website-up"


def test_by_slug_finds_by_name_alone_when_unique():
    """If only one template has a given name, the category prefix is optional."""
    meta = _templates.by_slug("website-up")
    assert meta.name == "website-up"


def test_by_slug_unknown_raises_with_listing():
    with pytest.raises(KeyError, match="no template"):
        _templates.by_slug("definitely-not-real")


def test_catalog_has_minimum_ten_templates():
    """Phase 12 shipped 10. A regression below that means someone deleted
    files; bumping above is fine - just update this number."""
    assert len(_all_metas()) >= 10, [m.slug for m in _all_metas()]
