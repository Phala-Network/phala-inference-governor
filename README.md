# Phala Inference Governor

PIG Governor is an independent Rust controller and thin Python adapter for
SGLang. It uses actual Decode token progress and sequence time to provide bounded
scheduling advice around a soft average-TPS reference. Native admission also
enforces mutable logical `max_running` and hard post-admit `max_waiting` bounds;
it does not impose a per-request TPS deadline or a TTFT deadline.

This project is separate from [Phala Inference Guard](https://github.com/Phala-Network/phala-inference-guard)
and [PIG TAIL](https://github.com/Phala-Network/pig-tail). TAIL owns transport and
attestation; SGLang retains request, KV/cache, worker synchronization and cleanup
ownership. Governor does not replace those mechanisms.

## Integration

The current source candidate targets official SGLang v0.5.20, commit
`94602c9c2b7cbdb8efd5c52802dac6a1c180089e`.
Use the complete shared engine source in
[Phala-Network/sglang](https://github.com/Phala-Network/sglang), including general
lifecycle, auth/diagnostic, worker, schema and model compatibility repairs.
Governor owns its component and [minimal integration hooks](patches/sglang/v0.5.20/README.md).
[sglang-serving-patches](https://github.com/Phala-Network/sglang-serving-patches)
maintains the single ordered patch stack for general and model-specific serving
changes. Governor publishes its component source and minimal hook patch. The
ordered stack and Governor input are composed on the fixed upstream baseline in
[Phala-Network/sglang](https://github.com/Phala-Network/sglang) as one complete
engine source commit and tree. Model-specific code activates only in its relevant
module, model or configuration; do not create a complete patch system per model
or require one PR per patch. Preserve frozen historical inputs and evidence while
integrating new changes in the shared source. Deployment configuration in
phala-models-compose consumes fixed complete-source and image identities; it does
not replace the source composition steps. Combined runtime images are published
to `ghcr.io/phala-network/sglang`, associated with
[Phala-Network/sglang](https://github.com/Phala-Network/sglang); this repository
publishes the Governor component and hooks, not SGLang runtime images.

The initial adapter supports **generation models, TP1/PP1/DP1, non-overlap
scheduling, no PD disaggregation**, with ordinary text/images and radix cache
enabled. Unsupported topology or a non-generation model fails during opt-in
initialization. Beam search is rejected while Governor is enabled because one
beam request expands into multiple native Decode rows that are not represented by
the current response surface. Broader topology and radix-disabled `input_embeds`
remain unqualified.

Build the Rust cdylib and install the Python package into the runtime image.
Component `0.2.1` uses C ABI v4. Production runtime configuration binds a
frozen response-surface profile to the exact composed engine, Governor source,
model artifact, hardware class and resolved SGLang settings:

```text
PIG_GOVERNOR_ENABLE=1
PIG_GOVERNOR_LIBRARY=/absolute/image/path/libpig_governor_core.so
PIG_TPS_REFERENCE=50
PIG_MAX_RUNNING=43
PIG_MAX_WAITING=3
PIG_TPS_PROFILE_PATH=/absolute/image/path/qwen3.8-27b-profile.json
PIG_TPS_PROFILE_SHA256=<64 lowercase hex characters>
PIG_ENGINE_COMMIT=<40 lowercase hex characters>
PIG_GOVERNOR_COMMIT=<40 lowercase hex characters>
PIG_MODEL_ARTIFACT_ID=sha256:<64 lowercase hex characters>
PIG_RUNTIME_HARDWARE_ID=h200-sxm-tp1-v1
```

With a positive reference, both profile variables are required and the profile
must be unexpired, identity-matched and cover every reachable response-surface
cell through exact or jointly-heavier evidence. `PIG_TPS_REFERENCE=0` is the
explicit offline sampling mode and may start without a profile. Runtime/model
identity changes clear prior and live evidence, rotate the CAS epoch and fail
closed until fresh qualified evidence exists.

Production uses the official `sglang serve` command. With the supplied auth patch,
`PIG_AUTH_FROM_TOKEN=1` reads the existing deployment `TOKEN` for both API and
admin authentication. Do not put credentials in argv. Missing/invalid TOKEN or
any simultaneous explicit API/admin key fails startup. Diagnostic serialization
redacts keys without altering runtime configuration or internal IPC.

## External policy API

Authenticated `GET/PATCH /admin/v1/predictive-policy` reads and atomically updates
the mutable `tps_reference`, `max_running` and `max_waiting` policy. PATCH requires
`expected_epoch` and `expected_revision` for CAS and accepts any non-empty subset
of those three fields. `max_running` must remain within the immutable native
SGLang runnable bound, and `max_waiting` cannot exceed the hard limit of `3`.
A successful hot update neither restarts the model nor resets actual history.
`tps_reference=0` is the explicit C2 offline sampling mode; a CAS update from 0
to the production reference preserves the learned response surface.

Authenticated `GET /admin/v1/predictive-profile?expected_epoch=<epoch>` exports
one epoch-guarded, non-cacheable envelope containing the exact runtime identity,
coverage report and a loadable profile document. Persist the nested `profile`
document as canonical JSON, review it, hash the exact bytes and configure that
path/hash for the production restart. A stale epoch returns 409; malformed or
multi-rank state fails closed with 503.

Production launchers should suppress the polling endpoints from Uvicorn access
logs together with metrics:

```text
--uvicorn-access-log-exclude-prefixes /metrics /admin/v1/predictive-policy /admin/v1/predictive-profile
```

Admission is TPS-first and occurs before SGLang's grammar or ordinary waiting
queue. It forecasts the candidate's projected `(Decode concurrency, context
pressure)` cell from measured per-user Decode evidence, and returns HTTP 429 when
that cell is unqualified or falls below the reference. A TPS-fit candidate is
then rejected with a distinct waiting-limit reason when
`max(0, admitted_nonterminal + 1 - max_running)` exceeds `max_waiting`. Native
ordinary/grammar/chunked waiting counts are retained as telemetry and do not
replace the reservation ledger. The production defaults are `50 / 43 / 3`.
`tps_reference`, `max_running` and `max_waiting` remain hot-updatable through the
same CAS API; `max_running` cannot exceed the native runnable bound and
`max_waiting` cannot exceed `3`.
The Rust core has no third-party dependencies; its versioned C ABI is loaded by
ctypes from a prebuilt library, without runtime Cargo builds.

## Validation and release status

The split patch composition and prior CPU evidence remain historical provenance.
The exact v3 commit, tree and hook digest are retained in
[the frozen v3 provenance record](docs/V3_FROZEN_PROVENANCE.md) and must not be
rewritten by the v4 release.
The ABI v4 incident-repair line adds frozen profile bootstrap, exact runtime
identity, profile export and pre-enqueue TPS admission. The first September 21,
2026 [Linux validation record](docs/validation/governor-v4-linux-r1.json) applies
only to the earlier `a43a9d30` engine candidate. The current
[r2 Linux record](docs/validation/governor-v4-linux-r2.json) binds the tested
Governor source `74e981760572420329468eef663e5dfcf4ba8f6b`, clean complete-engine
commit `354d47922eafa95ebc2d7bd63b7627d780a01c26`, tree
`2733a3bae11d56e4bf3f694fbb49f8e9e156086d`, deterministic hook and the full
CPU/native test results. Composed immutable image, GPU and authorized C2
validation remain pending. The previous mixed-source v0.1.0 image is historical
and is not a deployment candidate.

Historical validation includes real SGLang lifecycle, HTTP/CAS/auth, cancellation,
parallel sampling and queue-time regressions. The affected auth/lifecycle suite
passed 88 methods with zero skips; counts overlap other suites. Those results
remain provenance, not automatic acceptance of the newly split composition.

The development model completed a frozen 1050-request workload, selected
protocol/image/cancellation checks, explicit tokenizer-owner drain and native
retraction. Throughput was broadly flat against the retained predecessor.
These development overlays do not qualify a final immutable release image.

See [development acceptance](docs/validation/DEV_V0520_ACCEPTANCE.md) for exact
runtime identity, retained failed probes and evidence boundaries. Strict dynamic
platform policy failed; launch/model measurement coverage remains unproven.
Final-image verification, reproducible image publication and the authorized
production test remain pending. Package version 0.2.1 identifies the ABI v4
source candidate; it is not a claim that those release gates have passed.
Historical v0.1.0 tags, the 0.1.1 source state and mixed-source image evidence
remain unchanged.

[Execution plan](docs/PIG_SGLANG_NATIVE_QOS_DESIGN.md) ·
[Upgrade assessment](docs/SGLANG_V0520_ASSESSMENT.md) ·
[Repository boundary](docs/REPOSITORY_BOUNDARY.md)

The complete engine repository is the entry point for serving repairs and their
integration status. Historical split-patch validation above records provenance;
it does not require users to assemble per-patch PRs or profiles.
