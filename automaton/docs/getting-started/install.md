# Install

## Requirements

- Python 3.10 or later
- Any OS: Linux, macOS, Windows

## From PyPI

```bash
pip install automaton-engine
```

This installs the `automaton` CLI and the engine library. Optional extras:

```bash
# TLS cert generation (automaton tls init)
pip install "automaton-engine[tls]"

# Encrypted keyring on headless Linux servers (no D-Bus)
pip install "automaton-engine[secrets-headless]"

# Both
pip install "automaton-engine[tls,secrets-headless]"
```

## From source

```bash
git clone https://github.com/RicheyWorks/echoes
cd echoes/automaton
pip install -e ".[tls,secrets-headless]"
```

## Verify

```bash
automaton --help
```

## Next step

[Quickstart →](quickstart.md)
