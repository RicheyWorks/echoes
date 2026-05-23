# Quickstart

Five minutes from install to a running workflow.

## 1. Create a workflow file

```yaml
# hello.yaml
name: hello
steps:
  - name: greet
    type: file_append
    path: /tmp/automaton-hello.log
    text: "hello from automaton\n"

  - name: again
    type: file_append
    needs: [greet]
    path: /tmp/automaton-hello.log
    text: "step two ran after step one\n"
```

## 2. Register it

```bash
automaton register hello.yaml
```

## 3. Trigger a run

```bash
automaton trigger hello
```

## 4. Run the worker

```bash
automaton worker --stop-when-idle
```

The worker drains the queue and exits. You'll see log lines as each step executes.

## 5. Inspect the result

```bash
automaton inspect          # list recent runs
automaton inspect 1        # detail for run ID 1
cat /tmp/automaton-hello.log
```

## Keep it running

For a long-lived setup, run the worker, scheduler, and UI as persistent processes:

```bash
# Three separate terminals (or systemd units — see Deployment)
automaton worker
automaton scheduler
automaton serve
```

The web UI is at `http://localhost:8080` by default.

## Next step

[Configuration →](configuration.md)
