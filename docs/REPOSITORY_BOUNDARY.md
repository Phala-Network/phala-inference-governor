# Repository boundary

- **phala-inference-governor** owns the Rust control core, Python adapter,
  authenticated policy API and minimal SGLang hooks. It does not own generic
  serving repairs, model compatibility or the composed SGLang image.
- **Phala-Network/sglang** owns the complete shared engine source, including
  lifecycle/cleanup, metadata, grammar/schema, protocol, auth, diagnostics and
  model compatibility. Serving fixes must work without installing Governor.
- **sglang-serving-patches** maintains one ordered patch stack for general and
  model-specific serving changes. Common changes are not copied into per-model
  patch systems, and the workflow does not require one PR per patch.
- **Phala-Network/sglang** composes the ordered serving stack and the pinned
  Governor component/hooks on the fixed upstream baseline, then records the
  complete engine commit and tree used for release.
- **phala-models-compose** consumes the fixed complete engine, Governor commit,
  upstream/base identities and final immutable image. It publishes
  `ghcr.io/phala-network/sglang`, associated with Phala-Network/sglang, and does
  not substitute deployment configuration for the source composition steps.
- **PIG TAIL** remains the separate transport/TEE/attestation layer. Legacy Guard
  history is not renamed or absorbed.

The v0.5.20 split reproduces the historical combined source bytes. Historical
mixed patches/images remain in immutable Git/tag/registry history and are not
new deployment candidates. New composition tests, image qualification and e4
acceptance are required independently. No runtime source mounts, method
replacement, startup patching or source download is used for integration.

The immutable v3 source and hook identities are recorded in
[V3_FROZEN_PROVENANCE.md](V3_FROZEN_PROVENANCE.md). Later manifests and releases
must add new identities without changing that historical record.
