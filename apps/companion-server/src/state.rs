//! Top-level axum state for the companion server.
//!
//! Kept thin — the avatar pipeline owns its own `Arc<AvatarWsState>` and is
//! mounted via `Router::with_state` directly on the WS route.

use std::path::PathBuf;
use std::sync::Arc;

use companion_avatar::AvatarWsState;
use companion_core::ZeroclawClient;
use companion_pulse::PulseSubsystem;

#[derive(Clone)]
pub struct AppState {
    pub avatar: Option<Arc<AvatarWsState>>,
    pub pulse: Option<Arc<PulseSubsystem>>,
    pub zeroclaw: Arc<ZeroclawClient>,
    /// Path to the loaded `companion.toml`. Used to resolve where the
    /// runtime override file (`companion.runtime.json`) should be written
    /// when the UI saves subagent settings.
    pub config_path: PathBuf,
}
