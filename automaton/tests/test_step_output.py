"""Tests for Phase 18: step output capture.

Covers:
- python step type: execution, stdout capture, return value, error handling
- run_detail returns parsed output dicts (not raw JSON strings)
- shell step output keys in run_detail
- UI _render_step_output helper renders each step type correctly
"""
from __future__ import annotations

import sys
import textwrap
import types

import pytest

from automaton import db as _db
from automaton import engine
from automaton import steps as _steps
from automaton.ui import _render_step_output


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    conn = _db.connect(tmp_path / "test.db")
    _db.migrate(conn)
    return conn


def _run_workflow(store, spec):
    """Register, trigger, drain, and return run_detail."""
    engine.register_workflow(store, spec)
    run_id = engine.trigger_run(store, spec["name"])
    engine.worker_loop(store, stop_when_idle=True)
    return engine.run_detail(store, run_id)


def _make_module(name, source):
    """Compile source into a real module object and add it to sys.modules."""
    mod = types.ModuleType(name)
    exec(compile(source, f"<{name}>", "exec"), mod.__dict__)
    sys.modules[name] = mod
    return mod


# ---------------------------------------------------------------------------
# python step type — execution
# ---------------------------------------------------------------------------

class TestPythonStep:
    def test_python_step_runs_function_and_captures_return_value(self, store, tmp_path):
        """python step executes a function and stores its return value."""
        _make_module("_automaton_test_add", "def add(a, b): return a + b")
        detail = _run_workflow(store, {
            "name": "py_return",
            "steps": [{
                "name": "add",
                "type": "python",
                "module": "_automaton_test_add",
                "function": "add",
                "kwargs": {"a": 3, "b": 4},
            }],
        })
        assert detail["run"]["status"] == "completed"
        out = detail["steps"][0]["output"]
        assert isinstance(out, dict)
        assert out["return_value"] == 7

    def test_python_step_captures_stdout(self, store):
        """print() calls inside the function appear in output["stdout"]."""
        _make_module("_automaton_test_print", textwrap.dedent("""
            def greet(name):
                print(f"Hello, {name}!")
                return None
        """))
        detail = _run_workflow(store, {
            "name": "py_stdout",
            "steps": [{
                "name": "greet",
                "type": "python",
                "module": "_automaton_test_print",
                "function": "greet",
                "kwargs": {"name": "automaton"},
            }],
        })
        assert detail["run"]["status"] == "completed"
        out = detail["steps"][0]["output"]
        assert "Hello, automaton!" in out["stdout"]

    def test_python_step_captures_stderr(self, store):
        """stderr output (print to sys.stderr) is captured in output["stderr"]."""
        import sys as _sys
        _make_module("_automaton_test_stderr", textwrap.dedent("""
            import sys
            def warn():
                print("warning!", file=sys.stderr)
                return "ok"
        """))
        detail = _run_workflow(store, {
            "name": "py_stderr",
            "steps": [{
                "name": "warn",
                "type": "python",
                "module": "_automaton_test_stderr",
                "function": "warn",
            }],
        })
        assert detail["run"]["status"] == "completed"
        out = detail["steps"][0]["output"]
        assert "warning!" in out.get("stderr", "")

    def test_python_step_no_stdout_key_empty(self, store):
        """stdout is an empty string (not absent) when nothing is printed."""
        _make_module("_automaton_test_silent", "def silent(): return 42")
        detail = _run_workflow(store, {
            "name": "py_silent",
            "steps": [{
                "name": "silent",
                "type": "python",
                "module": "_automaton_test_silent",
                "function": "silent",
            }],
        })
        assert detail["run"]["status"] == "completed"
        out = detail["steps"][0]["output"]
        assert "stdout" in out
        assert out["stdout"] == ""
        assert out["return_value"] == 42

    def test_python_step_missing_module_fails(self, store):
        """ImportError on a missing module produces a failed step."""
        detail = _run_workflow(store, {
            "name": "py_bad_module",
            "steps": [{
                "name": "bad",
                "type": "python",
                "module": "_automaton_nonexistent_xyz",
                "function": "anything",
            }],
        })
        assert detail["run"]["status"] == "failed"
        step = detail["steps"][0]
        assert step["status"] == "failed"
        err = step.get("error") or {}
        msg = err.get("message", "") if isinstance(err, dict) else str(err)
        assert "_automaton_nonexistent_xyz" in msg

    def test_python_step_missing_function_fails(self, store):
        """AttributeError when function doesn't exist in the module."""
        _make_module("_automaton_test_no_fn", "x = 1")
        detail = _run_workflow(store, {
            "name": "py_bad_fn",
            "steps": [{
                "name": "bad",
                "type": "python",
                "module": "_automaton_test_no_fn",
                "function": "does_not_exist",
            }],
        })
        assert detail["run"]["status"] == "failed"
        err = detail["steps"][0].get("error") or {}
        msg = err.get("message", "") if isinstance(err, dict) else str(err)
        assert "does_not_exist" in msg

    def test_python_step_function_exception_fails(self, store):
        """Exception inside the function is caught and becomes a failed step."""
        _make_module("_automaton_test_explode", "def boom(): raise ValueError('kaboom')")
        detail = _run_workflow(store, {
            "name": "py_explode",
            "steps": [{
                "name": "boom",
                "type": "python",
                "module": "_automaton_test_explode",
                "function": "boom",
            }],
        })
        assert detail["run"]["status"] == "failed"
        err = detail["steps"][0].get("error") or {}
        msg = err.get("message", "") if isinstance(err, dict) else str(err)
        assert "kaboom" in msg

    def test_python_step_missing_module_field_fails(self, store):
        """Missing 'module' field raises StepError before import."""
        with pytest.raises(Exception):
            _steps.run_step({"type": "python", "function": "f"}, "ikey")

    def test_python_step_missing_function_field_fails(self, store):
        """Missing 'function' field raises StepError before import."""
        with pytest.raises(Exception):
            _steps.run_step({"type": "python", "module": "os"}, "ikey")

    def test_python_step_non_serialisable_return_value_becomes_repr(self, store):
        """If the return value can't be JSON-serialised, repr() is used."""
        _make_module("_automaton_test_unser", textwrap.dedent("""
            class MyObj:
                pass
            def get_obj():
                return MyObj()
        """))
        detail = _run_workflow(store, {
            "name": "py_unser",
            "steps": [{
                "name": "get",
                "type": "python",
                "module": "_automaton_test_unser",
                "function": "get_obj",
            }],
        })
        assert detail["run"]["status"] == "completed"
        out = detail["steps"][0]["output"]
        # repr() of an object contains its class name
        assert "MyObj" in str(out["return_value"])

    def test_python_is_registered_step_type(self):
        """'python' appears in the registered step types list."""
        assert "python" in _steps.registered_types()


# ---------------------------------------------------------------------------
# run_detail returns parsed output (not raw JSON strings)
# ---------------------------------------------------------------------------

class TestRunDetailOutputParsed:
    def test_shell_output_is_dict(self, store):
        detail = _run_workflow(store, {
            "name": "shell_out_dict",
            "steps": [{
                "name": "echo",
                "type": "shell",
                "cmd": [sys.executable, "-c", "print('hi')"],
            }],
        })
        out = detail["steps"][0]["output"]
        assert isinstance(out, dict), f"expected dict, got {type(out)}"
        assert "stdout" in out
        assert "returncode" in out
        # Must NOT have the raw key
        assert "output_json" not in detail["steps"][0]

    def test_file_append_output_is_dict(self, store, tmp_path):
        target = tmp_path / "out.log"
        detail = _run_workflow(store, {
            "name": "fa_out_dict",
            "steps": [{
                "name": "append",
                "type": "file_append",
                "path": str(target),
                "text": "hello",
            }],
        })
        out = detail["steps"][0]["output"]
        assert isinstance(out, dict)
        assert "appended" in out

    def test_no_output_step_has_none(self, store, tmp_path):
        """A step with no output key in the DB returns None (not a raw string)."""
        detail = _run_workflow(store, {
            "name": "signal_out",
            "steps": [
                {
                    "name": "append",
                    "type": "file_append",
                    "path": str(tmp_path / "x.log"),
                    "text": "x",
                }
            ],
        })
        # file_append has output, but it should be a dict not a string
        assert not isinstance(detail["steps"][0]["output"], str)

    def test_error_json_is_not_present(self, store):
        """The old error_json key should be absent from run_detail steps."""
        detail = _run_workflow(store, {
            "name": "no_old_keys",
            "steps": [{
                "name": "run",
                "type": "shell",
                "cmd": [sys.executable, "-c", "pass"],
            }],
        })
        step = detail["steps"][0]
        assert "output_json" not in step
        assert "error_json" not in step
        assert "output" in step
        assert "error" in step


# ---------------------------------------------------------------------------
# _render_step_output UI helper
# ---------------------------------------------------------------------------

class TestRenderStepOutput:
    def test_shell_success_shows_exit_badge_and_stdout(self):
        out = {"returncode": 0, "stdout": "hello world\n", "stderr": ""}
        html = _render_step_output(out, None)
        assert "exit" in html
        assert "hello world" in html

    def test_shell_failure_shows_nonzero_exit(self):
        out = {"returncode": 1, "stdout": "", "stderr": "boom"}
        html = _render_step_output(out, None)
        assert "1" in html        # exit code shown
        assert "boom" in html     # stderr shown

    def test_http_shows_status_code(self):
        out = {"status_code": 200, "body": "OK", "headers": {}}
        html = _render_step_output(out, None)
        assert "HTTP" in html
        assert "200" in html
        assert "OK" in html

    def test_python_shows_stdout_and_return_value(self):
        out = {"return_value": 42, "stdout": "printed\n"}
        html = _render_step_output(out, None)
        assert "printed" in html
        assert "42" in html

    def test_file_append_shows_written_badge(self):
        out = {"appended": True}
        html = _render_step_output(out, None)
        assert "written" in html

    def test_file_append_noop_shows_noop(self):
        out = {"appended": False, "reason": "idempotency_key already present"}
        html = _render_step_output(out, None)
        # Badge shows "no-op" indicator
        assert "no-op" in html

    def test_error_dict_message_shown(self):
        err = {"message": "something went wrong", "cmd": ["ls"]}
        html = _render_step_output(None, err)
        assert "something went wrong" in html

    def test_none_output_and_error_returns_empty(self):
        html = _render_step_output(None, None)
        assert html == ""

    def test_generic_dict_rendered_as_json(self):
        out = {"custom_key": "custom_value", "count": 3}
        html = _render_step_output(out, None)
        assert "custom_value" in html
