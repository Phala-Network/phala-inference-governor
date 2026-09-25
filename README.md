# Phala Inference Governor

PIG Governor is an independent Rust controller and thin Python adapter for
SGLang. It uses actual Decode token progress and sequence time to provide bounded
scheduling advice around a soft average-TPS reference, followed by explicit
`max_running` and `max_waiting` admission bounds. It does not impose a
per-request TPS floor, queue-age limit or TTFT rejection rule.

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

The initial adapter supports **TP1/PP1/DP1, non-overlap scheduling, no PD
disaggregation**, with ordinary text/images and radix cache enabled. Unsupported
topology fails during opt-in initialization. Broader topology and radix-disabled
`input_embeds` remain unqualified.

Build the Rust cdylib and install the Python package into the runtime image.
Component `0.2.4` uses C ABI v4. Production runtime configuration binds a
frozen response-surface profile to the exact composed engine, Governor source,
model artifact, hardware class and resolved SGLang settings:

```text
PIG_GOVERNOR_ENABLE=1
PIG_GOVERNOR_LIBRARY=/absolute/image/path/libpig_governor_core.so
PIG_TPS_REFERENCE=50
PIG_TPS_PROFILE_PATH=/absolute/image/path/qwen3.8-27b-profile.json
PIG_TPS_PROFILE_SHA256=<64 lowercase hex characters>
PIG_ENGINE_COMMIT=<40 lowercase hex characters>
PIG_GOVERNOR_COMMIT=<40 lowercase hex characters>
PIG_MODEL_ARTIFACT_ID=sha256:<64 lowercase hex characters>
PIG_RUNTIME_HARDWARE_ID=h200-sxm-tp1-v1
```

With a positive reference, both profile variables are required and the profile
must be unexpired and cover every reachable response-surface cell through exact
or jointly-heavier evidence. Static runtime identity fields must match exactly.
The probed `runtime.max_total_tokens` is a minimum capacity: the current runtime
may load a profile only when its capacity is greater than or equal to the sampled
capacity. Any decrease fails closed. `PIG_TPS_REFERENCE=0` is the explicit
offline sampling mode and may start without a profile. Runtime/model identity
changes clear prior and live evidence, rotate the CAS epoch and fail closed until
fresh qualified evidence exists.

Production uses the official `sglang serve` command. With the supplied auth patch,
`PIG_AUTH_FROM_TOKEN=1` reads the existing deployment `TOKEN` for both API and
admin authentication. Do not put credentials in argv. Missing/invalid TOKEN or
any simultaneous explicit API/admin key fails startup. Diagnostic serialization
redacts keys without altering runtime configuration or internal IPC.

Use `--skip-server-warmup` with positive-reference Governor startup. SGLang's
internal synthetic completion can be rejected by admission and otherwise abort
startup; explicit offline sampling and later functional probes remain separate.

## External policy API

Authenticated `GET/PATCH /admin/v1/predictive-policy` reads and atomically updates
the soft `tps_reference` plus hard `max_running` and `max_waiting` bounds. PATCH
requires `expected_epoch` and `expected_revision` for CAS. A successful hot update
neither restarts the model nor resets actual history. `tps_reference=0` is the
explicit C2 offline sampling mode; a CAS update from 0 to the production reference
preserves the learned response surface.
`max_waiting=0` pauses new native admission while existing work drains.
Restoring a positive waiting limit through CAS resumes admission without a
restart.

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
that cell is unqualified or falls below the reference. A TPS-fit request is then
rejected when it would exceed either the logical `max_running + max_waiting`
capacity or the actual native waiting bound. Production defaults are 43 running
and 3 waiting. The fourth queued arrival is rejected even when logical running
slots remain. Queue age and TTFT are not independent rejection conditions.
The Rust core has no third-party dependencies; its versioned C ABI is loaded by
ctypes from a prebuilt library, without runtime Cargo builds.

## Validation and release status

The split patch composition and prior CPU evidence remain historical provenance.
The exact v3 commit, tree and hook digest are retained in
[the frozen v3 provenance record](docs/V3_FROZEN_PROVENANCE.md) and must not be
rewritten by the v4 release.
The ABI v4 incident-repair candidate adds frozen profile bootstrap, strict static
identity with a fail-closed KV-capacity floor, profile export and pre-enqueue TPS
admission. The first September 21, 2026
[ABI v4 Linux record](docs/validation/governor-v4-linux-r1.json) belongs to the
earlier `a43a9d30` engine. The current hook bytes and complete engine tree were
cleanly replayed in the
[r2 Linux record](docs/validation/governor-v4-linux-r2.json).
Governor 0.2.2 adds the cross-start capacity compatibility repair and was
revalidated on September 22, 2026 with 28 Rust tests, 138 Governor
Python/FFI/SGLang tests, the ABI component contract and 10 native SGLang
pre-header HTTP error tests; see the
[0.2.2 Linux validation record](docs/validation/governor-v4-capacity-compat-linux-r1.json).
Composed-image, GPU and authorized C2 validation remain pending. The previous
mixed-source v0.1.0 image is historical and is not a deployment candidate.

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
production test remain pending. Package version 0.2.2 identifies the ABI v4
source candidate; it is not a claim that those release gates have passed.
Historical v0.1.0 tags, the 0.1.1 source state and mixed-source image evidence
remain unchanged.

[Execution plan](docs/PIG_SGLANG_NATIVE_QOS_DESIGN.md) ·
[Upgrade assessment](docs/SGLANG_V0520_ASSESSMENT.md) ·
[Repository boundary](docs/REPOSITORY_BOUNDARY.md)

The complete engine repository is the entry point for serving repairs and their
integration status. Historical split-patch validation above records provenance;
it does not require users to assemble per-patch PRs or profiles.
