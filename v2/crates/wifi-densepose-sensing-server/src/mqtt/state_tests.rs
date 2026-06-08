//! Unit tests for `mqtt::state` encoders + rate limiter.
//!
//! Split out of `state.rs` to keep that file under the workspace's 500-line
//! rule (it's wired back as a child `mod tests` via `#[path]`, so `super::*`
//! still resolves to the `state` module's private items).

    use super::*;
    use crate::mqtt::discovery::DiscoveryBuilder;

    fn builder() -> DiscoveryBuilder<'static> {
        DiscoveryBuilder {
            discovery_prefix: "homeassistant",
            node_id: "aabbccddeeff",
            node_friendly_name: Some("Bedroom"),
            sw_version: "v0.7.0",
            model: "ESP32-S3 CSI node",
            via_device: None,
        }
    }

    fn rates() -> PublishRates {
        PublishRates {
            vitals_hz: 0.2,
            motion_hz: 1.0,
            count_hz: 1.0,
            rssi_hz: 0.1,
            pose_hz: 1.0,
        }
    }

    fn snap() -> VitalsSnapshot {
        VitalsSnapshot {
            node_id: "aabbccddeeff".into(),
            timestamp_ms: 1779_512_400_000,
            presence: true,
            fall_detected: false,
            motion: 0.35,
            motion_energy: 1234.5,
            presence_score: 0.91,
            breathing_rate_bpm: Some(14.2),
            heartrate_bpm: Some(68.2),
            n_persons: 1,
            rssi_dbm: Some(-52.0),
            vital_confidence: 0.87,
        }
    }

    // ─── Scalar encoder (semantic fall_risk score) ──────────────────

    #[test]
    fn scalar_encodes_fall_risk_score_on_sensor_topic() {
        let b = builder();
        let enc = StateEncoder { builder: &b };
        let m = enc
            .scalar(EntityKind::FallRiskElevated, 73.0, 1779_512_400_000)
            .expect("fall_risk is a Sensor entity");
        assert!(m.topic.ends_with("/fall_risk_elevated/state"));
        assert!(m.topic.contains("/sensor/"));
        let v: Value = serde_json::from_str(&m.payload).unwrap();
        assert_eq!(v["score"], 73.0);
        assert!(v.get("ts").is_some());
        // Sensor state: QoS 0, not retained (per §3.5).
        assert_eq!(m.qos, 0);
        assert!(!m.retain);
    }

    #[test]
    fn scalar_rejects_non_sensor_entities() {
        let b = builder();
        let enc = StateEncoder { builder: &b };
        // A binary_sensor semantic primitive must not be encoded as a scalar.
        assert!(enc.scalar(EntityKind::NoMovement, 1.0, 0).is_none());
    }

    // ─── Rate limiter ────────────────────────────────────────────────

    #[test]
    fn rate_limiter_first_sample_always_passes() {
        let mut rl = RateLimiter::new();
        assert!(rl.allow(EntityKind::HeartRate, Duration::ZERO, &rates()));
    }

    #[test]
    fn rate_limiter_drops_within_gap() {
        let mut rl = RateLimiter::new();
        let r = rates();
        // 0.2 Hz → 5 s gap.
        assert!(rl.allow(EntityKind::HeartRate, Duration::from_secs(0), &r));
        assert!(!rl.allow(EntityKind::HeartRate, Duration::from_secs(1), &r));
        assert!(!rl.allow(EntityKind::HeartRate, Duration::from_secs(4), &r));
    }

    #[test]
    fn rate_limiter_allows_after_gap() {
        let mut rl = RateLimiter::new();
        let r = rates();
        assert!(rl.allow(EntityKind::HeartRate, Duration::from_secs(0), &r));
        // 5 s gap met → allow.
        assert!(rl.allow(EntityKind::HeartRate, Duration::from_secs(5), &r));
    }

    #[test]
    fn rate_limiter_per_entity_independent() {
        let mut rl = RateLimiter::new();
        let r = rates();
        assert!(rl.allow(EntityKind::HeartRate, Duration::from_secs(0), &r));
        // Different entity, same instant → independent budget.
        assert!(rl.allow(EntityKind::MotionLevel, Duration::from_secs(0), &r));
    }

    #[test]
    fn rate_limiter_change_only_entities_always_allow() {
        let mut rl = RateLimiter::new();
        let r = rates();
        // Presence is change-only → rate=0 → unlimited; caller does change detection.
        for s in 0..3 {
            assert!(rl.allow(EntityKind::Presence, Duration::from_secs(s), &r));
        }
    }

    #[test]
    fn rate_limiter_reset_re_enables_immediate_publish() {
        let mut rl = RateLimiter::new();
        let r = rates();
        assert!(rl.allow(EntityKind::HeartRate, Duration::from_secs(0), &r));
        assert!(!rl.allow(EntityKind::HeartRate, Duration::from_secs(1), &r));
        rl.reset();
        // Post-reset: first sample passes.
        assert!(rl.allow(EntityKind::HeartRate, Duration::from_secs(1), &r));
    }

    // ─── Boolean / binary_sensor encoder ─────────────────────────────

    #[test]
    fn boolean_encoder_emits_on_off_payload() {
        let b = builder();
        let enc = StateEncoder { builder: &b };
        let on = enc.boolean(EntityKind::Presence, true).unwrap();
        assert_eq!(on.payload, "ON");
        assert_eq!(on.qos, 1);
        assert!(on.retain, "binary_sensor state must be retained per §3.5");
        let off = enc.boolean(EntityKind::Presence, false).unwrap();
        assert_eq!(off.payload, "OFF");
    }

    #[test]
    fn boolean_encoder_rejects_non_binary_entities() {
        let b = builder();
        let enc = StateEncoder { builder: &b };
        assert!(enc.boolean(EntityKind::HeartRate, true).is_none());
        assert!(enc.boolean(EntityKind::FallDetected, true).is_none());
    }

    #[test]
    fn boolean_topic_matches_discovery_state_topic() {
        let b = builder();
        let enc = StateEncoder { builder: &b };
        let msg = enc.boolean(EntityKind::Presence, true).unwrap();
        assert_eq!(
            msg.topic,
            "homeassistant/binary_sensor/wifi_densepose_aabbccddeeff/presence/state"
        );
    }

    // ─── Numeric / sensor encoder ────────────────────────────────────

    #[test]
    fn numeric_encoder_emits_bpm_payload_for_heart_rate() {
        let b = builder();
        let enc = StateEncoder { builder: &b };
        let s = snap();
        let msg = enc.numeric(EntityKind::HeartRate, &s).unwrap();
        let json: serde_json::Value = serde_json::from_str(&msg.payload).unwrap();
        assert_eq!(json["bpm"], 68.2);
        assert_eq!(json["confidence"], 0.87);
        assert_eq!(msg.qos, 0, "sensor state is QoS 0 per §3.5");
        assert!(!msg.retain);
    }

    #[test]
    fn numeric_encoder_emits_motion_percent_payload() {
        let b = builder();
        let enc = StateEncoder { builder: &b };
        let s = snap();
        let msg = enc.numeric(EntityKind::MotionLevel, &s).unwrap();
        let json: serde_json::Value = serde_json::from_str(&msg.payload).unwrap();
        // 0.35 → 35.0%
        assert_eq!(json["level_pct"], 35.0);
    }

    #[test]
    fn numeric_encoder_returns_none_when_optional_field_missing() {
        let b = builder();
        let enc = StateEncoder { builder: &b };
        let mut s = snap();
        s.heartrate_bpm = None;
        assert!(enc.numeric(EntityKind::HeartRate, &s).is_none());
    }

    #[test]
    fn numeric_encoder_clamps_out_of_range_motion() {
        let b = builder();
        let enc = StateEncoder { builder: &b };
        let mut s = snap();
        s.motion = 1.7; // pathological — clamp to 1.0 then ×100.
        let msg = enc.numeric(EntityKind::MotionLevel, &s).unwrap();
        let json: serde_json::Value = serde_json::from_str(&msg.payload).unwrap();
        assert_eq!(json["level_pct"], 100.0);
    }

    #[test]
    fn numeric_encoder_rejects_non_sensor_entities() {
        let b = builder();
        let enc = StateEncoder { builder: &b };
        let s = snap();
        assert!(enc.numeric(EntityKind::Presence, &s).is_none());
        assert!(enc.numeric(EntityKind::FallDetected, &s).is_none());
    }

    // ─── Event encoder ───────────────────────────────────────────────

    #[test]
    fn event_encoder_emits_fall_payload() {
        let b = builder();
        let enc = StateEncoder { builder: &b };
        let msg = enc
            .event(EntityKind::FallDetected, "fall_detected", 1779_512_400_000, Some(0.87))
            .unwrap();
        let json: serde_json::Value = serde_json::from_str(&msg.payload).unwrap();
        assert_eq!(json["event_type"], "fall_detected");
        assert_eq!(json["confidence"], 0.87);
        assert_eq!(msg.qos, 1);
        assert!(!msg.retain, "events must never be retained — HA would replay old falls");
    }

    #[test]
    fn event_encoder_omits_confidence_when_absent() {
        let b = builder();
        let enc = StateEncoder { builder: &b };
        let msg = enc
            .event(EntityKind::BedExit, "bed_exit", 1779_512_400_000, None)
            .unwrap();
        assert!(!msg.payload.contains("confidence"));
    }

    #[test]
    fn event_encoder_rejects_non_event_entities() {
        let b = builder();
        let enc = StateEncoder { builder: &b };
        assert!(enc.event(EntityKind::Presence, "x", 0, None).is_none());
        assert!(enc.event(EntityKind::HeartRate, "x", 0, None).is_none());
    }

    #[test]
    fn iso_ts_is_rfc3339_utc_with_millis() {
        let ts = iso_ts(1779_512_400_000);
        assert!(ts.ends_with("Z"));
        assert!(ts.contains("T"));
        // .000 suffix from `SecondsFormat::Millis`.
        assert!(ts.contains("."), "want millisecond fraction in: {}", ts);
    }
