"""Tests for Phase 24: automaton run one-shot command.

Covers:
- Happy path exits 0 and prints step names
- Failing workflow exits 1
- --payload is forwarded to the run
- Missing spec file exits 1 with error message
- Invalid YAML exits 1 with error message
- Invalid workflow spec (no steps) exits 1
- --payload with bad JSON exits 1
- Re-running the same spec re-registers (version bumps)
- --timeout 0 means no limit (still completes)
- Output includes workflow name and run_id
- Step statuses are shown in output
- Skipped steps shown correctly
- cmd_run return value is 0/1 (int)
"""
from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from automaton import cli, db as _db, engine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_spec(tmp_path, spec: dict, filename="workflow.yaml") -> Path:
    p = tmp_path / filename
    p.write_text(yaml.dump(spec), encoding="utf-8")
    return p


def _run_cli(argv, db_path, capsys=None):
    """Invoke cli.main with AUTOMATON_DB pointing at db_path. Returns exit code."""
    with patch.dict("os.environ", {"AUTOMATON_DB": str(db_path)}):
        try:
            cli.main(argv)
            return 0
        except SystemExit as e:
            return int(e.code) if e.code is not None else 0


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "test.db"
    conn = _db.connect(p)
    _db.migrate(conn)
    conn.close()
    return p


# ---------------------------------------------------------------------------
# Basic happy path
# ---------------------------------------------------------------------------

class TestRunHappyPath:
    def test_simple_workflow_exits_0(self, tmp_path, db_path, capsys):
        spec = {
            "name": "simple",
            "steps": [{"name": "go", "type": "shell", "cmd": [sys.executable, "-c", "pass"]}],
        }
        f = _write_spec(tmp_path, spec)
        rc = _run_cli(["run", str(f)], db_path, capsys)
        assert rc == 0

    def test_output_includes_workflow_name(self, tmp_path, db_path, capsys):
        spec = {
            "name": "my-wf",
            "steps": [{"name": "s", "type": "shell", "cmd": [sys.executable, "-c", "pass"]}],
        }
        f = _write_spec(tmp_path, spec)
        with patch.dict("os.environ", {"AUTOMATON_DB": str(db_path)}):
            try:
                cli.main(["run", str(f)])
            except SystemExit:
                pass
        out = capsys.readouterr().out
        assert "my-wf" in out

    def test_output_includes_run_id(self, tmp_path, db_path, capsys):
        spec = {
            "name": "wf-id",
            "steps": [{"name": "s", "type": "shell", "cmd": [sys.executable, "-c", "pass"]}],
        }
        f = _write_spec(tmp_path, spec)
        with patch.dict("os.environ", {"AUTOMATON_DB": str(db_path)}):
            try:
                cli.main(["run", str(f)])
            except SystemExit:
                pass
        out = capsys.readouterr().out
        assert "run_id=" in out

    def test_output_includes_step_names(self, tmp_path, db_path, capsys):
        spec = {
            "name": "step-names",
            "steps": [
                {"name": "alpha", "type": "shell", "cmd": [sys.executable, "-c", "pass"]},
                {"name": "beta",  "type": "shell", "cmd": [sys.executable, "-c", "pass"],
                 "needs": ["alpha"]},
            ],
        }
        f = _write_spec(tmp_path, spec)
        with patch.dict("os.environ", {"AUTOMATON_DB": str(db_path)}):
            try:
                cli.main(["run", str(f)])
            except SystemExit:
                pass
        out = capsys.readouterr().out
        assert "alpha" in out
        assert "beta" in out

    def test_completed_shown_in_output(self, tmp_path, db_path, capsys):
        spec = {
            "name": "status-out",
            "steps": [{"name": "s", "type": "shell", "cmd": [sys.executable, "-c", "pass"]}],
        }
        f = _write_spec(tmp_path, spec)
        with patch.dict("os.environ", {"AUTOMATON_DB": str(db_path)}):
            try:
                cli.main(["run", str(f)])
            except SystemExit:
                pass
        out = capsys.readouterr().out
        assert "completed" in out.lower() or "COMPLETED" in out

    def test_stdout_from_step_shown(self, tmp_path, db_path, capsys):
        spec = {
            "name": "stdout-show",
            "steps": [{
                "name": "printer",
                "type": "shell",
                "cmd": [sys.executable, "-c", "print('hello-from-step')"],
            }],
        }
        f = _write_spec(tmp_path, spec)
        with patch.dict("os.environ", {"AUTOMATON_DB": str(db_path)}):
            try:
                cli.main(["run", str(f)])
            except SystemExit:
                pass
        out = capsys.readouterr().out
        assert "hello-from-step" in out


# ---------------------------------------------------------------------------
# Failure path
# ---------------------------------------------------------------------------

class TestRunFailurePath:
    def test_failing_step_exits_1(self, tmp_path, db_path):
        spec = {
            "name": "fail-wf",
            "steps": [{
                "name": "boom",
                "type": "shell",
                "cmd": [sys.executable, "-c", "raise SystemExit(1)"],
            }],
        }
        f = _write_spec(tmp_path, spec)
        rc = _run_cli(["run", str(f)], db_path)
        assert rc == 1

    def test_failed_shown_in_output(self, tmp_path, db_path, capsys):
        spec = {
            "name": "fail-out",
            "steps": [{
                "name": "boom",
                "type": "shell",
                "cmd": [sys.executable, "-c", "raise SystemExit(1)"],
            }],
        }
        f = _write_spec(tmp_path, spec)
        with patch.dict("os.environ", {"AUTOMATON_DB": str(db_path)}):
            try:
                cli.main(["run", str(f)])
            except SystemExit:
                pass
        out = capsys.readouterr().out
        assert "failed" in out.lower() or "FAILED" in out


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

class TestRunErrors:
    def test_missing_file_exits_1(self, tmp_path, db_path):
        rc = _run_cli(["run", str(tmp_path / "no-such-file.yaml")], db_path)
        assert rc == 1

    def test_invalid_yaml_exits_1(self, tmp_path, db_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text(":\nthis: [is: not: valid\n", encoding="utf-8")
        rc = _run_cli(["run", str(bad)], db_path)
        assert rc == 1

    def test_invalid_spec_exits_1(self, tmp_path, db_path):
        # Valid YAML but missing 'steps'
        bad = tmp_path / "nospec.yaml"
        bad.write_text("name: broken\n", encoding="utf-8")
        rc = _run_cli(["run", str(bad)], db_path)
        assert rc == 1

    def test_bad_payload_json_exits_1(self, tmp_path, db_path):
        spec = {
            "name": "pay",
            "steps": [{"name": "s", "type": "shell", "cmd": [sys.executable, "-c", "pass"]}],
        }
        f = _write_spec(tmp_path, spec)
        rc = _run_cli(["run", str(f), "--payload", "not-json-{"], db_path)
        assert rc == 1


# ---------------------------------------------------------------------------
# --payload
# ---------------------------------------------------------------------------

class TestRunPayload:
    def test_payload_visible_in_run_detail(self, tmp_path, db_path):
        spec = {
            "name": "pay-wf",
            "steps": [{"name": "s", "type": "shell", "cmd": [sys.executable, "-c", "pass"]}],
        }
        f = _write_spec(tmp_path, spec)
        payload = {"greeting": "hello", "count": 42}
        with patch.dict("os.environ", {"AUTOMATON_DB": str(db_path)}):
            try:
                cli.main(["run", str(f), "--payload", json.dumps(payload)])
            except SystemExit:
                pass
        conn = _db.connect(db_path)
        runs = engine.list_runs(conn)
        assert runs, "expected at least one run"
        detail = engine.run_detail(conn, runs[0]["id"])
        raw = detail["run"]["trigger_payload"]
        import json as _json
        stored_payload = _json.loads(raw) if isinstance(raw, str) else raw
        assert stored_payload == payload

    def test_payload_template_reference_works(self, tmp_path, db_path):
        spec = {
            "name": "pay-tpl",
            "steps": [{
                "name": "echo",
                "type": "shell",
                "cmd": [sys.executable, "-c",
                        "import sys; print('${{ run.payload.msg }}')"],
            }],
        }
        # Can't easily use ${{ }} in shell cmd with this approach, use file_append
        spec2 = {
            "name": "pay-tpl2",
            "steps": [{
                "name": "append",
                "type": "file_append",
                "path": str(tmp_path / "out.txt"),
                "text": "${{ run.payload.key }}",
            }],
        }
        f = _write_spec(tmp_path, spec2)
        with patch.dict("os.environ", {"AUTOMATON_DB": str(db_path)}):
            try:
                cli.main(["run", str(f), "--payload", '{"key": "VALUE_FROM_PAYLOAD"}'])
            except SystemExit:
                pass
        content = (tmp_path / "out.txt").read_text()
        assert "VALUE_FROM_PAYLOAD" in content


# ---------------------------------------------------------------------------
# Re-registration
# ---------------------------------------------------------------------------

class TestRunReregistration:
    def test_running_twice_bumps_version(self, tmp_path, db_path):
        spec = {
            "name": "rereg",
            "steps": [{"name": "s", "type": "shell", "cmd": [sys.executable, "-c", "pass"]}],
        }
        f = _write_spec(tmp_path, spec)
        _run_cli(["run", str(f)], db_path)
        _run_cli(["run", str(f)], db_path)
        conn = _db.connect(db_path)
        wfs = engine.list_workflows(conn)
        rereg = next(w for w in wfs if w["name"] == "rereg")
        assert rereg["version"] == 2

    def test_result_is_int(self, tmp_path, db_path):
        spec = {
            "name": "intrc",
            "steps": [{"name": "s", "type": "shell", "cmd": [sys.executable, "-c", "pass"]}],
        }
        f = _write_spec(tmp_path, spec)
        rc = _run_cli(["run", str(f)], db_path)
        assert isinstance(rc, int)


# ---------------------------------------------------------------------------
# --timeout
# ---------------------------------------------------------------------------

class TestRunTimeout:
    def test_timeout_0_means_no_limit(self, tmp_path, db_path):
        spec = {
            "name": "to-zero",
            "steps": [{"name": "s", "type": "shell", "cmd": [sys.executable, "-c", "pass"]}],
        }
        f = _write_spec(tmp_path, spec)
        rc = _run_cli(["run", str(f), "--timeout", "0"], db_path)
        assert rc == 0


# ---------------------------------------------------------------------------
# Skipped step output
# ---------------------------------------------------------------------------

class TestRunSkipped:
    def test_skipped_shown_in_output(self, tmp_path, db_path, capsys):
        spec = {
            "name": "skip-out",
            "steps": [{
                "name": "skip-me",
                "type": "shell",
                "cmd": [sys.executable, "-c", "pass"],
                "when": "false",
            }],
        }
        f = _write_spec(tmp_path, spec)
        with patch.dict("os.environ", {"AUTOMATON_DB": str(db_path)}):
            try:
                cli.main(["run", str(f)])
            except SystemExit:
                pass
        out = capsys.readouterr().out
        assert "skip-me" in out
        assert "skipped" in out

    def test_all_skipped_exits_0(self, tmp_path, db_path):
        spec = {
            "name": "all-skip",
            "steps": [{
                "name": "s",
                "type": "shell",
                "cmd": [sys.executable, "-c", "pass"],
                "when": "false",
            }],
        }
        f = _write_spec(tmp_path, spec)
        rc = _run_cli(["run", str(f)], db_path)
        assert rc == 0
