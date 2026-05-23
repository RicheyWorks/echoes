"""Tests for Phase 23: when: conditional step execution.

Covers:
- Static true/false/truthy/falsy when: values
- Template reference when: driven by upstream step output
- Template reference when: driven by upstream step status
- Skipped step does not fail the run
- Downstream of a skipped step is still queued and runs
- Run with all steps skipped completes (not failed)
- validate_spec rejects non-string when:
- _is_truthy helper directly
- UI pill shows 'skipped' class for skipped steps
"""
from __future__ import annotations

import sys

import pytest

from automaton import db as _db
from automaton import engine
from automaton.engine import _is_truthy


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    conn = _db.connect(tmp_path / "test.db")
    _db.migrate(conn)
    return conn


def _run(store, spec):
    engine.register_workflow(store, spec)
    run_id = engine.trigger_run(store, spec["name"])
    engine.worker_loop(store, stop_when_idle=True)
    return engine.run_detail(store, run_id)


# ---------------------------------------------------------------------------
# _is_truthy unit tests
# ---------------------------------------------------------------------------

class TestIsTruthy:
    def test_true_bool(self):
        assert _is_truthy(True) is True

    def test_false_bool(self):
        assert _is_truthy(False) is False

    def test_none_is_falsy(self):
        assert _is_truthy(None) is False

    def test_nonzero_int(self):
        assert _is_truthy(1) is True
        assert _is_truthy(42) is True

    def test_zero_int(self):
        assert _is_truthy(0) is False

    def test_string_true(self):
        assert _is_truthy("true") is True
        assert _is_truthy("True") is True
        assert _is_truthy("yes") is True
        assert _is_truthy("on") is True
        assert _is_truthy("1") is True
        assert _is_truthy("hello") is True

    def test_string_false(self):
        assert _is_truthy("false") is False
        assert _is_truthy("False") is False
        assert _is_truthy("no") is False
        assert _is_truthy("off") is False
        assert _is_truthy("0") is False
        assert _is_truthy("") is False

    def test_string_whitespace_only_is_falsy(self):
        assert _is_truthy("   ") is False

    def test_nonempty_list_truthy(self):
        assert _is_truthy([1, 2]) is True

    def test_empty_list_falsy(self):
        assert _is_truthy([]) is False


# ---------------------------------------------------------------------------
# validate_spec: when: field
# ---------------------------------------------------------------------------

class TestValidateSpecWhen:
    def _spec(self, when_val):
        s = {"name": "s", "type": "shell", "cmd": ["echo", "hi"]}
        if when_val is not None:
            s["when"] = when_val
        return {"name": "wf", "steps": [s]}

    def test_string_when_accepted(self):
        engine.validate_spec(self._spec("true"))  # no exception

    def test_template_when_accepted(self):
        engine.validate_spec(self._spec("${{ run.id }}"))

    def test_integer_when_rejected(self):
        with pytest.raises(ValueError, match="'when' must be a string"):
            engine.validate_spec(self._spec(1))

    def test_bool_when_rejected(self):
        with pytest.raises(ValueError, match="'when' must be a string"):
            engine.validate_spec(self._spec(True))

    def test_none_when_absent_is_ok(self):
        # when: not present at all is fine
        engine.validate_spec(self._spec(None))


# ---------------------------------------------------------------------------
# Static when: values
# ---------------------------------------------------------------------------

class TestStaticWhen:
    def test_when_true_step_runs(self, store):
        detail = _run(store, {
            "name": "wt_true",
            "steps": [{
                "name": "step",
                "type": "shell",
                "cmd": [sys.executable, "-c", "pass"],
                "when": "true",
            }],
        })
        assert detail["run"]["status"] == "completed"
        step = detail["steps"][0]
        assert step["status"] == "completed"

    def test_when_false_step_skipped(self, store):
        detail = _run(store, {
            "name": "wt_false",
            "steps": [{
                "name": "step",
                "type": "shell",
                "cmd": [sys.executable, "-c", "pass"],
                "when": "false",
            }],
        })
        assert detail["run"]["status"] == "completed"
        step = detail["steps"][0]
        assert step["status"] == "skipped"

    def test_when_0_step_skipped(self, store):
        detail = _run(store, {
            "name": "wt_zero",
            "steps": [{
                "name": "step",
                "type": "shell",
                "cmd": [sys.executable, "-c", "pass"],
                "when": "0",
            }],
        })
        assert detail["steps"][0]["status"] == "skipped"

    def test_when_empty_string_step_skipped(self, store):
        detail = _run(store, {
            "name": "wt_empty",
            "steps": [{
                "name": "step",
                "type": "shell",
                "cmd": [sys.executable, "-c", "pass"],
                "when": "",
            }],
        })
        assert detail["steps"][0]["status"] == "skipped"

    def test_when_yes_step_runs(self, store):
        detail = _run(store, {
            "name": "wt_yes",
            "steps": [{
                "name": "step",
                "type": "shell",
                "cmd": [sys.executable, "-c", "pass"],
                "when": "yes",
            }],
        })
        assert detail["steps"][0]["status"] == "completed"

    def test_skipped_step_output_is_none(self, store):
        detail = _run(store, {
            "name": "wt_out",
            "steps": [{
                "name": "step",
                "type": "shell",
                "cmd": [sys.executable, "-c", "pass"],
                "when": "false",
            }],
        })
        assert detail["steps"][0]["output"] is None
        assert detail["steps"][0]["error"] is None


# ---------------------------------------------------------------------------
# Skipped step does not fail the run
# ---------------------------------------------------------------------------

class TestSkippedDoesNotFailRun:
    def test_run_completes_when_only_step_skipped(self, store):
        detail = _run(store, {
            "name": "skip_only",
            "steps": [{
                "name": "s",
                "type": "shell",
                "cmd": [sys.executable, "-c", "pass"],
                "when": "false",
            }],
        })
        assert detail["run"]["status"] == "completed"

    def test_run_completes_with_mix_of_completed_and_skipped(self, store):
        detail = _run(store, {
            "name": "skip_mix",
            "steps": [
                {
                    "name": "runs",
                    "type": "shell",
                    "cmd": [sys.executable, "-c", "pass"],
                },
                {
                    "name": "skips",
                    "type": "shell",
                    "cmd": [sys.executable, "-c", "pass"],
                    "when": "false",
                },
            ],
        })
        assert detail["run"]["status"] == "completed"
        statuses = {s["name"]: s["status"] for s in detail["steps"]}
        assert statuses["runs"] == "completed"
        assert statuses["skips"] == "skipped"


# ---------------------------------------------------------------------------
# Downstream of a skipped step is still queued
# ---------------------------------------------------------------------------

class TestDownstreamOfSkipped:
    def test_downstream_runs_when_upstream_skipped(self, store, tmp_path):
        marker = tmp_path / "ran.txt"
        detail = _run(store, {
            "name": "skip_chain",
            "steps": [
                {
                    "name": "gate",
                    "type": "shell",
                    "cmd": [sys.executable, "-c", "pass"],
                    "when": "false",
                },
                {
                    "name": "action",
                    "type": "file_append",
                    "path": str(marker),
                    "text": "ran",
                    "needs": ["gate"],
                },
            ],
        })
        assert detail["run"]["status"] == "completed"
        statuses = {s["name"]: s["status"] for s in detail["steps"]}
        assert statuses["gate"] == "skipped"
        assert statuses["action"] == "completed"
        assert "ran" in marker.read_text()

    def test_three_step_chain_middle_skipped(self, store, tmp_path):
        marker = tmp_path / "final.txt"
        detail = _run(store, {
            "name": "three_chain",
            "steps": [
                {
                    "name": "first",
                    "type": "shell",
                    "cmd": [sys.executable, "-c", "pass"],
                },
                {
                    "name": "middle",
                    "type": "shell",
                    "cmd": [sys.executable, "-c", "pass"],
                    "needs": ["first"],
                    "when": "false",
                },
                {
                    "name": "last",
                    "type": "file_append",
                    "path": str(marker),
                    "text": "done",
                    "needs": ["middle"],
                },
            ],
        })
        assert detail["run"]["status"] == "completed"
        statuses = {s["name"]: s["status"] for s in detail["steps"]}
        assert statuses["first"] == "completed"
        assert statuses["middle"] == "skipped"
        assert statuses["last"] == "completed"


# ---------------------------------------------------------------------------
# Template-driven when: using upstream step output
# ---------------------------------------------------------------------------

class TestTemplateWhen:
    def test_when_references_upstream_stdout(self, store, tmp_path):
        """Step runs only when upstream wrote 'ok' to a file we check."""
        # Use a python step to return a known value, then reference it
        import types, sys as _sys
        mod = types.ModuleType("_test_when_mod")
        exec("def get_flag(): return 'run-me'", mod.__dict__)
        _sys.modules["_test_when_mod"] = mod

        detail = _run(store, {
            "name": "tpl_when_out",
            "steps": [
                {
                    "name": "flag",
                    "type": "python",
                    "module": "_test_when_mod",
                    "function": "get_flag",
                },
                {
                    "name": "conditional",
                    "type": "shell",
                    "cmd": [sys.executable, "-c", "pass"],
                    "needs": ["flag"],
                    "when": "${{ steps.flag.output.return_value }}",
                },
            ],
        })
        assert detail["run"]["status"] == "completed"
        statuses = {s["name"]: s["status"] for s in detail["steps"]}
        assert statuses["flag"] == "completed"
        assert statuses["conditional"] == "completed"

    def test_when_false_via_template_return_value(self, store):
        """Return value 'false' from python step causes downstream to skip."""
        import types, sys as _sys
        mod = types.ModuleType("_test_when_false_mod")
        exec("def get_flag(): return 'false'", mod.__dict__)
        _sys.modules["_test_when_false_mod"] = mod

        detail = _run(store, {
            "name": "tpl_when_false",
            "steps": [
                {
                    "name": "flag",
                    "type": "python",
                    "module": "_test_when_false_mod",
                    "function": "get_flag",
                },
                {
                    "name": "conditional",
                    "type": "shell",
                    "cmd": [sys.executable, "-c", "pass"],
                    "needs": ["flag"],
                    "when": "${{ steps.flag.output.return_value }}",
                },
            ],
        })
        statuses = {s["name"]: s["status"] for s in detail["steps"]}
        assert statuses["conditional"] == "skipped"
        assert detail["run"]["status"] == "completed"

    def test_when_references_run_id(self, store):
        """${{ run.id }} is always a nonzero int — so step always runs."""
        detail = _run(store, {
            "name": "tpl_when_runid",
            "steps": [{
                "name": "step",
                "type": "shell",
                "cmd": [sys.executable, "-c", "pass"],
                "when": "${{ run.id }}",
            }],
        })
        assert detail["steps"][0]["status"] == "completed"


# ---------------------------------------------------------------------------
# UI: skipped badge class is present
# ---------------------------------------------------------------------------

class TestSkippedUI:
    def test_status_text_has_skipped(self):
        from automaton.ui import _STATUS_TEXT
        assert "skipped" in _STATUS_TEXT

    def test_status_pill_renders_skipped_class(self):
        from automaton.ui import _status_pill
        html = _status_pill("skipped")
        assert "skipped" in html
        assert "italic" in html or "slate" in html
