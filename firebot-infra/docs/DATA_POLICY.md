# Data policy (as implemented and tested)

Principle from the dossier: store only what an engineering question needs, with access and retention controls.
Each rule below names the code that enforces it and the test that proves it (`tests/test_data_policy.py` unless noted).

| Rule | Enforced by | Test |
|---|---|---|
| The full recording stays on the robot computer and may contain detailed payloads (it is the black box). | AKSHARA writes locally; nothing sends it anywhere | `test_the_raw_recording_stays_local_and_complete` |
| Only the owner can read recordings (folder 0700, files 0600 on POSIX). | `Recorder._private` | `test_recordings_are_owner_only` |
| High-rate raw trace data is not shipped to analytics; only selected events are. | the application's `tee` filter (see `firebot-app` runner) | app `test_run_is_recorded_replayable_and_shipped_to_analytics` |
| Sensitive keys never reach analytics, at any nesting depth: audio, voice, speech, transcript, text, message, image(s), camera_frame, frame_data, lat, lon, lng, latitude, longitude, gps, location, address. | `ananta.connector.scrub` + `DEFAULT_DENY` (extend per deployment) | `test_sensitive_fields_never_reach_analytics_even_when_nested` |
| Shipped metadata is small: at most 20 keys, strings cut at 200 characters, nesting limited. | `scrub` | `test_metadata_is_bounded` |
| Devices are identified by a keyed hash (HMAC-SHA-256 with a per-deployment salt), not by name. | `connector.pseudonymize` | `test_device_identity_is_pseudonymous_and_salt_dependent` |
| Analytics SQL access is read-only. | `SqliteAnalytics.query` uses SQLite's authorizer | `test_query_is_read_only` (test_analytics) |
| Old recordings are deleted on request; only genuine recordings are ever touched; dry run by default. | `akshara.prune_runs`, `firebot-infra prune-runs` | `RetentionTests` |
| Old analytics rows can be deleted. | `SqliteAnalytics.prune` | `test_analytics_retention` |

## What you still have to decide (the code cannot)
- The deny-list is a starting point: add every field name your application uses for private content.
- The per-deployment `salt` for device pseudonyms is a secret: keep it out of source control.
- Retention periods (suggested: recordings 30 days, analytics 180 days) and who may read the robot's disk.
- On filesystems without permission bits (some Windows/FAT setups) the 0600/0700 protection does not apply: secure the folder another way.
- Recordings are not encrypted at rest. Use disk encryption if the device can be lost.
