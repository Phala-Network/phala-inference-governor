# Development v0.5.20 acceptance checkpoint

Updated 2026-09-19. This checkpoint supersedes historical statements that r44
is still serving or that the new candidate has not reached a real GPU.
It is partial development acceptance, not a production release qualification.

## Current govdev2 checkpoint

Fresh status `governor-v0520-status-release-prep-r1.json` (SHA256
`efd910b81079156753b4fcd58c1a51a27d3447da3ced8d374f7affd2c8db621e`)
confirms backend `e7ae81fdaba2475c6d3460f6b4e7f09e76d20011626afec267026bcf17c6d48d`,
started `2026-09-19T13:29:05.481416279Z`, version `0.5.20+phala.govdev2`,
epoch `6652b3ceb6ad445aa1a36a2adeeeea23`, reference 35/revision 1. TAIL is
unchanged. Neither service restarted or OOMed. This remains a staged development
runtime, not a published immutable image.

The text and multimodal disconnect cases now capture all three live facts:
one dispatched tokenizer owner, one Governor active sequence, and TAIL inflight
one, after visible SSE. Both explicitly closed streams then drained every owner,
Governor/native pool and TAIL counter. A cold-image completion also passed.
The r3 report is retained as failed because its last text-recovery assertion
expected exactly `OK` but received the valid response `OK.`. The recovery-only
r4 run accepts those two spellings, passed, and records three final zero drains;
it does not repeat the first three cases. Earlier r1/r2 failures remain retained:
r2 samples exposed premature client iterator disposal, fixed by retaining the
iterator until explicit close. Cached native Prometheus gauges are diagnostic
during activity; all native gauges remain required zero at final drain.

Native retract/resume also passed on govdev2: one retraction, 1024 output tokens,
1023 measured Decode tokens, one owner while running and parked, zero owners on
completion/drain. Public `queue_time` is `0.0007698535919189453` seconds, replacing
the predecessor's negative telemetry. No policy or serving configuration changed.

| Evidence | SHA256 |
| --- | --- |
| govdev2-owner-mm-r2.json (retained failed probe) | `fd5f4b888267c166b866dd14927cd42eef1f9d10c2867d3dcd4db1517b196090` |
| govdev2-owner-mm-r3.json (first three cases pass; exact-text failure retained) | `caabbbf382f32780445cd89e00d2381af2bc859c986cf60fd244c56840bd7aa3` |
| govdev2-owner-recovery-r4.json | `449d87b2e7df6f12952dedc1adcc2aebd75aff7f350cdae580f6cda55c451a31` |
| govdev2-retract-r1.json | `59986254a3d49505117c3569335f66d66498e0053821a733d849359b54f717d8` |

The successful default/string-RID parallel-sampling path has a separate native
owner leak (`B*(n-1)` unused initial owners). Its source fix now passes red/green
regression: the old source fails eight subcases across two test methods; the
fixed source passes those two methods and 28 owner/four timing regressions.
Do not intentionally reproduce it on the unfixed live runtime. Official-CLI
TOKEN auth plus affected contracts passed 88 methods with zero skips; these
overlap the owner/timing checks and must not be summed as independent coverage.
The complete seven-patch release series reproduces 59 frozen source/test files
against official v0.5.20; source reproduction is not a final-image or GPU test.
Final-image authentication and independent Governor/TAIL v0.1.0 release
packaging remain pending. The user authorized those releases and deployment only
to `e4bd4036-c788-45fd-bbac-3c649ef522b8`; strict dynamic-platform and launch-measurement
gaps below remain disclosed.

## Historical govdev1 serving identity

- Authorized development CVM: `805bfe7c-00f5-4e14-995a-8f25631b7703`.
- Backend: `48a957fcd3ea5fc8a27df869d2a9a54d8c9541153dd5014b7e440e4da2fc851f`,
  started `2026-09-19T10:04:40.659380842Z`.
- TAIL: `1a9bae9fc56f316f7ce92fd84e2d88adc670726f4880e357762d7e30b9ef0fda`,
  started `2026-09-19T10:04:41.124990164Z`.
- Both retain image `sha256:c4fe40487178fd7738600562c114e2198d281d5ca6c0f5a265019369f6af76c4`
  with staged readonly candidate components; this is not the official v0.5.20 image.
- Installed SGLang: `0.5.20+phala.govdev1`. Effective TP1/PP1/DP1,
  non-overlap, no PD, context 262144, FP8 e4m3 KV, EAGLE, response store enabled.
- Qwen3.8-27B-FP8 revision: `017b9c7af6b5689d5dd426a76e0bc077eb5ca20a`.
- Policy epoch `1db6eebcf06a46598b7e1c39f25a60be`, reference 35, revision 3.
- Latest status and protocol final checks show unchanged starts, zero restarts,
  and no OOM. The stopped r44 predecessor is retained for recovery.

## Completed checks and limits

Earlier basic acceptance covered chat/nonstream and stream, external Admin
35→42→35 without restart/history reset, stale revision 409 and unauthenticated
401. Protocol r1 additionally passed streamed chat n=2, completions and ordinary
Responses/nonstream and stream. These do not prove Responses retrieval/chaining.

Protocol r1 failed its empty named-tool argument assertion: HTTP 200 returned
the requested `ping` call but included example parameters. That probe omitted
`function.strict=true`, unlike the previously validated native empty-schema
regression. The original failure is retained. Protocol r2 corrected only this
contract, did not replay the four completed cases, and passed all seven remaining
cases: strict empty named tool, structured JSON, cold image, hot image, long
chunked image prompt, disconnect after first visible multimodal content, and text
recovery. Each case drained native running/waiting, KV/Mamba usage, TAIL in-flight,
and Governor active Decode sequences. Three final drains passed. These observations
do not establish tokenizer-owned request or SHM cleanup beyond the measured gauges.

Current TAIL also passed two fresh authenticated v1 hardware-report challenges:
actual TDX quote report-data matched signing address plus nonce; NVIDIA HOPPER
payload carried one nonempty GPU evidence/certificate pair and the same nonce.
Unauthenticated access returned 401. This proves collection and structural binding;
it does not verify signatures/collateral/TCB, TLS v2 binding, or launch/model policy.
Full hardware reports remain in the private task evidence directory.

## Evidence

Files below are under the task's `evidence/` directory on the authorized CVM;
the new status, protocol r1/r2 and hardware reports are downloaded with SHA256
verification to the existing local task directory.

| Artifact | SHA256 |
| --- | --- |
| governor-v0520-status-continuation-r4.json | `163efb564699a0b902f289503abc2ee1ee903bc0ae8fecd2b5742ace08e30ead` |
| governor-v0520-basic-r1.json | `3e344e2815ea8e1dbadfd23986a3e5bc71564e12859056e98fa00ed072c715a5` |
| governor-v0520-protocol-mm-r1.json | `c79afdf12e8611cbe0f930dee35ffc3cbe6013679428df391174a13905227c17` |
| governor-v0520-protocol-mm-r2.json | `99f27a138a3f7e4a34e4681a9cbbb330b7f8507fb0547268c6ff54cc07327931` |
| governor-tail-hardware-r1.json | `8d94af1a30336e016a32f0a64901871767b825c6f2f1bb6f9b40aa2858c65fb9` |

## Matched workload and real retraction

The frozen 1050-request/900-second mixed traffic run completed with 1050 HTTP
200 responses, complete SSE/usage, no observer errors, and three final drains.
The client had no CPU quota. Both manifests match
`f3ad011c98fe7549a7d0d205c4fc3e364555099b54c5d80165be791863fd7b9b`;
all request bodies, endpoints, images and arrival offsets match retained r44.
No old baseline was rerun. Current observations use Governor policy and native
metrics, with no deleted forecast journal or old serving-lineage validator.

| Observation | Retained r44 | v0.5.20 Governor |
| --- | ---: | ---: |
| Completed / HTTP 200 | 1050 / 1050 | 1050 / 1050 |
| Delivered output tokens / workload wall second | 245.0827 | 244.9905 |
| Client first-content TTFT P50 / P95, seconds | 0.3297 / 1.4914 | 0.3208 / 1.5072 |
| Client end-to-end P50 / P95, seconds | 1.5918 / 11.3401 | 1.4963 / 11.3606 |
| Backend CPU cores over observed sample span | 1.3022 | 1.2773 |

This is broadly flat throughput with mixed small latency differences, not a
causal or repeated performance gain. SGLang, dependencies, TAIL and observer
cadence differ. Current 60-second native average-TPS samples ranged from
50.8069 to 216.9293, with reference 35; this is a sampled observation, not a
per-window guarantee. The analyzer retains per-phase/kind client distributions
and low individual rates without turning them into failures. Queue age is not
available in the collected current API evidence.

After that run ended, an isolated native text request passed pause/retract/resume:
one native retraction, 1024 completed output tokens, and exactly 1023 measured
Decode tokens within a 6.057-second observation interval. Parked running/queue
counts were 0/1 and active KV/Mamba usage was zero. Governor active sequences
stayed at one while sequence-seconds increased and token count stayed at 16;
after resume, completion and all measured resources drained. This validates
release/requeue/re-prefill and lifetime accounting, not pressure victim selection
or forced-OOM behavior. No model configuration or source changed for these runs.

The native response exposed `queue_time=-0.261615514755249` after retract.
Source inspection identifies `set_wait_queue_entry_time` replacing the queue
timestamp while `forward_entry_time` retains its first value; `get_queueing_time`
subtracts those different lifecycle timestamps. Retain this as a native telemetry
defect, not a model-generation failure or valid queue-latency measurement.

## TLS v2 and captured-report verification

The current CGO TAIL binary passed two fresh TLS v2 nonce/SPKI challenges and a
real SSE returning `OK`, using the same pinned TLS public-key identity. Actual
NVML reported the expected H200, CC enabled and DevTools disabled. The temporary
loopback listener exited cleanly and its private-key directory was removed.

Both NVIDIA reports passed NRAS official policy, ES384 JWT signature, issuer,
time, nonce, overall/detached digest and tampered-signature rejection checks.
Both TDX reports passed standard DCAP signature/Intel-chain validation, with
`UpToDate` QE/platform TCB and tampered-report-data rejection.

**Strict platform policy failed for both quotes:** `Dynamic platform is not
allowed by policy`. This fresh failure remains independent of cryptographic
validity and was not reclassified as a pass. Backend/dependency/model launch
measurement coverage is still unproven; no production trust acceptance is claimed.

| Additional evidence | SHA256 |
| --- | --- |
| governor-matched-r1/outcome.json | `84d4d7ae06a4c053e82974b9a9d07840cfddf13db92b234d2b19aae77f153118` |
| governor-matched-analysis-r1.json | `299be74fb1dc715c238f01e196386e09ea6d4a3504ee663e729a4d9fa0222837` |
| governor-retract-r2.json | `0959c67968c26145be6b1059febf1291dc6fe351cf956e8c69108abcb66b7e32` |
| governor-tls-gpu-verification-r1/result.json | `8231d2964757222d02cdc36d5d20458c4e69df6a6151f7caf3b3f1fe86f571a8` |
| governor-tls-cpu-verification-r1/result.json (strict failure) | `637515949266d17e09385e1836adcbed4f92b4fa5faa10d0991a405592327510` |
| governor-tls-cpu-cryptography-r1/result.json | `0f0c3491738c3285b04bcf6763ce344d057f716139257d690973f260a161026b` |
| governor-tls-hardware-r1/outcome.json | `a19b2ab6f25939d9b137d5662ebcadb02d6c145a697f2f8de25788fa4e4e2c2c` |

The 42-file evidence archive is `evidence/governor-acceptance-r1.tgz`, SHA256
`28eabb7647bac08dda39a75d8a02b36bf16d7a2f080fb5414735490f6f1fdd7b`.
Its internal inventory hash is
`7c94cdacea0fc84904d041ca7957a74816dca46d32a111cb51f8527b7f66d828`.
It contains the frozen workload, analysis, retraction, TLS and verifier records;
deployment configs and private keys are excluded. Keep raw evidence private.
The archive was downloaded, its internal inventory hash was verified, and all
42 extracted files matched their recorded SHA256 and byte length locally.

## Remaining work

The source candidate now includes a read-only summary of the existing tokenizer
RID table/encoder dispatch map, exposed through protected `/server_info`;
unsupported router-local observation is explicitly null. A separate first-wait
timestamp repairs public first-admission queue time without changing current
requeue timestamps consumed by scheduling. The field survives real serialization
with clock rebasing and is reset on explicit prefill retry. Combined affected CPU
validation passed 28 ownership/HTTP regressions and four timing regressions.
The earlier failed ownership test run is retained: two old mock fixtures lacked
the newly ported `min_thinking_tokens=None` default and failed before dispatch;
only the fixture was corrected. These source changes are not yet live.

All four versioned patches independently reproduced 26 changed upstream files;
the generic upstream runtime delta is +552/-133 across 20 files, and its test
delta is +1284/-0. These counts exclude the separate Governor Rust/Python package
and Qwen-specific compatibility patches, so they are not whole-product totals.

Additional read-only kernel observation joined the current backend PID namespace
from a fixed c4/runc/GPU-hidden/one-CPU container: three drained samples each
scanned six processes with no errors, no `sgl_shm_mm` open handles or mappings,
and zero matching named segments in the shared host IPC namespace. This is
point-in-time POSIX-MM evidence, not Python RID-table or CUDA-IPC ownership proof.

New evidence hashes:

- Owner CPU report: `38b567f658bc18344a5b30b22d7693569b2060c1670da136a86f2f3480def57c`.
- Combined lifecycle CPU report: `363236b867fca9c238280a1f7949b139eb40ea4b806b991d3dbfa682cf46938c`.
- Kernel SHM report: `1d399e82969b84c7639df0f5eef8c3c117aec363d89acbcf09c10269a70d6214`.
- Four-patch reproduction manifest: `a3938c4da72727fa6b0bf3ef2ad2e1b2037db22d536636a3b91e36430b661000`.

Offline packaging produced `sglang-0.5.20+phala.govdev2-py3-none-any.whl`, SHA256
`64d282781e6d842c0b76a28a87d8cce24f119b588e7faf2f89638856c3a8fb93`,
under `governor-v0520-launch-r3/wheels/` on the authorized CVM. The installed
candidate is `governor-v0520-launch-r3/site/`. Module/distribution versions match;
all three changed installed runtime files match validated source hashes, and
CPU installed-package probes confirm owner summary and first/current queue-time
separation. Packaging report SHA256:
`c462e32735d59b1bc2e3cf721e1f3dfd76a45548ea4737663e1305d9d89b1886`.
The build reused the prior build tools offline and left both live containers,
their startup times, epoch, reference and revision unchanged.

Delivery-source audit (`governor-delivery-audit-r1.json`, SHA256
`af73f8eefc9a4f1f03c745321bafaf715fc19c05e211c43817816f3c7c6cdc97`)
confirms 269 Rust runtime lines, 116 Rust test lines/seven test declarations,
zero Rust external dependencies, and six Python runtime files/335 lines.
The seven Python test files contain 735 lines/34 test declarations; declaration
counts are distinct from executed or parameter-expanded test counts. Source
matches the verified Rust reference and currently mounted Python plugin.
The validated SGLang model-change union relative to official v0.5.20 covers
29 runtime files (+1731/-245) and 21 test files (+3385/-10); these physical-line
counts include comments/blanks and are scoped to the explicitly verified union.

Pinned installed-component comparison (`govdev2-source-delta-r1.json`, SHA256
`93efcfa311cb0f67f2bd2e6bff2b36e9f7b361f069f9f1e52b5aca5eaa96cd03`)
finds exactly four changed SGLang module files: generated version,
`http_server.py`, `tokenizer_manager.py`, and `req_time_stats.py`; the other
4549 module files match. Dependencies, Governor package/library, TAIL and
launcher inventories remain identical. Dist-info metadata is outside this
module comparison. Thus the govdev2 delta is observation/telemetry, not a new
controller or serving configuration; the govdev1 matched workload is retained.

The staged govdev2 transition and its n=1 ownership/retraction checks are now
complete as recorded above. Final source/test counts, n>1 repair, reproducible
packaging/provenance and applicable strict-platform/launch-measurement work remain.

Supported live inputs remain ordinary text/images with radix cache enabled.
The broader radix-disabled `input_embeds` path is unqualified: native retract
clears output IDs, conflicting with Governor's monotonic committed-output
assumption. The current native input validator rejects that path before scheduler
admission. Do not claim its support or merely suppress the accounting exception.
Broader topology still requires its own verified synchronization adapter.
The overall goal remains active.
