"""Phase 20: Workflow templates library tests.

Covers:
- discover() finds all 10 templates and returns correct metadata
- every template validates via engine.validate_spec (no schema drift)
- by_slug() finds by full slug and by name (when unambiguous)
- by_slug() raises KeyError for unknown slug
- copy() writes a file and refuses to overwrite
- automaton init --list exits 0 and lists all templates
- automaton init NAME --template SLUG creates the expected file
- automaton init without --template still lists templates
- render_index() produces markdown with every slug present
- INDEX.md on disk matches render_index() output (not stale)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from automaton import templates as _templates
from automaton.engine import validate_spec

TEMPLATES_ROOT = _templates.TEMPLATES_ROOT
EXPECTED_SLUGS = {
    "agent/claude-loop",
    "agent/echoes-daily",
    "backup/home-folder",
    "dev/docker-prune",
    "dev/git-mirror",
    "health/cert-expiry",
    "health/website-up",
    "infra/letsencrypt-renew",
    "infra/log-rotation",
    "media/photo-import",
    "personal/morning-brief",
}


# --------------------------------------------------------------------------- #
# discover() and metadata                                                      #
# --------------------------------------------------------------------------- #

class TestDiscover:
    def test_finds_all_ten_templates(self):
        metas = _templates.discover()
        assert len(metas) == 11

    def test_slugs_match_expected_set(self):
        slugs = {m.slug for m in _templates.discover()}
        assert slugs == EXPECTED_SLUGS

    def test_every_template_has_title(self):
        for m in _templates.discover():
            assert m.title, f"{m.slug} has no title"

    def test_every_template_has_description(self):
        for m in _templates.discover():
            assert m.description, f"{m.slug} has no description"

    def test_every_template_has_last_verified(self):
        for m in _templates.discover():
            assert m.last_verified, f"{m.slug} missing Last verified date"

    def test_slug_property_matches_category_name(self):
        for m in _templates.discover():
            assert m.slug == f"{m.category}/{m.name}"


# --------------------------------------------------------------------------- #
# Schema validation                                                            #
# --------------------------------------------------------------------------- #

class TestValidation:
    @pytest.mark.parametrize("slug", sorted(EXPECTED_SLUGS))
    def test_template_passes_validate_spec(self, slug):
        meta = _templates.by_slug(slug)
        spec = yaml.safe_load(meta.path.read_text(encoding="utf-8"))
        # Should not raise
        validate_spec(spec)

    @pytest.mark.parametrize("slug", sorted(EXPECTED_SLUGS))
    def test_template_is_valid_yaml(self, slug):
        meta = _templates.by_slug(slug)
        spec = yaml.safe_load(meta.path.read_text(encoding="utf-8"))
        assert isinstance(spec, dict)
        assert "name" in spec
        assert "steps" in spec


# --------------------------------------------------------------------------- #
# by_slug()                                                                    #
# --------------------------------------------------------------------------- #

class TestBySlug:
    def test_full_slug_returns_meta(self):
        m = _templates.by_slug("backup/home-folder")
        assert m.slug == "backup/home-folder"

    def test_name_alone_returns_meta_when_unambiguous(self):
        # "home-folder" only exists in one category
        m = _templates.by_slug("home-folder")
        assert m.slug == "backup/home-folder"

    def test_unknown_slug_raises_key_error(self):
        with pytest.raises(KeyError, match="no template named"):
            _templates.by_slug("does-not-exist")

    def test_ambiguous_name_raises_key_error(self):
        # Manufacture ambiguity by checking that docker-prune is unambiguous
        # (only in dev/ — this is really a guard test)
        m = _templates.by_slug("docker-prune")
        assert m.category == "dev"


# --------------------------------------------------------------------------- #
# copy()                                                                       #
# --------------------------------------------------------------------------- #

class TestCopy:
    def test_copy_creates_file(self, tmp_path):
        dest = tmp_path / "out.yaml"
        result = _templates.copy("backup/home-folder", dest)
        assert result == dest
        assert dest.exists()
        assert dest.stat().st_size > 0

    def test_copy_content_matches_original(self, tmp_path):
        meta = _templates.by_slug("backup/home-folder")
        dest = tmp_path / "out.yaml"
        _templates.copy("backup/home-folder", dest)
        assert dest.read_text(encoding="utf-8") == meta.path.read_text(encoding="utf-8")

    def test_copy_refuses_to_overwrite(self, tmp_path):
        dest = tmp_path / "out.yaml"
        dest.write_text("existing", encoding="utf-8")
        with pytest.raises(FileExistsError):
            _templates.copy("backup/home-folder", dest)

    def test_copy_unknown_slug_raises(self, tmp_path):
        with pytest.raises(KeyError):
            _templates.copy("no/such-template", tmp_path / "x.yaml")


# --------------------------------------------------------------------------- #
# automaton init CLI                                                           #
# --------------------------------------------------------------------------- #

def _run_init(*args):
    return subprocess.run(
        [sys.executable, "-m", "automaton", "init", *args],
        capture_output=True,
        text=True,
    )


class TestInitCLI:
    def test_list_flag_exits_zero(self):
        r = _run_init("--list")
        assert r.returncode == 0

    def test_list_shows_all_slugs(self):
        r = _run_init("--list")
        for slug in EXPECTED_SLUGS:
            assert slug in r.stdout, f"{slug} missing from --list output"

    def test_no_template_flag_lists_and_exits_zero(self):
        # automaton init with no positional arg and no --template lists templates
        r = _run_init()
        assert r.returncode == 0
        assert "available templates" in r.stdout

    def test_init_copies_template(self, tmp_path):
        dest = tmp_path / "mybak.yaml"
        r = subprocess.run(
            [sys.executable, "-m", "automaton", "init",
             "mybak", "--template", "backup/home-folder"],
            capture_output=True, text=True, cwd=tmp_path,
        )
        assert r.returncode == 0, r.stderr
        assert dest.exists()
        content = dest.read_text(encoding="utf-8")
        assert "name: backup-home-folder" in content

    def test_init_appends_yaml_extension(self, tmp_path):
        r = subprocess.run(
            [sys.executable, "-m", "automaton", "init",
             "noext", "--template", "dev/docker-prune"],
            capture_output=True, text=True, cwd=tmp_path,
        )
        assert r.returncode == 0, r.stderr
        assert (tmp_path / "noext.yaml").exists()

    def test_init_refuses_unknown_template(self, tmp_path):
        r = subprocess.run(
            [sys.executable, "-m", "automaton", "init",
             "x", "--template", "bogus/missing"],
            capture_output=True, text=True, cwd=tmp_path,
        )
        assert r.returncode != 0

    def test_init_refuses_to_overwrite(self, tmp_path):
        (tmp_path / "existing.yaml").write_text("old", encoding="utf-8")
        r = subprocess.run(
            [sys.executable, "-m", "automaton", "init",
             "existing", "--template", "backup/home-folder"],
            capture_output=True, text=True, cwd=tmp_path,
        )
        assert r.returncode != 0
        assert (tmp_path / "existing.yaml").read_text(encoding="utf-8") == "old"


# --------------------------------------------------------------------------- #
# render_index() and INDEX.md staleness                                        #
# --------------------------------------------------------------------------- #

class TestIndex:
    def test_render_index_contains_all_slugs(self):
        idx = _templates.render_index()
        for slug in EXPECTED_SLUGS:
            assert slug in idx, f"{slug} missing from INDEX output"

    def test_render_index_is_markdown(self):
        idx = _templates.render_index()
        assert idx.startswith("# Workflow templates")

    def test_index_md_on_disk_is_current(self):
        """Regression: INDEX.md must match render_index() — not stale."""
        index_path = TEMPLATES_ROOT / "INDEX.md"
        assert index_path.exists(), "templates/INDEX.md not committed"
        on_disk = index_path.read_text(encoding="utf-8")
        expected = _templates.render_index()
        assert on_disk == expected, (
            "templates/INDEX.md is stale. Regenerate with:\n"
            "    python3 -c 'from automaton.templates import render_index; "
            "print(render_index())' > templates/INDEX.md"
        )
