# Repository boundary

- **phala-inference-governor** owns the Rust control core, Python adapter,
  authenticated policy API and minimal SGLang hooks. It does not own generic
  serving repairs, model compatibility or the composed SGLang image.
- **sglang-serving-patches** owns independent lifecycle/cleanup, metadata,
  grammar/schema, protocol, auth and diagnostic fixes. Versioned `common/` and
  `models/<model>/` directories separate shared behavior from explicitly selected
  model compatibility. It must work without installing Governor.
- **Deployment build configuration** independently pins both repository commits,
  upstream SGLang and immutable base; selects common/model/Governor patches;
  produces `ghcr.io/phala-network/sglang`, associated with Phala-Network/sglang.
- **PIG TAIL** remains the separate transport/TEE/attestation layer. Legacy Guard
  history is not renamed or absorbed.

The v0.5.20 split reproduces the historical combined source bytes. Historical
mixed patches/images remain in immutable Git/tag/registry history and are not
new deployment candidates. New composition tests, image qualification and e4
acceptance are required independently. No runtime source mounts, method
replacement, startup patching or source download is used for integration.