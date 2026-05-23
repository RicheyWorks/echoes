# Step types

Built-in step types ship with automaton-engine. Additional types can be added via the plugin system.

## Built-in types

| Type | Description |
|---|---|
| `shell` | Run a shell command via subprocess |
| `http_get` | Make an HTTP GET request |
| `file_append` | Append text to a file |
| `python` | Call a Python function |
| `wait_for_signal` | Park the run until an external signal arrives |

See [Workflow YAML reference → Step types](workflow-yaml.md#step-types) for full field documentation on each.

## Plugin step types

External packages can register new step types without modifying automaton itself. They declare an entry point in their own `pyproject.toml`:

```toml
[project.entry-points."automaton.step_types"]
my_step = "my_package.step_module"
```

The module must call `@step_type("my_step")` on the function that implements it:

```python
from automaton.steps import step_type

@step_type("my_step")
def execute(spec: dict, payload: dict, idempotency_key: str) -> str:
    """
    spec: the step definition from the workflow YAML
    payload: the run's trigger payload
    idempotency_key: unique key for this step attempt; use it to deduplicate
    Returns: a string to store as the step's output
    """
    ...
```

Install the plugin package alongside automaton-engine:

```bash
pip install automaton-engine my-step-plugin
```

The worker discovers it automatically at startup via `importlib.metadata.entry_points`.

## Listing registered types

```bash
automaton inspect --step-types
# or via the API:
curl http://localhost:8080/api/step_types
```
