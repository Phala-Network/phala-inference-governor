# Minimal Governor hooks for SGLang v0.5.20

Official upstream: `94602c9c2b7cbdb8efd5c52802dac6a1c180089e`.

First apply the separately pinned common fixes from
[sglang-serving-patches](https://github.com/Phala-Network/sglang-serving-patches).
Select model compatibility packs explicitly there when needed. Then apply
`0001-governor-hooks.patch`, whose exact hash and common prerequisite appear in
[manifest.json](manifest.json).

This patch touches only three runtime files:

- `http_server.py`: authenticated Governor policy route.
- `scheduler.py`: explicit enable/create, pre-grammar-queue TPS admission,
  prefill advice, batch-level actual-result/abort accounting, policy snapshot
  and namespaced policy dispatch.
- `io_struct.py`: allow the namespaced control payload.

General control correlation, diagnostics, auth, lifecycle/worker/schema fixes and
model compatibility are owned by the serving-patches repository. Governor does
not own request/KV/cache resources or replace native lifecycle mechanisms.

The new patch applies to common with or without either Qwen model pack. The
six-combination [reproduction report](../../../docs/validation/split-reproduction-r1.json)
proves patch application/AST and exact reconstruction of all 59 historical files
for the complete Qwen combination. New execution and image gates are pending.

The old mixed patches and release manifest remain retrievable from immutable
commit `b24dbadb3a8a9a1c701bb8cac12bd7648a0bb157` / tag `v0.1.0`.
Do not apply that historical mixed series alongside this one. Combined runtime
images are built by deployment configuration and published to
`ghcr.io/phala-network/sglang`.
