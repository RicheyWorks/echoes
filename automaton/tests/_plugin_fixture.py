"""Pretend external plugin. Importing this module registers `test_echo`.

In real life this code would live in a separately-installed package whose
pyproject.toml declares:

    [project.entry-points."automaton.step_types"]
    test_echo = "my_package.echo_step"
"""
from automaton.steps import step_type


@step_type("test_echo")
def echo(spec, idempotency_key):
    return {"echoed": spec.get("payload"), "key": idempotency_key}
