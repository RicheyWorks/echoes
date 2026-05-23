"""Tests for Phase 25: workflow required inputs (inputs: field).

Covers:
- validate_spec: accepts valid inputs: list
- validate_spec: rejects non-list inputs:
- validate_spec: rejects non-string items
- validate_spec: rejects empty-string items
- validate_spec: empty list [] is fine
- validate_spec: no inputs: field is fine
- trigger_run: succeeds when all inputs satisfied
- trigger_run: succeeds when no inputs declared
- trigger_run: succeeds when inputs: [] (empty)
- trigger_run: raises ValueError with list of missing keys
- trigger_run: missing one of several inputs
- trigger_run: no payload and inputs required raises
- trigger_run: extra payload keys beyond required are fine
- automaton run: --payload satisfies inputs, exits 0
- automaton run: missing inputs exits 1 with message
- Template references to payload inputs work end-to-end
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from automaton import cli, db as _db, engine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    conn = _db.connect(tmp_path / "test.db")
    _db.migrate(conn)
    return conn


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "test.db"
    conn = _db.connect(p)
    _db.migrate(conn)
    conn.close()
    return p


def _simple_step():
    return [{"name": "s", "type": "shell", "cmd": [sys.executable, "-c", "pass"]}]


def _spec(name, inputs=None, steps=None):
    d = {"name": name, "steps": steps or _simple_step()}
    if inputs is not None:
        d["inputs"] = inputs
    return d


def _run_cli(argv, db_path):
    with patch.dict("os.environ", {"AUTOMATON_DB": str(db_path)}):
        try:
            cli.main(argv)
            return 0
        except SystemExit as e:
            return int(e.code) if e.code is not None else 0


def _write_spec(tmp_path, spec, filename="wf.yaml"):
    p = tmp_path / filename
    p.write_text(yaml.dump(spec), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# validate_spec
# ---------------------------------------------------------------------------

class TestValidateSpecInputs:
    def test_no_inputs_field_ok(self):
        engine.validate_spec(_spec("no-inputs"))

    def test_empty_list_ok(self):
        engine.validate_spec(_spec("empty-inputs", inputs=[]))

    def test_single_string_ok(self):
        engine.validate_spec(_spec("one", inputs=["recipient"]))

    def test_multiple_strings_ok(self):
        engine.validate_spec(_spec("multi", inputs=["a", "b", "c"]))

    def test_non_list_rejected(self):
        with pytest.raises(ValueError, match="list"):
            engine.validate_spec(_spec("bad", inputs="recipient"))

    def test_dict_rejected(self):
        with pytest.raises(ValueError, match="list"):
            engine.validate_spec(_spec("bad", inputs={"key": "value"}))

    def test_non_string_item_rejected(self):
        with pytest.raises(ValueError, match="non-empty string"):
            engine.validate_spec(_spec("bad", inputs=["ok", 42]))

    def test_empty_string_item_rejected(self):
        with pytest.raises(ValueError, match="non-empty string"):
            engine.validate_spec(_spec("bad", inputs=["ok", ""]))

    def test_whitespace_only_item_rejected(self):
        with pytest.raises(ValueError, match="non-empty string"):
            engine.validate_spec(_spec("bad", inputs=["ok", "  "]))

    def test_none_item_rejected(self):
        with pytest.raises(ValueError, match="non-empty string"):
            engine.validate_spec(_spec("bad", inputs=["ok", None]))


# ---------------------------------------------------------------------------
# trigger_run input validation
# ---------------------------------------------------------------------------

class TestTriggerRunInputs:
    def test_no_inputs_no_payload_ok(self, store):
        engine.register_workflow(store, _spec("no-in"))
        run_id = engine.trigger_run(store, "no-in")
        assert run_id > 0

    def test_empty_inputs_list_no_payload_ok(self, store):
        engine.register_workflow(store, _spec("empty-in", inputs=[]))
        run_id = engine.trigger_run(store, "empty-in")
        assert run_id > 0

    def test_required_input_present_ok(self, store):
        engine.register_workflow(store, _spec("one-in", inputs=["key"]))
        run_id = engine.trigger_run(store, "one-in", trigger_payload={"key": "val"})
        assert run_id > 0

    def test_multiple_inputs_all_present_ok(self, store):
        engine.register_workflow(store, _spec("multi-in", inputs=["a", "b", "c"]))
        run_id = engine.trigger_run(store, "multi-in",
                                     trigger_payload={"a": 1, "b": 2, "c": 3})
        assert run_id > 0

    def test_extra_payload_keys_ok(self, store):
        engine.register_workflow(store, _spec("extra", inputs=["needed"]))
        run_id = engine.trigger_run(store, "extra",
                                     trigger_payload={"needed": "x", "extra": "y"})
        assert run_id > 0

    def test_missing_single_input_raises(self, store):
        engine.register_workflow(store, _spec("miss1", inputs=["recipient"]))
        with pytest.raises(ValueError, match="recipient"):
            engine.trigger_run(store, "miss1")

    def test_missing_one_of_several_raises(self, store):
        engine.register_workflow(store, _spec("miss2", inputs=["a", "b", "c"]))
        with pytest.raises(ValueError, match="c"):
            engine.trigger_run(store, "miss2", trigger_payload={"a": 1, "b": 2})

    def test_missing_all_of_several_raises(self, store):
        engine.register_workflow(store, _spec("miss3", inputs=["x", "y"]))
        with pytest.raises(ValueError) as exc:
            engine.trigger_run(store, "miss3")
        msg = str(exc.value)
        assert "x" in msg
        assert "y" in msg

    def test_no_payload_with_required_input_raises(self, store):
        engine.register_workflow(store, _spec("nopay", inputs=["key"]))
        with pytest.raises(ValueError, match="key"):
            engine.trigger_run(store, "nopay", trigger_payload=None)

    def test_error_message_names_workflow(self, store):
        engine.register_workflow(store, _spec("my-wf", inputs=["token"]))
        with pytest.raises(ValueError, match="my-wf"):
            engine.trigger_run(store, "my-wf")

    def test_run_not_created_on_missing_input(self, store):
        """No orphaned run row when trigger_run raises."""
        engine.register_workflow(store, _spec("orphan", inputs=["k"]))
        before = len(engine.list_runs(store))
        with pytest.raises(ValueError):
            engine.trigger_run(store, "orphan")
        after = len(engine.list_runs(store))
        assert after == before


# ---------------------------------------------------------------------------
# End-to-end: payload inputs used in templates
# ---------------------------------------------------------------------------

class TestInputsE2E:
    def test_payload_input_used_in_step_template(self, store, tmp_path):
        out = tmp_path / "result.txt"
        engine.register_workflow(store, {
            "name": "tpl-in",
            "inputs": ["target"],
            "steps": [{
                "name": "write",
                "type": "file_append",
                "path": str(out),
                "text": "${{ run.payload.target }}",
            }],
        })
        engine.trigger_run(store, "tpl-in", trigger_payload={"target": "HELLO"})
        engine.worker_loop(store, stop_when_idle=True)
        assert "HELLO" in out.read_text()

    def test_missing_input_prevents_run_from_being_created(self, store):
        engine.register_workflow(store, _spec("guard", inputs=["secret_key"]))
        with pytest.raises(ValueError):
            engine.trigger_run(store, "guard")
        # Nothing in run history
        assert engine.list_runs(store) == []


# ---------------------------------------------------------------------------
# automaton run CLI integration
# ---------------------------------------------------------------------------

class TestCliRunWithInputs:
    def test_payload_satisfies_inputs_exits_0(self, tmp_path, db_path):
        spec = {
            "name": "cli-in",
            "inputs": ["name"],
            "steps": [{"name": "s", "type": "shell",
                        "cmd": [sys.executable, "-c", "pass"]}],
        }
        f = _write_spec(tmp_path, spec)
        rc = _run_cli(["run", str(f), "--payload", '{"name": "Alice"}'], db_path)
        assert rc == 0

    def test_missing_input_exits_1(self, tmp_path, db_path):
        spec = {
            "name": "cli-miss",
            "inputs": ["required_key"],
            "steps": [{"name": "s", "type": "shell",
                        "cmd": [sys.executable, "-c", "pass"]}],
        }
        f = _write_spec(tmp_path, spec)
        # No --payload → missing required_key
        rc = _run_cli(["run", str(f)], db_path)
        assert rc == 1

    def test_missing_input_error_message(self, tmp_path, db_path, capsys):
        spec = {
            "name": "cli-miss2",
            "inputs": ["the_token"],
            "steps": [{"name": "s", "type": "shell",
                        "cmd": [sys.executable, "-c", "pass"]}],
        }
        f = _write_spec(tmp_path, spec)
        with patch.dict("os.environ", {"AUTOMATON_DB": str(db_path)}):
            try:
                cli.main(["run", str(f)])
            except SystemExit:
                pass
        err = capsys.readouterr().err
        assert "the_token" in err or "required" in err.lower() or "missing" in err.lower()
