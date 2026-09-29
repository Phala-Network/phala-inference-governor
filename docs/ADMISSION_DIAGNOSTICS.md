# Recovery and admission diagnostics

Version 0.2.10 retains C ABI v5. An observed slow cell may receive one recovery
exploration only when all Governor work has drained, health observations are
clean, the last decode observation is at least two seconds old, and existing
exploration/capacity/waiting limits permit it. Busy low-TPS decisions still
reject. The decision keeps the original low prediction and reports
`original_reason`, `original_evidence_source`, `recovery_probe=true` and final
`online_exploration`; it does not claim a predicted fit. Recovery samples must
qualify before subsequent requests become fits. Release and native rollback
reuse the existing reservation lifecycle and preserve exploration cooldown.

Diagnostic capture is disabled by default. Set `PIG_ADMISSION_DIAGNOSTICS=1`
before startup for an explicitly bounded experiment. Optional integer bounds:

| Variable suffix | Default | Maximum |
| --- | ---: | ---: |
| `_CAPACITY` | 128 | 512 |
| `_MAX_EVENTS` | 4096 | 65536 |
| `_SECONDS` | 1200 | 3600 |

Suffixes follow `PIG_ADMISSION_DIAGNOSTICS`; minimum is one. The existing
authenticated admission telemetry contains a `diagnostics` snapshot with
sequence-numbered decision events, overwrite count and capture-stop reason.
Capture begins at the first decision and does not restart after reaching its
time/event bound. Collectors must reconcile event coverage with admission
counters and disclose gaps or truncated capture. Disabled mode has no ring,
request hashing or serialization.

Events contain only bounded scalar prediction, policy and lifecycle fields.
Internal RIDs are HMACed with an unexported process-local key; no prompt,
generated text or credentials are included. This associates events within a
capture, not with client `X-Request-Id`. No per-HTTP-request receipt header or
new endpoint is provided. Do not infer a rejected request's prediction from a
later `last` field or claim client-level joins from these HMACs.

Deterministic real-core regressions cover idle recovery, sustained slowness,
busy refusal, concurrent single-probe ownership and rollback cooldown.
Capture on/off parity and bounded/privacy checks cover diagnostic behavior.
These checks establish component behavior; a specific final image still needs
GPU performance and production-chain acceptance.
