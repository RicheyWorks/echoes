"""Template-resolution tests.

Cover:
- run.id and run.payload references
- steps.<name>.output.<path> references
- whole-string template returns the typed value
- interpolation template returns a str
- missing path raises TemplateError
- a workflow chaining shell -> file_append actually passes data through
"""
from __future__ import annotations

from pathlib import Path

import pytest

from automaton import db as _db
from automaton import engine
from automaton import templating


@pytest.fixture
def store(tmp_path):
    conn = _db.connect(tmp_path / "test.db")
    _db.migrate(conn)
    return conn


def test_lookup_simple():
    ctx = {"run": {"id": 7, "payload": {"name": "alice"}}, "steps": {}}
    assert templating._lookup("run.id", ctx) == 7
    assert templating._lookup("run.payload.name", ctx) == "alice"


def test_lookup_missing_raises():
    ctx = {"run": {"id": 1}, "steps": {}}
    with pytest.raises(templating.TemplateError) as e:
        templating._lookup("run.nope", ctx)
    assert "nope" in str(e.value)


def test_lookup_list_index():
    ctx = {"steps": {"a": {"output": {"xs": [10, 20, 30]}}}}
    assert templating._lookup("steps.a.output.xs.1", ctx) == 20


def test_render_sole_template_preserves_type():
    ctx = {"run": {"id": 42}, "steps": {}}
    # Sole template returns int, not str
    assert templating.render("${{ run.id }}", ctx) == 42


def test_render_interpolation_stringifies():
    ctx = {"run": {"id": 42}, "steps": {}}
    assert templating.render("run-${{ run.id }}-done", ctx) == "run-42-done"


def test_render_recurses_into_dicts_and_lists():
    ctx = {"run": {"id": 5, "payload": {"name": "bob"}}, "steps": {}}
    spec = {
        "url": "https://x/${{ run.id }}",
        "headers": {"X-Name": "${{ run.payload.name }}"},
        "tags": ["${{ run.payload.name }}", "static"],
    }
    rendered = templating.render(spec, ctx)
    assert rendered == {
        "url": "https://x/5",
        "headers": {"X-Name": "bob"},
        "tags": ["bob", "static"],
    }


def test_no_templates_pass_through():
    ctx = {"run": {"id": 1}, "steps": {}}
    assert templating.render({"a": 1, "b": "no refs"}, ctx) == {"a": 1, "b": "no refs"}


def test_chain_first_step_output_into_second(store, tmp_path):
    """End-to-end: shell prints data, file_append uses it via templating."""
    target = tmp_path / "out.log"
    spec = {
        "name": "chain",
        "steps": [
            {
                "name": "get_value",
                "type": "shell",
                "cmd": ["sh", "-c", "echo 'agent-says-hello'"],
            },
            {
                "name": "write_it",
                "type": "file_append",
                "needs": ["get_value"],
                "path": str(target),
                # The shell step's stdout is in its output - reference it here.
                # Note: stdout includes a trailing newline; we accept that.
                "text": "got: ${{ steps.get_value.output.stdout }}",
            },
        ],
    }
    engine.register_workflow(store, spec)
    run_id = engine.trigger_run(store, "chain")
    engine.worker_loop(store, stop_when_idle=True)

    detail = engine.run_detail(store, run_id)
    assert detail["run"]["status"] == "completed"
    content = target.read_text()
    assert "got: agent-says-hello" in content


def test_unresolved_template_fails_step(store, tmp_path):
    """A template that points at a nonexistent path fails the step cleanly."""
    spec = {
        "name": "bad_ref",
        "steps": [{
            "name": "writer",
            "type": "file_append",
            "path": str(tmp_path / "wont_get_written"),
            "text": "ref: ${{ steps.never.output.foo }}",
        }],
    }
    engine.register_workflow(store, spec)
    run_id = engine.trigger_run(store, "bad_ref")
    engine.worker_loop(store, stop_when_idle=True)
    detail = engine.run_detail(store, run_id)
    assert detail["run"]["status"] == "failed"
    step = detail["steps"][0]
    assert step["status"] == "failed"
    assert "TemplateError" in (step["error_json"] or "")


def test_run_payload_visible(store, tmp_path):
    """trigger_payload is accessible via run.payload."""
    target = tmp_path / "out.log"
    spec = {
        "name": "p",
        "steps": [{
            "name": "writer",
            "type": "file_append",
            "path": str(target),
            "text": "hello ${{ run.payload.who }}",
        }],
    }
    engine.register_workflow(store, spec)
    engine.trigger_run(store, "p", trigger_payload={"who": "world"})
    engine.worker_loop(store, stop_when_idle=True)
    assert "hello world" in target.read_text()


def test_shell_step_runs_and_captures(store):
    """Shell step type smoke test - runs, captures stdout/stderr/returncode."""
    spec = {
        "name": "sh",
        "steps": [{
            "name": "echo",
            "type": "shell",
            "cmd": ["sh", "-c", "echo out; echo err >&2; exit 0"],
        }],
    }
    engine.register_workflow(store, spec)
    run_id = engine.trigger_run(store, "sh")
    engine.worker_loop(store, stop_when_idle=True)
    detail = engine.run_detail(store, run_id)
    assert detail["run"]["status"] == "completed"
    import json
    out = json.loads(detail["steps"][0]["output_json"])
    assert "out" in out["stdout"]
    assert "err" in out["stderr"]
    assert out["returncode"] == 0


def test_shell_nonzero_returncode_fails(store):
    spec = {
        "name": "shfail",
        "steps": [{
            "name": "boom",
            "type": "shell",
            "cmd": ["sh", "-c", "exit 7"],
        }],
    }
    engine.register_workflow(store, spec)
    engine.trigger_run(store, "shfail")
    engine.worker_loop(store, stop_when_idle=True)
    detail = engine.run_detail(store, "shfail" and 1)  # run_id=1
    assert detail["run"]["status"] == "failed"
