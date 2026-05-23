"""Plugin discovery tests.

We don't want to install a fake package to test entry points - too much
machinery. Instead we monkey-patch entry_points() to return a synthetic
record that points at a module we ship for tests.

This proves the discovery path: a module registered via the entry-point
mechanism gets its @step_type decorators run, and the step is then
invokable through run_step.
"""
from __future__ import annotations

import importlib.metadata as md
from types import SimpleNamespace

import pytest

from automaton import steps


@pytest.fixture(autouse=True)
def reset_registry():
    """Reset the plugin-loaded flag and any test step types between tests."""
    steps._PLUGINS_LOADED = False
    # Remove any test-injected step types
    for k in list(steps._STEP_TYPES):
        if k.startswith("test_"):
            del steps._STEP_TYPES[k]
    yield
    steps._PLUGINS_LOADED = False
    for k in list(steps._STEP_TYPES):
        if k.startswith("test_"):
            del steps._STEP_TYPES[k]


def test_builtin_types_registered():
    assert "http_get" in steps.registered_types()
    assert "file_append" in steps.registered_types()


def test_plugin_loads_via_entry_point(monkeypatch):
    """A module pointed to by an entry point gets imported and its
    @step_type decoration takes effect."""
    # Build a fake EntryPoint object. The .load() method must import a
    # real module that calls @step_type at module scope. We use the
    # ready-made fixture module shipped in tests/_plugin_fixture.py.
    fake_ep = SimpleNamespace(
        name="test_plugin",
        value="tests._plugin_fixture",
        load=lambda: __import__("tests._plugin_fixture", fromlist=["*"]),
    )
    monkeypatch.setattr(steps, "entry_points",
                        lambda group=None: [fake_ep])
    types = steps.registered_types()
    assert "test_echo" in types

    # Invoke through run_step
    out = steps.run_step({"type": "test_echo", "payload": "hi"},
                        idempotency_key="k1")
    assert out == {"echoed": "hi", "key": "k1"}


def test_unknown_type_raises_with_known_types_listed():
    """run_step's error message includes what IS registered - useful
    feedback for agents/operators who typo a type name."""
    with pytest.raises(steps.StepError) as exc:
        steps.run_step({"type": "no_such_type"}, idempotency_key="k")
    msg = str(exc.value)
    assert "no_such_type" in msg
    assert "http_get" in msg  # known types are listed


def test_plugin_load_failure_does_not_crash(monkeypatch):
    """A broken plugin must not take down the whole worker."""
    def bad_load():
        raise ImportError("simulated plugin error")
    fake_ep = SimpleNamespace(name="broken", value="x.y", load=bad_load)
    monkeypatch.setattr(steps, "entry_points",
                        lambda group=None: [fake_ep])
    # Should not raise - built-ins still work.
    types = steps.registered_types()
    assert "http_get" in types
