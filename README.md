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
Apply the [ordered versioned patches](patches/sglang/v0.5.20/README.md).
The first six cover explicit Governor hooks, independent lifecycle/serving fixes
and environment authentication. The seventh retains the Qwen3.8-27B-specific
serving compatibility changes for this model release. No full SGLang checkout,
monkeypatching, class replacement or runtime source transformation is required.

The initial adapter supports **TP1/PP1/DP1, non-overlap scheduling, no PD
disaggregation**, with ordinary text/images and radix cache enabled. Unsupported
topology fails during opt-in initialization. Broader topology and radix-disabled
`input_embeds` remain unqualified.

Build the Rust cdylib and install the Python package into the runtime image.
Runtime configuration:

```text
PIG_GOVERNOR_ENABLE=1
PIG_GOVERNOR_LIBRARY=/absolute/image/path/libpig_governor_core.so
PIG_TPS_REFERENCE=35
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
The Rust core has no third-party dependencies; its versioned C ABI is loaded by
ctypes from a prebuilt library, without runtime Cargo builds.

## Validation and release status

The seven-patch series reproduces all 59 frozen changed source/test files from
the official commit. [The release source manifest](patches/sglang/v0.5.20/release-manifest.json)
records their exact LF hashes. Source validation includes real SGLang lifecycle,
HTTP/CAS/auth, cancellation, parallel sampling and queue-time regressions. The
latest affected auth/lifecycle suite passed 88 test methods with zero skips;
these overlap prior suites and are not additive test counts.

The development model completed a frozen 1050-request workload, selected
protocol/image/cancellation checks, explicit tokenizer-owner drain and native
retraction. Throughput was broadly flat against the retained predecessor.
These development overlays do not qualify a final immutable release image.

See [development acceptance](docs/validation/DEV_V0520_ACCEPTANCE.md) for exact
runtime identity, retained failed probes and evidence boundaries. Strict dynamic
platform policy failed; launch/model measurement coverage remains unproven.
Final-image verification, reproducible image publication and the authorized
production test remain pending. Package version 0.1.0 identifies this source;
it is not a claim that those release gates have already passed.

[Execution plan](docs/PIG_SGLANG_NATIVE_QOS_DESIGN.md) ·
[Upgrade assessment](docs/SGLANG_V0520_ASSESSMENT.md) ·
[Repository boundary](docs/REPOSITORY_BOUNDARY.md)
