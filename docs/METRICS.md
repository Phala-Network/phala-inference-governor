# Native Governor metrics

Governor 0.2.8 / ABI4 provides `pig_governor.metrics.GovernorMetrics`.
The minimal SGLang hook initializes it after the native Governor, then publishes
on the existing approximately one-second active/idle scheduler accounting path.
`--enable-metrics` is required. SGLang's multiprocess registry owns exposition at
`/metrics`; the collection endpoint is TAIL's authenticated `/v1/metrics`.
TAIL forwards the backend response without adding owned metrics.

## Names and labels

All names below have prefix `pig_governor_`. Base labels reuse the scheduler's
fixed deployment labels. The exporter adds only `window` (`60s`, `2s`) and the
fixed `reason` vocabulary. Conflicting deployment labels named `reason` or
`window` are omitted from Governor metrics. It exports no request ID, prompt, runtime hash,
epoch, revision, filename, user label or per-cell series.

| Suffix | Type | Extra labels | Meaning |
| --- | --- | --- | --- |
| `available` | gauge | none | Telemetry implementation is installed: 1; older/missing package: 0. |
| `enabled` | gauge | none | This scheduler has native Governor admission enabled. |
| `state_valid` | gauge | none | The last telemetry read succeeded. Check sample age too. |
| `sample_timestamp_seconds` | gauge | none | Unix timestamp of the last successful read. |
| `tps_reference` | gauge | none | Configured soft per-sequence decode TPS target. Zero is explicit sampling mode. |
| `running_limit`, `waiting_limit`, `native_running_limit` | gauge | none | Live hard limits and resolved native capacity. |
| `outstanding_requests` | gauge | none | Live reservations including queued/prefill work. |
| `active_decode_sequences` | gauge | none | Live sequences with committed decode progress. |
| `waiting_requests` | gauge | none | Current native waiting + grammar + chunked-prefill owners. |
| `waiting_valid` | gauge | none | Current native waiting count is known. |
| `admission_attempts_total` | counter | none | Admission calls, including reuse. |
| `admission_rejects_total` | counter | none | Rejected admission calls. |
| `admission_reuses_total` | counter | none | Calls carrying an existing reservation. |
| `admission_rejections_total` | counter | `reason` | Rejections by bounded native reason. |
| `average_tps`, `average_tps_valid` | gauge | `window` | Rolling decode TPS and whether exposure/identity make it usable. |
| `decode_tokens_60s`, `decode_sequence_seconds_60s` | gauge | none | Rolling 60-second token and exposure evidence, not cumulative counters. |
| `evidence_observed` | gauge | none | Decode feedback has been observed during the current controller epoch. |
| `online_cells_qualified` | gauge | none | Fresh qualified live cells, independent of any optional profile. |
| `profile_cells_valid` | gauge | none | Unexpired optional offline prior cells, excluding an identity transition. |
| `coverage_cells_total` | gauge | none | Possible cells: resolved native running limit times four pressure classes. |
| `surface_ready` | gauge | none | At least one usable online/prior cell exists. |
| `identity_transition` | gauge | none | Admission is paused while the previous runtime identity drains. |
| `surface_epoch_rotations_total` | counter | none | Completed runtime identity rotations. |

`reason` has exactly nine values, preinitialized to zero: `fit`,
`reference_disabled`, `tps_risk`, `cold_prior`, `unknown`, `waiting_limit`,
`aggregate_tps_risk`, `online_exploration`, `identity_transition`. Normally allowed
reasons remain zero; their presence does not imply a rejection occurred.
No reason label is derived from user input. Prometheus may additionally expose
the standard counter `_created` samples in single-process mode.

## Missing and stale evidence

An unknown TPS is `NaN`, with corresponding `average_tps_valid=0`. A measured
zero TPS is `0` with validity 1. A cold controller has no qualified cells; online
cells expire under the core's 60-second freshness/exposure rules, even while
idle. Aggregate observation validity is separate from surface qualification.
`evidence_observed=1` alone does not establish fresh evidence.

The exporter supplies the live native owner count; it never substitutes
`last_waiting_count`. With no native sample, `waiting_requests` is `NaN` unless
all reservations have drained, in which case current waiting is known to be zero.

Disabled Governor has `available=1`, `enabled=0`, `state_valid=1`; policy/evidence
gauges are unavailable and validity/readiness flags are zero. An older package
without the exporter exposes `available=0` and its actual enablement, while
ordinary SGLang metrics remain scrapeable. A telemetry read error invalidates
gauges, preserves cumulative counters and the last successful timestamp, and
logs a fixed message without request/exception details.

Use `state_valid`, `enabled`, and the age of `sample_timestamp_seconds` together
to decide whether gauges are current. A blocked scheduler can leave its last
sample behind; this observer adds no timer thread or scrape-side policy calls.
`surface_ready=1` means some predictive evidence exists, not that all workloads
are covered, that admission will succeed, or that a TPS SLO is guaranteed.
Online and prior coverage can overlap; do not sum them as unique coverage.

Counter totals survive policy/reference updates and runtime epoch rotations.
They have normal process-lifetime/reset semantics; use Prometheus `rate`/`increase`.
The telemetry sample reads under the existing policy lock without refreshing
identity, making admission decisions, reserving resources or exporting a profile
document. GPU and final-image acceptance must use the frozen integrated image.
