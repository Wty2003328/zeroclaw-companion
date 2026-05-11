//! Shared building blocks for the zeroclaw companion.
//!
//! - [`zeroclaw`]: REST + SSE client for the upstream zeroclaw daemon
//! - [`llm`]: OpenAI-compatible chat client (works with OpenAI, OpenRouter,
//!   Together, Ollama, vLLM, Groq, DeepInfra, and any compat endpoint)
//! - [`config`]: top-level companion config types

pub mod config;
pub mod llm;
pub mod zeroclaw;

pub use config::{
    AgentKind, AvatarOverride, CompanionConfig, RuntimeOverride, SubagentOverride,
    ZeroclawConfig, ZeroclawOverride, runtime_override_path,
};
pub use llm::{ChatMessage, LlmClient, LlmConfig, Role};
pub use zeroclaw::{AgentEvent, ZeroclawClient};
