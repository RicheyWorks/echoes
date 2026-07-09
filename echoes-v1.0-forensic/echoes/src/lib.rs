//! echoes — forensic agent core.
//!
//! Library crate exposing the hash-chain + Merkle-tree integrity primitives,
//! the SQLite/remote persistence layer, and the sensor event sources. The
//! `echoes` binary (`src/main.rs`) is a thin CLI over this library.

pub mod agent;
pub mod sensor;
pub mod store;
