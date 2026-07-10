# echoes (repository)

[![test](https://github.com/RicheyWorks/echoes/actions/workflows/test.yml/badge.svg)](https://github.com/RicheyWorks/echoes/actions/workflows/test.yml)
[![echoes](https://github.com/RicheyWorks/echoes/actions/workflows/echoes.yml/badge.svg)](https://github.com/RicheyWorks/echoes/actions/workflows/echoes.yml)
[![mobile](https://github.com/RicheyWorks/echoes/actions/workflows/mobile.yml/badge.svg)](https://github.com/RicheyWorks/echoes/actions/workflows/mobile.yml)
[![docs](https://github.com/RicheyWorks/echoes/actions/workflows/docs.yml/badge.svg)](https://github.com/RicheyWorks/echoes/actions/workflows/docs.yml)

Two related projects around one idea: **automation you can trust, with an
audit trail you can prove.**

## [`automaton/`](automaton/) — personal automation engine (Python)

Triggers fire workflows; workflows are a DAG of steps; every observable side
effect happens **exactly once**, even when workers crash mid-step. SQLite by
default, Postgres for multi-machine. Published on PyPI as
[`automaton-engine`](https://pypi.org/project/automaton-engine/).

- [README](automaton/README.md) · [CHANGELOG](automaton/CHANGELOG.md) · [Docs site](https://RicheyWorks.github.io/echoes/)
- Native clients: [iOS (SwiftUI)](automaton/deploy/ios/) · [Android (Compose)](automaton/deploy/android/)
- Deployment: [systemd](automaton/deploy/systemd/) · [launchd](automaton/deploy/macos/) · [Windows](automaton/deploy/windows/) · [Docker](automaton/Dockerfile) · [Tailscale mesh](automaton/deploy/mesh/)

## [`echoes-v1.0-forensic/`](echoes-v1.0-forensic/) — forensic agent framework (Rust)

An agent whose memory is a SHA-256 hash chain with a Merkle root — every
decision is tamper-evident, and integrity is verified on every load. Real
event sources (file watching, process scanning), SQLite persistence, and a
remote-store mode that uses `automaton` as its durable backend.

- Core crate: [`echoes-v1.0-forensic/echoes/`](echoes-v1.0-forensic/echoes/) (lib + `echoes` CLI)
- WASM bindings: [`echoes-v1.0-forensic/echoes-wasm/`](echoes-v1.0-forensic/echoes-wasm/) — the hash-chain/Merkle primitives for JS/TS, with a native parity guard against the core crate

## How they connect

`automaton` has a first-party `echoes_agent` step type: any workflow can run a
forensic agent for N ticks and record its Merkle root + integrity verdict in
automaton's linearizable event log — cryptographically attested audit trails
inside ordinary automation runs. In the other direction, `echoes run
--remote-store` persists agent memory to an automaton server across machines.

## Repository layout

| Path | What it is |
|---|---|
| `automaton/` | Python engine, tests, deploy assets, docs |
| `echoes-v1.0-forensic/` | Current Rust workspace (core + wasm) |
| `echoes-v0.8-enhanced/`, `echoes (1)/` | Earlier iterations kept for history |
| `.github/workflows/` | CI: `test` (3 OS × py3.10–3.12 + Postgres), `echoes` (fmt/clippy/test + wasm parity), `mobile` (iOS + Android builds), `docs`, `release` |
| `ADR-001-architecture-roadmap.md` | Architecture audit, phased roadmap, and running known-issues log |

## Development

```bash
# Python engine
cd automaton && pip install -e ".[tls,secrets-headless]" pytest && pytest tests/

# Rust core
cd echoes-v1.0-forensic/echoes && cargo test && cargo clippy --all-targets -- -D warnings

# WASM bindings (native parity tests + wasm build)
cd echoes-v1.0-forensic/echoes-wasm && cargo test && cargo build --target wasm32-unknown-unknown --release
```
