# SGLang v0.5.20 integration

Pinned official commit: `94602c9c2b7cbdb8efd5c52802dac6a1c180089e`.

Apply these patches in order to that source:

1. `0001-governor-hooks.patch`: explicit scheduler/HTTP hooks, namespaced control payloads and correlated get/set replies.
2. `0002-serving-cleanup.patch`: generator/disconnect/SHM ownership and safe native MM cleanup.
3. `0003-worker-metadata-grammar.patch`: independent prefill metadata handoff and identity grammar-mask fixes.
4. `0004-lifecycle-observation.patch`: read-only tokenizer ownership counts and first-admission queue-time telemetry across retractions. Current requeue timestamps and scheduling remain unchanged.
5. `0005-parallel-sampling-owners.patch`: register only logical input parents before native n-way fan-out; successful default/string-RID requests no longer leave unused owners.
6. `0006-unified-token-auth.patch`: explicit `PIG_AUTH_FROM_TOKEN=1` resolves both API and admin auth from the existing environment TOKEN through the official CLI. Missing/invalid TOKEN and all explicit API/admin keys are rejected in this mode. Diagnostic output redacts credentials while runtime configuration and IPC retain the actual values.

7. `0007-qwen-serving-compatibility.patch`: the model-specific residual changes
   retained for the validated Qwen3.8-27B serving contract, including changes in
   files also touched by the generic hooks. Apply it for this model release;
   it is separate from Governor's generic integration requirements.

The seven-patch release series reproduces all 59 frozen source/test files from
official commit `94602c9c2b7cbdb8efd5c52802dac6a1c180089e`, with exact LF hashes
and Python AST checks in the fixed 805 CPU environment. The current complete
manifest is `release-manifest.json` (SHA256
`ce5384182634b4b7410fcd6ad631e1397642c15c2b2cdb560469149d1cf899fe`).
The older four-patch `manifest.json` below remains historical evidence.
Parallel-sampling red/green validation ran two real-lifecycle test methods: the
old source had eight expected failing subcases, while the corrected source passed
those methods and 28 owner/four timing regressions. Auth validation passed 88
methods with zero skips, including these affected lifecycle contracts; these
overlapping counts are not additive. Source reports are retained under
`docs/validation/n2-owner-lifecycle-cpu-r3.json` and
`docs/validation/governor-token-auth-cpu-r2.json`.

The patchset was applied with `git apply --check` followed by `git apply` for
each patch. The current four-patch series reproduces all 26 changed source/test
files in LF text against the pinned official source. `manifest.json` records
the current patch and source hashes; `lifecycle-manifest.json` records the
five-file delta from the earlier candidate. The archived three-patch check
compared 25 selected files, including an unchanged selected file.
If a Windows export introduces CRLF, normalize the exported Python text to LF
before applying; this is text reproduction, not a raw-byte identity claim.

The separately maintained Governor Rust core and C ABI are unchanged. The Python
adapter reads published effective configuration namespaces; no dynamic method
replacement or class wrapping is used in the runtime integration.

The earlier three-patch CPU integration passed 126 Python tests with zero skips on the approved fixed
development image. It used the complete official v0.5.20 Python source plus
these overlays, real SGLang methods/msgspec/HTTP and the unchanged verified Rust
shared library. Rust source/header/manifest/lock matched the archived r5 build,
whose seven Rust tests already passed; the Rust tests were not needlessly rerun.
The fourth patch passed 28 native RID/HTTP observation regressions plus four
queue-time/serialization regressions on the model migration candidate. This is
32 targeted checks, not 32 additional unique tests on top of the earlier 126.
The running govdev1 wheel does not yet contain this fourth patch.

Evidence:
- [CPU integration](../../../docs/validation/governor-v0520-candidate-r1.json)
- [Current patch reproduction](../../../docs/validation/governor-v0520-patchset-r2.json)
- [Lifecycle targeted validation](../../../docs/validation/governor-queue-telemetry-r1.json)

The CPU environment still has the earlier kernel/deep-gemm dependencies and uses
upstream CPU test kernel stubs where required. This does not qualify v0.5.20 GPU
binaries, performance, H200 confidential computing, or a real model/TAIL rollout.
Those remain separate acceptance work. Subsequent GPU startup and selected
protocol/MM/cancel checks passed on the candidate; r44 is stopped and retained.
See [current acceptance](../../../docs/validation/DEV_V0520_ACCEPTANCE.md).
