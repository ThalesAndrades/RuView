//! ADR-115 §3.12 — node→zone model for the zone-gated semantic primitives
//! (`bed_exit`, `bathroom_occupied`, `multi_room_transition`).
//!
//! Loaded from `--semantic-zones-file` (JSON). **Zone-centric** schema: each
//! zone lists the node ids that sense it plus optional tags.
//!
//! ```json
//! { "zones": [
//!     { "name": "bedroom",  "nodes": ["1"], "tags": ["bed"] },
//!     { "name": "bathroom", "nodes": ["2"], "tags": ["bathroom"] },
//!     { "name": "living",   "nodes": ["3"] }
//! ]}
//! ```
//!
//! Node ids are the ESP32 node id the firmware reports (the `node_id: u8`
//! carried per node), written as strings so the file stays operator-friendly.
//!
//! - `active_zones` is resolved **per tick** from which nodes currently report
//!   presence (see [`ZoneMap::active_zones`]).
//! - `bed_zones` is the static set of zones tagged `bed`
//!   (see [`ZoneMap::bed_zones`]) and drives the `bed_exit` primitive.
//! - The `bathroom` primitive matches a zone *named* after its configured tag
//!   (default `"bathroom"`), so naming the bathroom zone `"bathroom"` wires it
//!   with no extra config.

use std::path::Path;

use serde::Deserialize;

/// One named area and the nodes that sense it.
#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
pub struct ZoneSpec {
    /// Human/HA-facing zone name (e.g. `"bedroom"`). Also what the bathroom
    /// primitive matches against its `bathroom_zone_tag`.
    pub name: String,
    /// ESP32 node ids (stringified `node_id`) that cover this zone.
    #[serde(default)]
    pub nodes: Vec<String>,
    /// Free-form tags. `"bed"` (case-insensitive) marks a bed zone for
    /// `bed_exit`; others are reserved for future primitives.
    #[serde(default)]
    pub tags: Vec<String>,
}

/// Parsed `--semantic-zones-file`. `Default` (no zones) leaves the three
/// zone-gated primitives dormant — their documented behavior when zones are
/// unconfigured.
#[derive(Debug, Clone, Deserialize, Default, PartialEq, Eq)]
pub struct ZoneMap {
    #[serde(default)]
    pub zones: Vec<ZoneSpec>,
}

impl ZoneMap {
    /// Parse from a JSON string.
    pub fn from_json_str(s: &str) -> Result<Self, serde_json::Error> {
        serde_json::from_str(s)
    }

    /// Load + parse from a file path.
    pub fn from_file(path: &Path) -> Result<Self, Box<dyn std::error::Error>> {
        let raw = std::fs::read_to_string(path)?;
        Ok(Self::from_json_str(&raw)?)
    }

    /// Number of configured zones (operator-facing diagnostic).
    pub fn len(&self) -> usize {
        self.zones.len()
    }

    pub fn is_empty(&self) -> bool {
        self.zones.is_empty()
    }

    /// Names of zones any of whose nodes appear in `present` (stringified node
    /// ids that currently report presence). One entry per zone, config order.
    pub fn active_zones(&self, present: &[String]) -> Vec<String> {
        self.zones
            .iter()
            .filter(|z| z.nodes.iter().any(|n| present.iter().any(|p| p == n)))
            .map(|z| z.name.clone())
            .collect()
    }

    /// Names of zones tagged `bed` (case-insensitive). Static set the
    /// `bed_exit` primitive treats as bed areas.
    pub fn bed_zones(&self) -> Vec<String> {
        self.zones
            .iter()
            .filter(|z| z.tags.iter().any(|t| t.eq_ignore_ascii_case("bed")))
            .map(|z| z.name.clone())
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> ZoneMap {
        ZoneMap::from_json_str(
            r#"{
                "zones": [
                    { "name": "bedroom",  "nodes": ["1", "4"], "tags": ["bed"] },
                    { "name": "bathroom", "nodes": ["2"], "tags": ["bathroom"] },
                    { "name": "living",   "nodes": ["3"] }
                ]
            }"#,
        )
        .expect("sample parses")
    }

    #[test]
    fn parses_zone_centric_schema() {
        let zm = sample();
        assert_eq!(zm.len(), 3);
        assert_eq!(zm.zones[0].name, "bedroom");
        assert_eq!(zm.zones[0].nodes, vec!["1", "4"]);
        assert_eq!(zm.zones[0].tags, vec!["bed"]);
    }

    #[test]
    fn active_zones_resolve_from_present_nodes() {
        let zm = sample();
        // Node 1 present → bedroom only.
        assert_eq!(zm.active_zones(&["1".into()]), vec!["bedroom"]);
        // Node 2 present → bathroom.
        assert_eq!(zm.active_zones(&["2".into()]), vec!["bathroom"]);
        // A second bedroom node (4) still resolves to bedroom.
        assert_eq!(zm.active_zones(&["4".into()]), vec!["bedroom"]);
        // Two zones at once.
        assert_eq!(
            zm.active_zones(&["1".into(), "3".into()]),
            vec!["bedroom", "living"]
        );
        // No present nodes → no active zones.
        assert!(zm.active_zones(&[]).is_empty());
        // Unknown node id → nothing.
        assert!(zm.active_zones(&["99".into()]).is_empty());
    }

    #[test]
    fn bed_zones_are_the_bed_tagged_ones() {
        let zm = sample();
        assert_eq!(zm.bed_zones(), vec!["bedroom"]);
    }

    #[test]
    fn bed_tag_match_is_case_insensitive() {
        let zm = ZoneMap::from_json_str(
            r#"{ "zones": [ { "name": "br", "nodes": ["1"], "tags": ["BED"] } ] }"#,
        )
        .unwrap();
        assert_eq!(zm.bed_zones(), vec!["br"]);
    }

    #[test]
    fn empty_and_missing_fields_are_tolerated() {
        // Missing `zones` → empty map (dormant primitives).
        let zm = ZoneMap::from_json_str("{}").unwrap();
        assert!(zm.is_empty());
        // Zone with no nodes/tags is valid (just never active).
        let zm = ZoneMap::from_json_str(r#"{ "zones": [ { "name": "x" } ] }"#).unwrap();
        assert_eq!(zm.len(), 1);
        assert!(zm.active_zones(&["1".into()]).is_empty());
        assert!(zm.bed_zones().is_empty());
    }

    #[test]
    fn malformed_json_is_an_error() {
        assert!(ZoneMap::from_json_str("not json").is_err());
        assert!(ZoneMap::from_json_str(r#"{ "zones": [ { } ] }"#).is_err()); // missing name
    }
}
