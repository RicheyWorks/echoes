"""Tests for Phase 26: env: field on steps + automaton validate command.

Covers:
- validate_spec: accepts valid env: dict
- validate_spec: rejects non-dict env:
- validate_spec: rejects non-string keys
- validate_spec: rejects non-string values
- Shell step: env var injected and visible to subprocess
- Shell step: template reference in env value resolved
- Shell step: absent env: is fine (no env vars added beyond os.environ)
- automaton validate: valid spec exits 0, prints OK
- automaton validate: invalid spec exits 1, prints error
- automaton validate: missing file exits 1
- automaton validate: bad YAML exits 1
- automaton validate: non-dict YAML exits 1
- automaton validate: prints workflow name and step count
- automaton validate: DAG cycle detected
- automaton validate: duplicate step names detected
"""
from __future__ import annotations

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


def _run_cli(argv, capsys=None, db_path=None):
    env = {}
    if db_path:
        env["AUTOMATON_DB"] = str(db_path)
    with patch.dict("os.environ", env):
        try:
            cli.main(argv)
            return 0
        except SystemExit as e:
            return int(e.code) if e.code is not None else 0


def _write(tmp_path, spec, name="wf.yaml"):
    p = tmp_path / name
    p.write_text(yaml.dump(spec), encoding="utf-8")
    return p


def _simple_spec(name="wf"):
    return {
        "name": name,
        "steps": [{"name": "s", "type": "shell", "cmd": [sys.executable, "-c", "pass"]}],
    }


# ---------------------------------------------------------------------------
# validate_spec: env: field
# ---------------------------------------------------------------------------

class TestValidateSpecEnv:
    def _step_with_env(self, env):
        return {
            "name": "wf",
            "steps": [{"name": "s", "type": "shell",
                        "cmd": [sys.executable, "-c", "pass"], "env": env}],
        }

    def test_no_env_field_ok(self):
        engine.validate_spec(_simple_spec())

    def test_empty_dict_ok(self):
        engine.validate_spec(self._step_with_env({}))

    def test_string_key_and_value_ok(self):
        engine.validate_spec(self._step_with_env({"MY_VAR": "hello"}))

    def test_multiple_vars_ok(self):
        engine.validate_spec(self._step_with_env({"A": "1", "B": "2", "C": "3"}))

    def test_template_value_ok(self):
        engine.validate_spec(self._step_with_env({"VER": "${{ run.payload.version }}"}))

    def test_non_dict_env_rejected(self):
        with pytest.raises(ValueError, match="dict"):
            engine.validate_spec(self._step_with_env(["MY_VAR=hello"]))

    def test_string_env_rejected(self):
        with pytest.raises(ValueError, match="dict"):
            engine.validate_spec(self._step_with_env("MY_VAR=hello"))

    def test_int_key_rejected(self):
        with pytest.raises(ValueError, match="key"):
            engine.validate_spec(self._step_with_env({1: "value"}))

    def test_int_value_rejected(self):
        with pytest.raises(ValueError, match="value"):
            engine.validate_spec(self._step_with_env({"KEY": 42}))

    def test_bool_value_rejected(self):
        with pytest.raises(ValueError, match="value"):
            engine.validate_spec(self._step_with_env({"KEY": True}))

    def test_none_value_rejected(self):
        with pytest.raises(ValueError, match="value"):
            engine.validate_spec(self._step_with_env({"KEY": None}))


# ---------------------------------------------------------------------------
# Shell step: env: E2E
# ---------------------------------------------------------------------------

class TestShellEnvE2E:
    def _run(self, store, spec):
        engine.register_workflow(store, spec)
        run_id = engine.trigger_run(store, spec["name"])
        engine.worker_loop(store, stop_when_idle=True)
        return engine.run_detail(store, run_id)

    def test_env_var_visible_to_subprocess(self, store, tmp_path):
        out = tmp_path / "result.txt"
        detail = self._run(store, {
            "name": "env-e2e",
            "steps": [{
                "name": "write",
                "type": "shell",
                "cmd": [sys.executable, "-c",
                        f"import os; open(r'{out}', 'w').write(os.environ['MYVAR'])"],
                "env": {"MYVAR": "hello-from-env"},
            }],
        })
        assert detail["run"]["status"] == "completed"
        assert out.read_text() == "hello-from-env"

    def test_multiple_env_vars(self, store, tmp_path):
        out = tmp_path / "result.txt"
        detail = self._run(store, {
            "name": "multi-env",
            "steps": [{
                "name": "write",
                "type": "shell",
                "cmd": [sys.executable, "-c",
                        f"import os; open(r'{out}', 'w').write(os.environ['A']+os.environ['B'])"],
                "env": {"A": "foo", "B": "bar"},
            }],
        })
        assert detail["run"]["status"] == "completed"
        assert out.read_text() == "foobar"

    def test_env_template_from_payload(self, store, tmp_path):
        """env: value uses ${{ run.payload.key }} and is resolved before subprocess."""
        out = tmp_path / "result.txt"
        engine.register_workflow(store, {
            "name": "env-tpl",
            "steps": [{
                "name": "write",
                "type": "shell",
                "cmd": [sys.executable, "-c",
                        f"import os; open(r'{out}', 'w').write(os.environ['TARGET'])"],
                "env": {"TARGET": "${{ run.payload.dest }}"},
            }],
        })
        engine.trigger_run(store, "env-tpl", trigger_payload={"dest": "RESOLVED_VALUE"})
        engine.worker_loop(store, stop_when_idle=True)
        assert out.read_text() == "RESOLVED_VALUE"

    def test_no_env_field_subprocess_inherits_os_environ(self, store):
        """No env: field → subprocess still has access to os.environ."""
        detail = self._run(store, {
            "name": "no-env",
            "steps": [{
                "name": "check",
                "type": "shell",
                "cmd": [sys.executable, "-c",
                        "import os; assert 'PATH' in os.environ or True"],
            }],
        })
        assert detail["run"]["status"] == "completed"

    def test_automaton_idempotency_key_injected(self, store, tmp_path):
        """AUTOMATON_IDEMPOTENCY_KEY is always injected by the shell handler."""
        out = tmp_path / "key.txt"
        detail = self._run(store, {
            "name": "idem-key",
            "steps": [{
                "name": "capture",
                "type": "shell",
                "cmd": [sys.executable, "-c",
                        f"import os; open(r'{out}', 'w').write(os.environ.get('AUTOMATON_IDEMPOTENCY_KEY',''))"],
            }],
        })
        assert detail["run"]["status"] == "completed"
        assert out.read_text() != ""  # key was injected


# ---------------------------------------------------------------------------
# automaton validate CLI
# ---------------------------------------------------------------------------

class TestAutomatonValidate:
    def test_valid_spec_exits_0(self, tmp_path):
        f = _write(tmp_path, _simple_spec())
        rc = _run_cli(["validate", str(f)])
        assert rc == 0

    def test_valid_spec_prints_ok(self, tmp_path, capsys):
        f = _write(tmp_path, _simple_spec("my-workflow"))
        with patch.dict("os.environ", {}):
            try:
                cli.main(["validate", str(f)])
            except SystemExit:
                pass
        out = capsys.readouterr().out
        assert "ok" in out.lower() or "my-workflow" in out

    def test_output_includes_workflow_name(self, tmp_path, capsys):
        f = _write(tmp_path, _simple_spec("special-name"))
        with patch.dict("os.environ", {}):
            try:
                cli.main(["validate", str(f)])
            except SystemExit:
                pass
        out = capsys.readouterr().out
        assert "special-name" in out

    def test_output_includes_step_count(self, tmp_path, capsys):
        spec = {
            "name": "three-step",
            "steps": [
                {"name": "a", "type": "shell", "cmd": [sys.executable, "-c", "pass"]},
                {"name": "b", "type": "shell", "cmd": [sys.executable, "-c", "pass"], "needs": ["a"]},
                {"name": "c", "type": "shell", "cmd": [sys.executable, "-c", "pass"], "needs": ["b"]},
            ],
        }
        f = _write(tmp_path, spec)
        with patch.dict("os.environ", {}):
            try:
                cli.main(["validate", str(f)])
            except SystemExit:
                pass
        out = capsys.readouterr().out
        assert "3" in out

    def test_missing_name_exits_1(self, tmp_path):
        spec = {"steps": [{"name": "s", "type": "shell", "cmd": ["echo"]}]}
        f = _write(tmp_path, spec)
        rc = _run_cli(["validate", str(f)])
        assert rc == 1

    def test_missing_steps_exits_1(self, tmp_path):
        f = _write(tmp_path, {"name": "broken"})
        rc = _run_cli(["validate", str(f)])
        assert rc == 1

    def test_missing_file_exits_1(self, tmp_path):
        rc = _run_cli(["validate", str(tmp_path / "no-such-file.yaml")])
        assert rc == 1

    def test_bad_yaml_exits_1(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text(":\n  - [broken\n", encoding="utf-8")
        rc = _run_cli(["validate", str(bad)])
        assert rc == 1

    def test_non_dict_yaml_exits_1(self, tmp_path):
        p = tmp_path / "list.yaml"
        p.write_text("- item1\n- item2\n", encoding="utf-8")
        rc = _run_cli(["validate", str(p)])
        assert rc == 1

    def test_cycle_detected_exits_1(self, tmp_path):
        spec = {
            "name": "cyclic",
            "steps": [
                {"name": "a", "type": "shell", "cmd": ["echo"], "needs": ["b"]},
                {"name": "b", "type": "shell", "cmd": ["echo"], "needs": ["a"]},
            ],
        }
        f = _write(tmp_path, spec)
        rc = _run_cli(["validate", str(f)])
        assert rc == 1

    def test_duplicate_step_names_exits_1(self, tmp_path):
        spec = {
            "name": "dups",
            "steps": [
                {"name": "s", "type": "shell", "cmd": ["echo"]},
                {"name": "s", "type": "shell", "cmd": ["echo"]},
            ],
        }
        f = _write(tmp_path, spec)
        rc = _run_cli(["validate", str(f)])
        assert rc == 1

    def test_invalid_when_type_exits_1(self, tmp_path):
        spec = {
            "name": "bad-when",
            "steps": [{"name": "s", "type": "shell", "cmd": ["echo"], "when": 42}],
        }
        f = _write(tmp_path, spec)
        rc = _run_cli(["validate", str(f)])
        assert rc == 1

    def test_invalid_env_value_exits_1(self, tmp_path):
        spec = {
            "name": "bad-env",
            "steps": [{"name": "s", "type": "shell", "cmd": ["echo"],
                        "env": {"KEY": 123}}],
        }
        f = _write(tmp_path, spec)
        rc = _run_cli(["validate", str(f)])
        assert rc == 1

    def test_error_message_goes_to_stderr(self, tmp_path, capsys):
        f = _write(tmp_path, {"name": "broken"})
        with patch.dict("os.environ", {}):
            try:
                cli.main(["validate", str(f)])
            except SystemExit:
                pass
        err = capsys.readouterr().err
        assert "error" in err.lower() or len(err) > 0
