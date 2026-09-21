# SGLang v0.5.20 upgrade assessment

Assessment date: 2026-09-19. This records the initial evaluation. Subsequent
source migration and development runtime transition are recorded in
[live acceptance](validation/DEV_V0520_ACCEPTANCE.md); its current-state evidence
supersedes historical starting-state descriptions below.

Official release: https://github.com/sgl-project/sglang/releases/tag/v0.5.20
Published 2026-09-18 22:41:33 UTC (2026-09-19 06:41:33 Asia/Shanghai).
Peeled source commit: 94602c9c2b7cbdb8efd5c52802dac6a1c180089e.
Current development base: 1a56fbb0dc48ec3fb2b4b629fc4e74a3b57c639a.
There are 30 visible downstream commits relative to official v0.5.19. The source repository is shallow, so no absence-of-ancestry claim about v0.5.20 is made from a failed merge-base query; conclusions below use direct source comparisons.
The current base is a Phala development revision, not pristine official v0.5.19.

## Recommendation

Track v0.5.20 as the next development integration baseline, before spending a
full round of model qualification on the older plugin baseline. Keep the
accepted r44 serving predecessor until the new adapter, dependencies and actual
model behavior are verified. No measured Qwen3.8-27B TPS improvement is claimed.

Relevant release work includes EAGLE draft-extend output pruning (#35546),
speculative-worker prefill shared-read staging (#38554), Mamba deferred-init
cleanup (#37165), dispatched-request disconnect/abort handling (#35255), and a
CPU scheduler/cache simulator (#33824). These require comparison with Phala's
existing fixes; similar problem descriptions do not prove equivalent coverage.

The live development image already uses CUDA 13.0.3, so retirement of CUDA 12
release artifacts does not itself force this environment across CUDA major
versions. Actual dependency changes include sglang-kernel 0.4.6.post1 to 0.4.7,
sgl-deep-gemm 0.1.7 to 0.2.0, smg-grpc-servicer minimum 0.5 to 0.9, and
nvshmem4py-cu13. The fixed old CPU image is only an analysis environment; it
does not qualify these new runtime dependencies or GPU/CC behavior.

Responses storage is now opt-in via --enable-response-store (#39122).
Retrieval, previous_response_id chaining and background work need explicit
handling; ordinary non-stored Responses requests are a different case.

## Measured patch portability

The fixed 805 CPU environment ran git apply --check per patch file against the
pinned official source. CRLF introduced in the archived source was explicitly
normalized to LF before the decisive check.

- 19 runtime files: 11 apply; 8 fail, also with --ignore-whitespace.
- Four added regression-test files apply textually.
- The complete patch fails to apply.
- These are textual checks only, not import, semantic or runtime compatibility.

The eight runtime conflicts are serving_base.py, serving_chat.py,
serving_completions.py, serving_responses.py, scheduler.py,
tokenizer_manager.py, managers/utils.py and eagle_worker_v2.py.

Evidence: [dry-run report](validation/governor-v0520-patch-evaluation-r2.json).

## Semantic migration requirements

The Rust controller/C ABI has no identified mandatory redesign. Its interface
uses monotonic time, committed token deltas, active sequence counts and bounded
advice; it does not depend on SGLang request/cache/tensor types.

Python initialization must validate effective published settings through
get_parallel(), get_schedule() and get_disagg(). ServerArgs now preserves raw
operator input in a msgspec.Struct; directly reading raw fields is not a robust
effective-configuration check. HTTP authentication should use get_serving(),
matching the updated native middleware.

The post-result hook still belongs after native result processors and before
_record_step_counters. Req remains extensible and output_ids_through_stop retains
the required committed-output semantics. Keep native cache/chunk-abort processing
ahead of any Governor prefill-defer decision. The later 0.2.1 policy adds the
Scheduler-owned post-admit waiting gate without changing this ordering.

The request-correlated control_nonce fix is still needed: upstream
communicator.py has no equivalent correlation mechanism. Preserve payload types,
tokenizer opt-in and scheduler nonce echo together.

Tokenizer _send_one_request is now async and awaits preparation/dispatch
readiness. Port SHM/abort cleanup while preserving that ordering. New cache
attempt cleanup does not by itself prove multimodal release_features or the
complete generator-disconnect ownership fixes are covered. Worker metadata and
identity-grammar-mask fixes also need separate review; do not delete them solely
because the release mentions related features.

## Making later upgrades easier

Keep Governor core, version-specific adapter/hooks and independent serving fixes
separate. Split the current combined patch into Governor control/hooks, generic
cancellation/SHM cleanup, and worker/grammar patches so upstreamed fixes can be
retired individually. Pin tested SGLang commits and validate configuration,
CAS/cancellation correlation, committed-output accounting and native cleanup on
each supported version.

The official general plugin framework provides discovery and dynamic function
hooks/class replacement. Its existence does not provide a stable explicit
scheduler-lifecycle contract for Governor. Continue the current no-monkeypatch
design; seek small upstream lifecycle extension points rather than hiding
version dependence in dynamic method wrappers.

After adaptation, run native-module and control/cleanup tests, then the real
model/TAIL chain on the approved development CVM with exact recovery and three
fresh drains. Preserve TP1/PP1/DP1, non-overlap, no PD, soft average TPS and no
the authenticated mutable TPS/running/waiting policy. Broader topology support
remains separate work.


## Benefit boundaries and retained downstream work

EAGLE PR #35546 selects draft-extend logits rows and has direct mechanism relevance to the current EAGLE/full-decode-graph configuration. Its published +2.5 percent result is for 102K context per rank, not a measured result for this model. Mamba PR #37165 clears deferred prefill initialization metadata before speculative decode and is a concrete correctness item to test. The DFlash-specific Mamba checkpoint fix is not a benefit for the current EAGLE path. H200 figures for Qwen3.8-Flash-Next TP2/EP2 likewise do not describe Qwen3.8-27B TP1.

The current base already contains downstream disconnect-abort work with the #35255 title and strict-thinking/grammar changes. Review these for overlap rather than adding their advertised benefits twice. Existing Phala xgrammar wheel/source changes also need an explicit retain/drop decision against the new dependency set. Audit the 30 downstream commits and carry only necessary fixes; do not mechanically restore the old r44 experimental architecture.
