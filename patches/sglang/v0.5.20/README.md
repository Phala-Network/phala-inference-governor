# Minimal Governor hooks for SGLang v0.5.20

Official upstream is `94602c9c2b7cbdb8efd5c52802dac6a1c180089e`.
Apply the ordered serving stack from
[sglang-serving-patches](https://github.com/Phala-Network/sglang-serving-patches)
first. The immediate parent for this v4 hook is complete SGLang commit
`5871a82cb0ef1221126b8c9712881e497041bd38`, tree
`b0b36fb1d0b46410a6bd8c721ddb163237cc1be9`. That parent includes the common
pre-header HTTP error propagation needed for a scheduler admission 429 to remain
a real HTTP 429 for streaming endpoints.

Then apply `0001-governor-hooks.patch`, SHA256
`52087d47d575c95351e17e5335ac6ca5a3d5b6bb63548438502521ca524117ea`.
The result is complete SGLang commit
`354d47922eafa95ebc2d7bd63b7627d780a01c26`, tree
`2733a3bae11d56e4bf3f694fbb49f8e9e156086d`. Exact identities and validation
scope are recorded in [manifest.json](manifest.json).

The Governor patch still touches only three runtime files:

- `http_server.py` exposes authenticated, non-cacheable predictive-policy and
  predictive-profile routes.
- `scheduler.py` creates the ABI v4 adapter from effective runtime values,
  reserves or rejects before grammar/waiting insertion, emits HTTP 429 for an
  unsafe projected response-surface cell or logical waiting overflow, records
  actual Decode progress, releases exactly once across admission handoff
  failures, exports policy/profile state and rotates evidence when runtime or
  model identity changes. Governor opt-in fails for non-generation models and
  rejects beam search before beam-group creation because the current response
  surface represents one native Decode row per request.
- `io_struct.py` carries the namespaced control payload; Scheduler remains the
  validator and owner of the update transaction.

The preceding common serving patch owns HTTP/SSE propagation and has an
independent native SGLang regression. Governor does not own request, KV, cache,
tensor or native cleanup resources. Its request attachment is admission and
accounting state only.

On September 21, 2026, the composed source passed 28 Rust unit tests, 133
Governor Python/FFI/SGLang tests, 10 native SGLang pre-header error tests and the
real ABI 4 component contract on the authorized Linux test host. Those checks
do not establish GPU, final-image or production acceptance.

The exact v3 hook bytes and manifest are retained under [history/v3](history/v3)
and bound by the [frozen v3 provenance record](../../../docs/V3_FROZEN_PROVENANCE.md).
Do not apply v3 and v4 hooks together. Runtime images are built from the complete
Phala-Network/sglang source and published only to `ghcr.io/phala-network/sglang`.
