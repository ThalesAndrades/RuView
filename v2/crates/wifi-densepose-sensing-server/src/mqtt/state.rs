//! State payload encoding + rate limiting (ADR-115 §3.5 / §3.7).
//!
//! This module owns the translation from internal `sensing-server`
//! broadcast messages (`pose_data`, `edge_vitals`, `sensing_update`)
//! into the per-entity MQTT state-topic payloads consumed by Home
//! Assistant. It is gated behind the `mqtt` feature flag at the call
//! site, but the encoders and rate-limiter logic compile without any
//! network deps so they're testable under `--no-default-features`.
//!
//! Per ADR-115 §3.5, state-topic QoS / retain / cadence is:
//!
//! | Topic kind             | QoS | Retain | Cadence                |
//! |------------------------|-----|--------|------------------------|
//! | `sensor/*/state`       |  0  |   no   | rate-limited per §3.7  |
//! | `binary_sensor/*/state`|  1  |  yes   | on change only         |
//! | `event/*/state`        |  1  |   no   | on event               |
//! | `*/availability`       |  1  |  yes   | LWT + 30 s heartbeat   |
//!
//! Per ADR-115 §3.7, default rates are:
//!
//! - presence binary  : on change
//! - person count     : 1.0 Hz
//! - vitals (HR / BR) : 0.2 Hz (every 5 s)
//! - motion level     : 1.0 Hz
//! - fall events      : on event (no rate limit)
//! - RSSI             : 0.1 Hz
//! - pose             : 1.0 Hz when `--mqtt-publish-pose` (off by default)
//! - zones            : on change

use std::collections::HashMap;
use std::time::Duration;

use serde::Serialize;
use serde_json::Value;

use super::config::PublishRates;
use super::discovery::{DiscoveryComponent, EntityKind};

/// Encoded outbound MQTT publication. `topic` is fully-qualified
/// (already prefixed with the discovery namespace + node id). `payload`
/// is the UTF-8 string the broker should publish on that topic.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StateMessage {
    pub topic: String,
    pub payload: String,
    pub qos: u8,
    pub retain: bool,
}

impl StateMessage {
    pub fn new(topic: String, payload: String, component: DiscoveryComponent, is_change_only: bool) -> Self {
        let (qos, retain) = match component {
            DiscoveryComponent::BinarySensor => (1, is_change_only),
            DiscoveryComponent::Event => (1, false),
            DiscoveryComponent::Sensor => (0, false),
        };
        Self { topic, payload, qos, retain }
    }
}

/// Sample-rate-limit decisions, per entity. Tracks the last-emitted
/// instant per entity and gates further emissions accordingly. Time is
/// supplied by the caller so the limiter is testable without a clock.
#[derive(Debug, Default)]
pub struct RateLimiter {
    last: HashMap<EntityKind, Duration>,
}

impl RateLimiter {
    /// Build a fresh limiter with no per-entity history.
    pub fn new() -> Self {
        Self { last: HashMap::new() }
    }

    /// Decide whether a sample for `entity` is allowed to publish at
    /// `now`, given the configured `rates`. Returns true to publish
    /// (and updates last-emitted state); false to drop.
    pub fn allow(&mut self, entity: EntityKind, now: Duration, rates: &PublishRates) -> bool {
        let min_gap = match rate_hz_for(entity, rates) {
            // Zero / negative Hz → emit only on change (caller path).
            // Here we treat it as "always allow" because the caller is
            // already gating with change detection.
            rate if rate <= 0.0 => return true,
            rate => Duration::from_secs_f64(1.0 / rate),
        };
        match self.last.get(&entity) {
            Some(&prev) if now.saturating_sub(prev) < min_gap => false,
            _ => {
                self.last.insert(entity, now);
                true
            }
        }
    }

    /// Reset all per-entity history. Used after a reconnect so the first
    /// post-reconnect sample is emitted promptly.
    pub fn reset(&mut self) {
        self.last.clear();
    }
}

/// Look up the configured Hz for an entity. Numerical entities use the
/// `rates` struct; non-rate-limited entities (events / change-only)
/// return 0.0 to short-circuit limiting.
fn rate_hz_for(entity: EntityKind, rates: &PublishRates) -> f64 {
    match entity {
        // Change-only / event entities — caller drives them.
        EntityKind::Presence
        | EntityKind::ZoneOccupancy
        | EntityKind::FallDetected
        | EntityKind::BedExit
        | EntityKind::MultiRoomTransition
        | EntityKind::SomeoneSleeping
        | EntityKind::PossibleDistress
        | EntityKind::RoomActive
        | EntityKind::ElderlyInactivityAnomaly
        | EntityKind::MeetingInProgress
        | EntityKind::BathroomOccupied
        | EntityKind::NoMovement => 0.0,
        // Rate-limited measurements.
        EntityKind::PersonCount => rates.count_hz,
        EntityKind::BreathingRate | EntityKind::HeartRate => rates.vitals_hz,
        EntityKind::MotionLevel | EntityKind::MotionEnergy => rates.motion_hz,
        EntityKind::PresenceScore => rates.motion_hz,
        EntityKind::Rssi => rates.rssi_hz,
        EntityKind::PoseKeypoints => rates.pose_hz,
        EntityKind::FallRiskElevated => rates.motion_hz,
    }
}

// ─── Per-entity state payload encoders ───────────────────────────────────

/// Inputs the encoder accepts. The caller (publisher loop) projects the
/// internal server broadcast into this struct so the encoder never
/// touches the original `serde_json::Value`s directly. Avoids leaking
/// the server's internal schema into ADR-115's wire format.
#[derive(Debug, Clone, Default)]
pub struct VitalsSnapshot {
    pub node_id: String,
    pub timestamp_ms: i64,
    pub presence: bool,
    pub fall_detected: bool,
    pub motion: f64,             // 0.0–1.0
    pub motion_energy: f64,
    pub presence_score: f64,     // 0.0–1.0
    pub breathing_rate_bpm: Option<f64>,
    pub heartrate_bpm: Option<f64>,
    pub n_persons: u32,
    pub rssi_dbm: Option<f64>,
    pub vital_confidence: f64,   // 0.0–1.0
}

#[derive(Serialize, Debug)]
struct NumberWithConfidence {
    bpm: f64,
    confidence: f64,
    ts: String,
}

#[derive(Serialize, Debug)]
struct MotionStatePayload {
    level_pct: f64,
    ts: String,
}

#[derive(Serialize, Debug)]
struct EnergyStatePayload {
    energy: f64,
    ts: String,
}

#[derive(Serialize, Debug)]
struct CountStatePayload {
    n_persons: u32,
    ts: String,
}

#[derive(Serialize, Debug)]
struct PresenceScorePayload {
    score_pct: f64,
    ts: String,
}

#[derive(Serialize, Debug)]
struct RssiPayload {
    dbm: f64,
    ts: String,
}

#[derive(Serialize, Debug)]
struct FallEventPayload {
    event_type: &'static str,
    ts: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    confidence: Option<f64>,
}

/// Generic scalar sensor payload. Matches the `{{ value_json.score }}`
/// template the discovery builder advertises for score-style sensors
/// (e.g. `fall_risk_elevated`, 0–100).
#[derive(Serialize)]
struct ScoreStatePayload {
    score: f64,
    ts: String,
}

/// Encoder bundle that knows how to render each entity's state payload
/// from a [`VitalsSnapshot`]. Operates on an existing [`DiscoveryBuilder`]
/// so topics are guaranteed to match what was advertised at discovery
/// time.
pub struct StateEncoder<'a> {
    pub builder: &'a super::discovery::DiscoveryBuilder<'a>,
}

impl<'a> StateEncoder<'a> {
    /// Build the binary state ("ON"/"OFF") topic + payload for the given
    /// boolean entity.
    pub fn boolean(&self, entity: EntityKind, on: bool) -> Option<StateMessage> {
        if !matches!(entity.component(), DiscoveryComponent::BinarySensor) {
            return None;
        }
        let topic = format!(
            "{}/{}/wifi_densepose_{}/{}/state",
            self.builder.discovery_prefix,
            entity.component().as_str(),
            self.builder.node_id,
            entity.topic_slug(),
        );
        let payload = if on { "ON" } else { "OFF" }.to_string();
        Some(StateMessage::new(topic, payload, entity.component(), true))
    }

    /// Numeric/measurement state encoder.
    pub fn numeric(&self, entity: EntityKind, snap: &VitalsSnapshot) -> Option<StateMessage> {
        if !matches!(entity.component(), DiscoveryComponent::Sensor) {
            return None;
        }
        let ts = iso_ts(snap.timestamp_ms);
        let payload_value: Value = match entity {
            EntityKind::PersonCount => serde_json::to_value(CountStatePayload {
                n_persons: snap.n_persons,
                ts: ts.clone(),
            }).ok()?,
            EntityKind::BreathingRate => {
                let bpm = snap.breathing_rate_bpm?;
                serde_json::to_value(NumberWithConfidence {
                    bpm,
                    confidence: snap.vital_confidence,
                    ts: ts.clone(),
                }).ok()?
            }
            EntityKind::HeartRate => {
                let bpm = snap.heartrate_bpm?;
                serde_json::to_value(NumberWithConfidence {
                    bpm,
                    confidence: snap.vital_confidence,
                    ts: ts.clone(),
                }).ok()?
            }
            EntityKind::MotionLevel => serde_json::to_value(MotionStatePayload {
                level_pct: (snap.motion.clamp(0.0, 1.0)) * 100.0,
                ts: ts.clone(),
            }).ok()?,
            EntityKind::MotionEnergy => serde_json::to_value(EnergyStatePayload {
                energy: snap.motion_energy,
                ts: ts.clone(),
            }).ok()?,
            EntityKind::PresenceScore => serde_json::to_value(PresenceScorePayload {
                score_pct: snap.presence_score.clamp(0.0, 1.0) * 100.0,
                ts: ts.clone(),
            }).ok()?,
            EntityKind::Rssi => {
                let dbm = snap.rssi_dbm?;
                serde_json::to_value(RssiPayload { dbm, ts: ts.clone() }).ok()?
            }
            _ => return None,
        };
        let topic = format!(
            "{}/{}/wifi_densepose_{}/{}/state",
            self.builder.discovery_prefix,
            entity.component().as_str(),
            self.builder.node_id,
            entity.topic_slug(),
        );
        let payload = serde_json::to_string(&payload_value).ok()?;
        Some(StateMessage::new(topic, payload, DiscoveryComponent::Sensor, false))
    }

    /// One-shot event encoder. Used for fall, bed exit, multi-room
    /// transition.
    pub fn event(&self, entity: EntityKind, event_type: &'static str, ts_ms: i64, confidence: Option<f64>) -> Option<StateMessage> {
        if !matches!(entity.component(), DiscoveryComponent::Event) {
            return None;
        }
        let payload_json = FallEventPayload { event_type, ts: iso_ts(ts_ms), confidence };
        let payload = serde_json::to_string(&payload_json).ok()?;
        let topic = format!(
            "{}/{}/wifi_densepose_{}/{}/state",
            self.builder.discovery_prefix,
            entity.component().as_str(),
            self.builder.node_id,
            entity.topic_slug(),
        );
        Some(StateMessage::new(topic, payload, DiscoveryComponent::Event, false))
    }

    /// Generic scalar-sensor encoder. Renders `{ "score": <value>, "ts": … }`
    /// for any `Sensor`-component entity whose discovery template reads
    /// `value_json.score` (the semantic `fall_risk_elevated` 0–100 score).
    pub fn scalar(&self, entity: EntityKind, value: f64, ts_ms: i64) -> Option<StateMessage> {
        if !matches!(entity.component(), DiscoveryComponent::Sensor) {
            return None;
        }
        let payload = serde_json::to_string(&ScoreStatePayload {
            score: value,
            ts: iso_ts(ts_ms),
        })
        .ok()?;
        let topic = format!(
            "{}/{}/wifi_densepose_{}/{}/state",
            self.builder.discovery_prefix,
            entity.component().as_str(),
            self.builder.node_id,
            entity.topic_slug(),
        );
        Some(StateMessage::new(topic, payload, DiscoveryComponent::Sensor, false))
    }
}

fn iso_ts(ms: i64) -> String {
    // Avoid pulling chrono into a hot path: format manually as ISO-8601
    // UTC. chrono is already in the crate's deps, but we keep this
    // encoder allocation-light for benchmark numbers.
    let secs = ms / 1000;
    let nanos = ((ms % 1000) * 1_000_000) as u32;
    let dt = chrono::DateTime::<chrono::Utc>::from_timestamp(secs, nanos)
        .unwrap_or_else(|| chrono::DateTime::<chrono::Utc>::from_timestamp(0, 0).unwrap());
    dt.to_rfc3339_opts(chrono::SecondsFormat::Millis, true)
}

#[cfg(test)]
#[path = "state_tests.rs"]
mod tests;
