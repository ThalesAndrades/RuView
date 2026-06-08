//! Cross-restart persistence for the semantic layer's *learned* baselines.
//!
//! Most semantic primitives are pure per-tick FSMs (session-relative timers
//! that correctly reset on restart). Two carry **learned, long-lived**
//! values that must survive a reboot, or the safety gates mis-fire for
//! minutes-to-hours after every restart:
//!
//! - `ElderlyInactivityAnomaly::longest_idle` — the inactivity baseline the
//!   anomaly multiplier is applied against. A reboot resets it to the 30-min
//!   floor, dropping the firing threshold and spamming false anomalies (or,
//!   if it had learned a long quiet baseline, losing the protection).
//! - `PossibleDistress` rolling HR baseline — the resting heart-rate
//!   reference the `>1.5×` distress test compares against.
//!
//! Only those two scalars are stored (never the session-relative timers), so
//! restoring into a fresh bus can't corrupt uptime-relative state. The store
//! is a single human-inspectable JSON file under the server's data dir.

use std::path::Path;

use serde::{Deserialize, Serialize};

/// On-disk snapshot of the semantic layer's learned baselines.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct SemanticSnapshot {
    /// `ElderlyInactivityAnomaly` inactivity baseline, in seconds.
    #[serde(default)]
    pub elderly_longest_idle_secs: f64,
    /// `PossibleDistress` rolling heart-rate baseline (BPM), if learned.
    #[serde(default)]
    pub distress_hr_baseline: Option<f64>,
}

/// File name under the data dir.
const FILE_NAME: &str = "semantic_baselines.json";

/// Persist the snapshot to `<data_dir>/semantic_baselines.json` (pretty JSON).
/// Creates the data dir if needed.
pub fn save(data_dir: &Path, snap: &SemanticSnapshot) -> Result<(), Box<dyn std::error::Error>> {
    std::fs::create_dir_all(data_dir)?;
    let json = serde_json::to_string_pretty(snap)?;
    std::fs::write(data_dir.join(FILE_NAME), json)?;
    Ok(())
}

/// Load the snapshot. Returns `Err` on first run (no file) — callers treat
/// that as "start from a cold baseline".
pub fn load(data_dir: &Path) -> Result<SemanticSnapshot, Box<dyn std::error::Error>> {
    let json = std::fs::read_to_string(data_dir.join(FILE_NAME))?;
    Ok(serde_json::from_str(&json)?)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips_through_disk() {
        let dir = std::env::temp_dir().join(format!("ruview-sem-{}", std::process::id()));
        let snap = SemanticSnapshot {
            elderly_longest_idle_secs: 5400.0,
            distress_hr_baseline: Some(68.0),
        };
        save(&dir, &snap).unwrap();
        let back = load(&dir).unwrap();
        assert_eq!(snap, back);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn load_is_err_when_absent() {
        let dir = std::env::temp_dir().join(format!("ruview-sem-missing-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        assert!(load(&dir).is_err());
    }

    #[test]
    fn tolerates_partial_json() {
        // Missing fields default (forward/backward compatibility).
        let snap: SemanticSnapshot = serde_json::from_str("{}").unwrap();
        assert_eq!(snap.elderly_longest_idle_secs, 0.0);
        assert_eq!(snap.distress_hr_baseline, None);
    }
}
