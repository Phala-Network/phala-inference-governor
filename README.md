# Phala Inference Governor

PIG Governor is an independent Rust controller and thin Python adapter for
SGLang. It uses actual Decode token progress and sequence time to provide bounded
scheduling advice around a soft average-TPS reference. It does not impose a
per-request TPS floor or a hard queue/TTFT rejection rule.

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
is an optional deterministic export of the complete source, not a separately
maintained implementation or a required release step. Model-specific code
activates only in its relevant module/model/configuration. Preserve frozen
historical inputs and evidence while integrating new changes in the shared source.
Deployment build configuration pins the complete engine and Governor commits. Combined runtime images
are published to `ghcr.io/phala-network/sglang`, associated with
[Phala-Network/sglang](https://github.com/Phala-Network/sglang); this repository
publishes the Governor component and hooks, not SGLang runtime images.

The initial adapter supports **TP1/PP1/DP1, non-overlap scheduling, no PD
disaggregation**, with ordinary text/images and radix cache enabled. Unsupported
topology fails during opt-in initialization. Broader topology and radix-disabled
`input_embeds` remain unqualified.

Build the Rust cdylib and install the Python package into the runtime image.
Runtime configuration:

```text
PIG_GOVERNOR_ENABLE=1
PIG_GOVERNOR_LIBRARY=/absolute/image/path/libpig_governor_core.so
PIG_TPS_REFERENCE=50
```

Production uses the official `sglang serve` command. With the supplied auth patch,
`PIG_AUTH_FROM_TOKEN=1` reads the existing deployment `TOKEN` for both API and
admin authentication. Do not put credentials in argv. Missing/invalid TOKEN or
any simultaneous explicit API/admin key fails startup. Diagnostic serialization
redacts keys without altering runtime configuration or internal IPC.

## External policy API

Authenticated `GET/PATCH /admin/v1/predictive-policy` reads and updates the soft
`tps_reference`. PATCH requires `expected_epoch` and `expected_revision` for CAS.
A successful hot update neither restarts the model nor resets actual history.
`tps_reference=0` is the explicit C2 offline sampling mode; a CAS update from 0
to the production reference preserves the learned response surface.

Admission is TPS-first and occurs before SGLang's grammar or ordinary waiting
queue. It forecasts the candidate's projected `(Decode concurrency, context
pressure)` cell from measured per-user Decode evidence, and returns HTTP 429 when
that cell is unqualified or falls below the reference. There is no fixed waiting
or inflight cap; waiting is not itself a rejection condition.
The Rust core has no third-party dependencies; its versioned C ABI is loaded by
ctypes from a prebuilt library, without runtime Cargo builds.

## Validation and release status

The split patch composition passes application and Python AST checks for six
common/model/Governor selections. The complete Qwen combination reproduces all
59 historical source/test files exactly. Common and selected model CPU regressions now pass; composed-image
verification remains pending; the previous mixed-source v0.1.0 image is historical
and is not a new deployment candidate. The minimal Governor patch changes only
three runtime files.

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
production test remain pending. Package version 0.1.1 identifies this source;
it is not a claim that those release gates have already passed.
Version 0.1.1 identifies the split Governor component with standalone core CI;
historical v0.1.0 tags and mixed-source image evidence remain unchanged.

[Execution plan](docs/PIG_SGLANG_NATIVE_QOS_DESIGN.md) ·
[Upgrade assessment](docs/SGLANG_V0520_ASSESSMENT.md) ·
[Repository boundary](docs/REPOSITORY_BOUNDARY.md)

The complete engine repository is the entry point for serving repairs and their
integration status. Historical split-patch validation above records provenance;
it does not require users to assemble per-patch PRs or profiles.
