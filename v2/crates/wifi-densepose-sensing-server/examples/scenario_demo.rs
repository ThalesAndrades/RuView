//! Deterministic, hardware-free demo of the ADR-115 §3.12 semantic primitives.
//!
//! Replays a scripted "night in an elderly resident's room" through the **real**
//! `SemanticBus` FSMs, fast-forwarding time via each snapshot's `since_start` and
//! wall-clock fields (the primitives key their dwell windows off those, so we can
//! compress a 60-minute inactivity gate into one print line). No server, no ports,
//! no ESP32 — runs in milliseconds.
//!
//! ```text
//! cargo run -p wifi-densepose-sensing-server --example scenario_demo --no-default-features
//! ```
//!
//! It shows the elderly-care safety arc the product exists for:
//!   room_active → someone_sleeping → no_movement → elderly_inactivity_anomaly
//!   → (resident gets up overnight) → bed_exit
//!
//! This is a *pipeline* demo on scripted inputs — it proves the inference layer
//! fires the right primitives in the right order, NOT real-world accuracy (that
//! needs field validation against ground truth).

use wifi_densepose_sensing_server::semantic::{
    PrimitiveConfig, PrimitiveState, RawSnapshot, SemanticBus, SemanticEvent, SemanticKind,
};

use std::time::Duration;

/// One scripted moment: a human-readable beat + the sensor state at that instant.
struct Beat {
    /// What's happening, for the narrative.
    narrative: &'static str,
    /// Wall clock shown to the reader (e.g. "22:30").
    clock: &'static str,
    /// Seconds since the server started (drives dwell windows).
    since_start_s: u64,
    /// Local time-of-day in seconds since midnight (drives the bed-exit window).
    tod_s: u32,
    presence: bool,
    motion: f64,
    breathing_bpm: Option<f64>,
    /// Zones currently reporting presence (which room the resident is in).
    active_zones: &'static [&'static str],
}

fn snapshot(b: &Beat) -> RawSnapshot {
    RawSnapshot {
        node_id: "demo".to_string(),
        since_start: Duration::from_secs(b.since_start_s),
        timestamp_ms: (b.since_start_s as i64) * 1000,
        presence: b.presence,
        motion: b.motion,
        breathing_rate_bpm: b.breathing_bpm,
        heart_rate_bpm: None,
        vital_confidence: 0.8,
        n_persons: if b.presence { 1 } else { 0 },
        active_zones: b.active_zones.iter().map(|s| s.to_string()).collect(),
        bed_zones: vec!["bedroom".to_string()], // the bed is in the bedroom zone
        local_seconds_since_midnight: b.tod_s,
        ..Default::default()
    }
}

/// Friendly name for an emitted primitive.
fn label(kind: SemanticKind) -> &'static str {
    match kind {
        SemanticKind::SomeoneSleeping => "someone_sleeping",
        SemanticKind::PossibleDistress => "possible_distress",
        SemanticKind::RoomActive => "room_active",
        SemanticKind::ElderlyAnomaly => "elderly_inactivity_anomaly",
        SemanticKind::Meeting => "meeting_in_progress",
        SemanticKind::BathroomOccupied => "bathroom_occupied",
        SemanticKind::FallRisk => "fall_risk_elevated",
        SemanticKind::BedExit => "bed_exit",
        SemanticKind::NoMovement => "no_movement",
        SemanticKind::MultiRoom => "multi_room_transition",
    }
}

/// Render one emitted event, or `None` to suppress it from the narrative
/// (fall_risk publishes a 0-score gauge every tick — pure noise here).
fn render(ev: &SemanticEvent) -> Option<String> {
    if matches!(ev.kind, SemanticKind::FallRisk) {
        return None;
    }
    let name = label(ev.kind);
    let body = match &ev.state {
        PrimitiveState::Boolean { active, reason, .. } => format!(
            "{:<28} {}   [{}]",
            name,
            if *active { "→ ON " } else { "→ off" },
            reason.tags.join(", ")
        ),
        PrimitiveState::Event { event_type, reason } => {
            format!("{:<28} ⚡ EVENT ({event_type})   [{}]", name, reason.tags.join(", "))
        }
        PrimitiveState::Scalar { value, reason } => {
            format!("{:<28} = {value:.0}   [{}]", name, reason.tags.join(", "))
        }
        PrimitiveState::Idle => return None,
    };
    Some(body)
}

fn main() {
    // A night in an ILPI resident's single room (bedroom zone holds the bed).
    let script = [
        Beat { narrative: "Resident is up and about in the bedroom", clock: "21:30",
            since_start_s: 120, tod_s: 21 * 3600 + 1800, presence: true, motion: 0.40,
            breathing_bpm: None, active_zones: &["bedroom"] },
        Beat { narrative: "Lies down in bed, breathing settles", clock: "22:30",
            since_start_s: 200, tod_s: 22 * 3600 + 1800, presence: true, motion: 0.005,
            breathing_bpm: Some(14.0), active_zones: &["bedroom"] },
        Beat { narrative: "Asleep for 5+ minutes", clock: "22:35",
            since_start_s: 520, tod_s: 22 * 3600 + 2100, presence: true, motion: 0.005,
            breathing_bpm: Some(13.0), active_zones: &["bedroom"] },
        Beat { narrative: "Room goes quiet (activity gate relaxes)", clock: "23:00",
            since_start_s: 820, tod_s: 23 * 3600, presence: true, motion: 0.005,
            breathing_bpm: Some(13.0), active_zones: &["bedroom"] },
        Beat { narrative: "Still, unmoving, for 30+ minutes", clock: "23:05",
            since_start_s: 2010, tod_s: 23 * 3600 + 300, presence: true, motion: 0.005,
            breathing_bpm: Some(13.0), active_zones: &["bedroom"] },
        Beat { narrative: "Inactivity now exceeds 2× the learned baseline", clock: "02:00",
            since_start_s: 3900, tod_s: 2 * 3600, presence: true, motion: 0.005,
            breathing_bpm: Some(13.0), active_zones: &["bedroom"] },
        Beat { narrative: "Resident gets up overnight and leaves the bed", clock: "03:00",
            since_start_s: 4000, tod_s: 3 * 3600, presence: true, motion: 0.30,
            breathing_bpm: None, active_zones: &["hall"] },
    ];

    let mut bus = SemanticBus::new(PrimitiveConfig::default());

    println!("\n  RuView — semantic primitives, scripted overnight scenario");
    println!("  (real FSMs, fast-forwarded time, no hardware)\n");
    println!("  ────────────────────────────────────────────────────────────────────");

    for beat in &script {
        let events = bus.tick(&snapshot(beat));
        let rendered: Vec<String> = events.iter().filter_map(render).collect();

        println!("  {} — {}", beat.clock, beat.narrative);
        if rendered.is_empty() {
            println!("           · (no state change)");
        } else {
            for line in rendered {
                println!("           · {line}");
            }
        }
    }

    println!("  ────────────────────────────────────────────────────────────────────");
    println!(
        "\n  These are the Home-Assistant entities a caregiver would see flip in order.\n"
    );
}
