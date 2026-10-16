//! Rust migration draft for MRRC Modern.
//!
//! This crate captures the core contracts that a full Rust port must preserve
//! before replacing the production Python/FastAPI server.

pub mod app;
pub mod audio;
pub mod backends;
pub mod compat;
pub mod config;
pub mod memory;
pub mod protocol;
pub mod safety;
pub mod scheduler;
pub mod scope;
pub mod service;
pub mod state;
pub mod transport;
pub mod web;
