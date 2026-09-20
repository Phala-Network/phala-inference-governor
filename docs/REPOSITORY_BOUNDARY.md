# Repository boundary

- **phala-inference-governor** owns the Rust control core, Python adapter,
  authenticated policy API and minimal SGLang hooks. It does not own generic
  serving repairs, model compatibility or the composed SGLang image.
- **Phala-Network/sglang** owns the complete shared engine source, including
  lifecycle/cleanup, metadata, grammar/schema, protocol, auth, diagnostics and
  model compatibility. Serving fixes must work without installing Governor.
- **sglang-serving-patches** is an optional deterministic export of that source,
  not an independent implementation or a required build/release step.
- **Deployment build configuration** pins the complete engine and Governor commits,
  upstream SGLang and immutable base;
  produces `ghcr.io/phala-network/sglang`, associated with Phala-Network/sglang.
- **PIG TAIL** remains the separate transport/TEE/attestation layer. Legacy Guard
  history is not renamed or absorbed.

The v0.5.20 split reproduces the historical combined source bytes. Historical
mixed patches/images remain in immutable Git/tag/registry history and are not
new deployment candidates. New composition tests, image qualification and e4
acceptance are required independently. No runtime source mounts, method
replacement, startup patching or source download is used for integration.
