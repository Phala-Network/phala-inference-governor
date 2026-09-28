# Phala Inference Governor

Governor is a Rust admission controller with a Python adapter for SGLang. It
learns from actual Decode progress, uses a soft average-TPS reference, and
applies explicit running/waiting bounds before requests enter native queues.
It does not impose a per-request TPS floor or a TTFT rejection rule.

Governor is separate from the [Guard proxy](https://github.com/Phala-Network/phala-inference-guard)
and [TAIL transport/attestation layer](https://github.com/Phala-Network/pig-tail).
SGLang retains ownership of request execution, KV/cache and cleanup.

## Build the component

Use Linux, Rust/Cargo and Python 3.10 or later. The Rust core has no third-party
crate dependencies. From this repository:

```bash
cargo build --manifest-path rust/Cargo.toml --release --locked --offline
python -m pip install .
export PIG_GOVERNOR_LIBRARY="$PWD/rust/target/release/libpig_governor_core.so"
python scripts/check_component_contract.py
```

The contract probe exercises a private controller handle; it does not contact a
running model. See [probe details](scripts/README.md). Package installation does
not install SGLang or its engine hooks.

## Integrate with SGLang

Use the complete [Phala SGLang source](https://github.com/Phala-Network/sglang)
and a compatible Governor component. Current package metadata identifies
**0.2.9 / C ABI v5**; this identifies source, not universal image qualification.
Enable Governor explicitly in an image containing the native library:

```bash
export PIG_GOVERNOR_ENABLE=1
export PIG_GOVERNOR_LIBRARY=/opt/phala/governor/libpig_governor_core.so
export PIG_TPS_REFERENCE=50
```

The supported adapter boundary is **TP1/PP1/DP1, non-overlap scheduling, no PD
disaggregation**. Broader topologies remain unqualified. Use a matching complete
engine; do not apply historical hook patches again to an engine that embeds them.
Positive-reference startup requires `--skip-server-warmup` because synthetic
warmup requests can be rejected by admission.

Online learning needs no precomputed profile. An optional profile requires both
`PIG_TPS_PROFILE_PATH` and `PIG_TPS_PROFILE_SHA256` and strict identity validation.
See [integration and policy API](docs/INTEGRATION.md) for lifecycle, identity,
authentication and profile semantics.

## Management interface

- `GET/PATCH /admin/v1/predictive-policy`: authenticated policy reads and atomic
  updates to `tps_reference`, `max_running` and `max_waiting`; updates require
  `expected_epoch` and `expected_revision`.
- `GET /admin/v1/predictive-profile?expected_epoch=...`: epoch-checked profile
  availability/export. Online mode does not fabricate a reusable static profile.
- Governor telemetry is published through SGLang `/metrics`, forwarded by TAIL
  at authenticated `/v1/metrics`. See the [metric contract](docs/METRICS.md).

These endpoints belong to the integrated SGLang service, not a standalone
Governor server. A zero TPS reference is explicit sampling mode;
`max_waiting=0` pauses new admission while existing work drains.

## Development and releases

[`main`](https://github.com/Phala-Network/phala-inference-governor/tree/main)
is the integration line. Immutable [tags](https://github.com/Phala-Network/phala-inference-governor/tags)
identify releases. Combined serving images belong to `ghcr.io/phala-network/sglang`.
Select a release with a matching engine, component and image receipt.

- [Contributing and checks](CONTRIBUTING.md)
- [Documentation map](docs/README.md)
- [Repository responsibilities](docs/REPOSITORY_BOUNDARY.md)
- [Release policy](docs/RELEASING.md)
- [Historical validation](docs/HISTORY.md)

## License

[Apache License 2.0](LICENSE).
