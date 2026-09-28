# Governor integration and policy API

## Integration

The current source candidate targets official SGLang v0.5.20, commit
`94602c9c2b7cbdb8efd5c52802dac6a1c180089e`.
Use the complete shared engine source in
[Phala-Network/sglang](https://github.com/Phala-Network/sglang), including general
lifecycle, auth/diagnostic, worker, schema and model compatibility repairs.
Governor owns its component and [minimal integration hooks](../patches/sglang/v0.5.20/README.md).
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
Component `0.2.9` uses C ABI v5. The Python adapter requires the ABI v5
`quarantine` symbol and rejects older libraries. Online learning starts with the resolved
SGLang settings and a positive TPS reference; it does not need a precomputed
model profile or manually supplied artifact/hardware identifiers:

```text
PIG_GOVERNOR_ENABLE=1
PIG_GOVERNOR_LIBRARY=/absolute/image/path/libpig_governor_core.so
PIG_TPS_REFERENCE=50
```

A v1 response-surface profile produced for ABI v5 may be supplied with both
`PIG_TPS_PROFILE_PATH` and `PIG_TPS_PROFILE_SHA256` as an optional startup prior.
That explicit path retains strict commit, artifact, hardware, predictor, expiry,
SHA-256 and resolved-runtime validation. Two absent or empty profile variables
select online mode; a non-empty path/hash must be supplied together. Missing
profile cells are learned online.

`pig_governor_admission.waiting_count` is the live native waiting-owner count
when supplied by the Scheduler. Without a native sample it is `0` only after
all Governor reservations drain, and otherwise `null`. The separate
`last_waiting_count` is the most recent admission sample, not a live queue size.

Without a profile, the online identity records the complete resolved runtime,
available version/commit/artifact/hardware fields, and a hash of the model
locator when available. Missing fields remain null; no artifact digest or
hardware slug is invented. If identity changes while old requests remain,
Governor rejects new admissions and profile export until those reservations
drain; it then clears old evidence and rotates the CAS epoch before learning
under the new identity. With an optional v1 profile, static fields still match
exactly; probed `runtime.max_total_tokens` may increase but not decrease.

Unknown response-surface cells use bounded exploration: at most one exploratory
reservation is active, starts are at least two seconds apart, and exploration
requires zero native waiting owners and stays within `max_running`. Concurrency
expands one cell at a time only when the adjacent lighter cell has qualified
live evidence at least 1.25 times the reference; a prior alone cannot authorize
expansion. A lone request can periodically
reprobe after insufficient or stale evidence. Once a cell has qualified data,
its forecast decides admission; measured TPS below the reference rejects with
429. `PIG_TPS_REFERENCE=0` remains an explicit unrestricted sampling mode.

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

Authenticated `GET /admin/v1/predictive-profile?expected_epoch=<epoch>` returns
an epoch-guarded, non-cacheable coverage envelope. Online identity mode returns
`availability=online_identity` and `profile=null`: it does not fabricate a
loadable v1 profile. An explicitly profile-bound v1 identity can still export a
loadable profile document. A stale epoch returns 409; malformed or multi-rank
state fails closed with 503.

Production launchers should suppress the polling endpoints from Uvicorn access
logs together with metrics:

```text
--uvicorn-access-log-exclude-prefixes /metrics /admin/v1/predictive-policy /admin/v1/predictive-profile
```

Admission is TPS-first and occurs before SGLang's grammar or ordinary waiting
queue. It forecasts the candidate's projected `(Decode concurrency, context
pressure)` cell from measured per-user Decode evidence; unknown cells follow the
bounded exploration gate above. A qualified cell below the reference returns
HTTP 429. A TPS-fit request is then
rejected when it would exceed either the logical `max_running + max_waiting`
capacity or the actual native waiting bound. Production defaults are 43 running
and 3 waiting. The fourth queued arrival is rejected even when logical running
slots remain. Queue age and TTFT are not independent rejection conditions.
The Rust core has no third-party dependencies; its versioned C ABI is loaded by
ctypes from a prebuilt library, without runtime Cargo builds.
