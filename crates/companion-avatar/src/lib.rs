//! Live2D anime avatar for the zeroclaw companion.
//!
//! Architecture (port-style, model-agnostic TTS):
//!
//! ```text
//!   zeroclaw                              companion
//!   ────────                              ─────────
//!   /api/events  ──SSE──▶  AvatarRouter ──▶ Subagent (translate + emote)
//!                                             │
//!                                             ▼
//!                                          TTS port  (POST /tts)
//!                                             │
//!                                             ▼
//!                                          Live2D viewer  (WS /ws/avatar)
//! ```
//!
//! All extension points are runtime: no fork-side zeroclaw changes are
//! needed. The companion subscribes to upstream zeroclaw via its public
//! REST + SSE surface.

pub mod config;
pub mod expression;
pub mod lip_sync;
pub mod protocol;
pub mod subagent;
pub mod traits;
pub mod tts_server;
pub mod ws;

pub use config::AvatarConfig;
pub use expression::{ExpressionMapper, Live2DExpression};
pub use lip_sync::{LipSyncAnalyzer, LipSyncData, LipSyncFrame};
pub use protocol::{AvatarMessage, AvatarNotification, LipSyncDataProto, LipSyncFrameProto};
pub use subagent::{AvatarSubagent, SubagentAnalysis, SubagentMotion};
pub use tts_server::{AnimeTtsManager, AudioOutput};
pub use ws::{AvatarEvent, AvatarWsState, handle_ws_avatar, process_speak};
