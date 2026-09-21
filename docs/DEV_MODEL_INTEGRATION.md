# Development model integration

This is a development migration checklist, not a production rollout or a live
acceptance result. Governor remains separate from Guard and from the TEE/TAIL
proxy. The r44 predecessor is now stopped and retained. The historical preflight and preparation steps below are superseded for current state by [live acceptance](validation/DEV_V0520_ACCEPTANCE.md).

## Verified starting point

The 2026-09-19 read-only preflight found the same r44 backend and adapter as the
r5 CPU validation. Effective server configuration is TP1/PP1/DP1, non-overlap,
no disaggregation, context length 262144, FP8 e4m3 KV, and EAGLE. Running/waiting,
KV/Mamba usage and tokenizer-owned requests were zero at that observation.
This observation does not substitute for three fresh drains immediately before
a later replacement.

The development launcher already reads TOKEN from the container environment,
assigns both native API and admin keys in process, and redacts their log values.
Keep that launcher; do not add credentials to command arguments or reports.
Its mounted bytes must be checked against the retained predecessor when
preparing recovery.

## Candidate startup

The staged next candidate is now `0.5.20+phala.govdev2`, installed under
`governor-v0520-launch-r3/site/` in the authorized CVM task directory. Its wheel
SHA256 is `64d282781e6d842c0b76a28a87d8cce24f119b588e7faf2f89638856c3a8fb93`.
The running backend is still govdev1. The new candidate adds only the validated
ownership observation and retraction queue-time telemetry delta; Governor core,
model configuration and GPU dependencies remain as in the accepted govdev1 run.

Use the tested prebuilt Rust library and Python package with:

- PIG_GOVERNOR_ENABLE=1
- PIG_GOVERNOR_LIBRARY set to the absolute mounted shared-library path
- PIG_TPS_REFERENCE=50
- Python import path containing the mounted plugin package

Remove only the obsolete native-QoS CLI options: tps-reference,
dev-queue-rounds, dev-prefill-progress, dev-phase-costs, dev-plan-observe,
dev-queue-plan-mode and dev-plan-horizon (all have the --native-qos- prefix).
Keep the actual model, revision, context, KV type, EAGLE and GPU settings.
Replace the old experimental source overlays with the explicit tested patch
payload. Do not leave the old scheduler, tokenizer or allocator overlays active.
Tokenizer and scheduler must both carry the correlated control-message patch.

## Proxy compatibility: CPU candidate complete, live acceptance pending

The existing Guard native mode polls /native_qos and requires a verified epoch
before forwarding generation. Governor deliberately has no such endpoint or
request-epoch admission fence. Reusing this mode would reject every generation
request with 503.

The existing Go cmd/phala-tail was adapted in its own source tree. The r2 source passed 23 top-level tests plus 4 subtests, race checks, vet and dependency checks excluding the old nativeqos/admission/server/request-classifier packages. Its archived static CPU-test build cannot provide native NVML. The current running CGO1 binary is `tail-governor-cgo-r1.bin`, SHA256 `fa8c8207fe60c15df00c6276af380479aa908f13991dec88dfe6a00c1b2b8fb2`. Actual TLS/NVML/DCAP/NRAS results and the strict dynamic-platform policy failure are recorded in the live acceptance checkpoint.

The development ingress retains transparent streaming forwarding, existing API
authentication, public route allowlist and attestation behavior. The admin
GET/PATCH /admin/v1/predictive-policy path should forward the plugin response,
without recreating the old native-QoS document or adding admission rules.

PATCH keeps expected_epoch, expected_revision and tps_reference. A 409 requires
a fresh read before the caller decides on a new update. A 503 may follow an
operation that is still in progress; read actual state before deciding whether
another update is necessary. Do not automatically retry uncertain writes.

The proxy must propagate disconnects to the upstream and preserve error codes.
Its own in-flight count is transport evidence, not scheduler/cache ownership.
Do not republish unavailable old native-QoS gauges as zeros.

## Model acceptance

Before replacement, save and download/hash the exact current recovery payload,
verify all candidate hashes, take three fresh drains, and retain the predecessor.
Keep the existing private network and loopback-only ingress.

After startup verify effective flags and plugin/library identity, authenticated
policy read, hot update and stale-CAS rejection, normal and streamed generation,
multimodal and structured/tool protocols, disconnect/cancellation and recovery.
Drain evidence must combine actual proxy in-flight requests, SGLang native
running/waiting and KV/Mamba usage, and Governor active Decode sequences.
Where tokenizer ownership is unavailable in the simplified server, add a
minimal generic lifecycle observation or report it as unverified; do not rebuild
the old experiment layer.

Compare matched workloads against the retained baseline: average Decode TPS,
completed throughput, TTFT/queue age, fairness, CPU usage, errors and OOM.
Average TPS stays a soft target. A low individual rate or long TTFT does not
create a new rejection rule.


Governor runtime archive r5: f06e2f0839fc2887f07c1a80812f0ece5dfe722aedb8a1b5886a1966953cd96d. All 26 payload hashes were verified after download. The earlier exact r44 recovery archive was downloaded and its 66 files verified before the completed govdev1 transition. A govdev2 replacement requires fresh recovery of the currently running govdev1 chain; the historical r44 recovery is not a substitute. Keep the unchanged TAIL running if only the backend wheel is replaced, and verify upstream connectivity after the new backend becomes ready.
