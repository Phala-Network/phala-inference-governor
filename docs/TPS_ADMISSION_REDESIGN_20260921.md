# Native Governor TPS-first admission redesign

Status: active implementation baseline for the 2026-09-20 incident repair.
This document is normative for the admission path. The average TPS target is a
soft product objective; the admission decision uses a conservative
counterfactual so overload can return 429 before SGLang owns more work.

## Why the deployed algorithm failed

The failure was architectural before it was a tuning error.

1. The old Guard path performed `forecast -> decision -> atomic reservation ->
   forward or 429`. Replacing Guard with the intentionally thin TAIL removed
   that admission owner. TAIL correctly forwarded every authenticated request.
2. Native Governor was connected only to `before_prefill`. That hook runs
   after a request has entered SGLang's waiting population. It can briefly
   prefer Decode, but it cannot undo enqueue or produce an HTTP 429.
3. The Rust `choose` path received no candidate request, no post-admit state,
   no waiting/reservation ledger and no response surface. It was scheduling
   advice, not an admission predictor.
4. The exported `average_tps` was `decode_tokens / active_sequence_seconds`.
   Waiting requests were absent from the denominator. A node could therefore
   report a high value while hundreds of accepted requests waited behind the
   measured Decode set.
5. On `qwen3-8-27b-usc2-0f`, `max_running_requests=48` and
   `max_queued_requests=None`. During the incident, native waiting reached 232
   while 40 requests were running. SGLang and HAProxy returned zero 429s, so
   Redpill's fallback-on-429 policy kept selecting the same node.

The following previously attempted models are rejected:

- `current aggregate TPS / (N + 1)`: it assumes aggregate throughput remains
  constant at an unobserved state and that a waiting candidate immediately
  decodes. Section 5.4 of the v0.12.5 plan explicitly rejects it.
- a fixed `running_limit`, inflight limit, waiting limit or TAIL semaphore as the
  sole controller: each substitutes a count for the requested dynamic TPS
  forecast. Governor now layers explicit running/waiting safety bounds after the
  TPS decision; those bounds do not replace the forecast.
- one admission credit per poll or a 500 ms pacer: it limits growth rate, not
  final Decode concurrency, and can still accumulate delayed waiting.
- a decision made after Router/SGLang metrics observe the new request: the
  request is already accepted and cannot drive fallback.
- active startup completions: cold JIT and warm backend runs produced different
  policy; production startup must not send synthetic inference.
- a simulator that counts the same request as both running and waiting.

## Product contract

- `PIG_TPS_REFERENCE` defaults to 50 for the Qwen3.8-27B deployment and remains
  hot-updatable through authenticated `GET/PATCH
  /admin/v1/predictive-policy` with epoch/revision CAS.
- The reference is an average per-user Decode TPS target with normal variation,
  not a per-request or per-token deadline.
- `PIG_MAX_RUNNING` and `PIG_MAX_WAITING` are hard admission bounds and are
  hot-updatable through the same authenticated CAS API. Production defaults are
  43 and 3. New admission must also respect the actual ordinary/grammar/pending
  chunk backlog. Queue age and TTFT are not admission conditions.
- TPS is evaluated first. A request is rejected when its conservative post-admit
  TPS forecast is below the reference, the evidence needed for that forecast is
  explicitly unqualified, or a TPS-fit admission would exceed the configured
  running/waiting bounds. The reasons remain distinct and observable.
- A rejection is an OpenAI-compatible HTTP 429 before waiting/grammar queue
  insertion, KV allocation, cache prefetch or worker dispatch. TAIL forwards
  the status and body unchanged; Redpill then performs its existing fallback.
- A fit request may wait. SGLang remains the owner of Req, KV/cache, worker
  synchronization and native cleanup.
- Prompt tokens never enter the Decode numerator. Requested output length is
  a prospective feature, not a completed-token observation.

## Counterfactual state and atomic reservation

The Scheduler is the single admission owner. Every validated generation gets
one request-attached reservation before either the grammar queue or ordinary
waiting queue can own it. The owner maintains bounded aggregate counters; it
does not scan an unbounded queue on every arrival.

For a candidate, the owner constructs:

```text
outstanding_after = admitted_nonterminal + 1
projected_decode_concurrency =
    min(outstanding_after, native_max_running_requests)
logical_projected_waiting =
    max(0, outstanding_after - policy_max_running)
native_waiting_before =
    ordinary_waiting + grammar_waiting + pending_chunk
projected_waiting =
    max(logical_projected_waiting, native_waiting_before + 1)
candidate_context_upper =
    exact_tokenized_input + validated_max_new_tokens
projected_pressure_class =
    conservative class of admitted work plus candidate_context_upper
```

`native_max_running_requests` describes the engine's physical runnable set and
continues to select the TPS response-surface cell. `policy_max_running` is the
separate mutable hard policy bound. Once `outstanding_after` is above the native
value, another waiting request does not by itself increase projected Decode
concurrency, though it can increase `projected_waiting` or change the pressure
class. Native waiting is supplied synchronously by the Scheduler before queue
insertion; it is not a delayed polling metric. Spare logical running slots do
not exempt a new request from the native waiting bound. Three queued arrivals
followed by a fourth arrival in the same scheduler cycle therefore reject the
fourth request before insertion.

The check, decision, reservation commit and native enqueue are one Scheduler
transaction. A rejected request commits no reservation. Batch inputs are
decided sequentially in that same owner, so an earlier fit is visible to the
next candidate. Grammar, multimodal and ordinary requests share the same
ledger. A retracted request already owns a reservation and is never admitted a
second time. Already-admitted work is not aborted by a hot policy reduction;
new admission remains closed while existing ownership exceeds the new bounds.
Internal retraction reuses ownership and is not a new client admission.

Terminal completion, validation rollback, queue-limit eviction, cancellation,
disconnect, timeout, upstream/internal failure, grammar abort and shutdown all
release exactly once. Repeated abort output is idempotent. Request-attached
state prevents a RID prefix match, retry or retraction from double releasing.

## Trusted response surface

Governor predicts the target state from measurements made at that state. It
does not transform a current aggregate rate into an unobserved N+1 rate.

The bounded surface key is:

```text
(runtime/profile identity,
 projected Decode concurrency bucket,
 context-pressure/request class)
```

Concurrency is exact across the native runnable range used by this deployment.
Context pressure uses a small fixed set of token-horizon classes. The profile
format and array sizes are bounded; overflow is rejected during construction.

For each actually observed state, the core accrues:

- exact committed Decode-token deltas, excluding the first output token;
- actual sequence-seconds while that state was active, including Prefill
  interference between Decode results;
- short and long bounded windows;
- sample/exposure mass and last qualified time.

The per-user rate for a cell is therefore
`completion_decode_tokens / sequence_seconds`. This is measured directly; it
is not aggregate TPS divided by a projected population. The cell forecast is a
conservative lower bound from the lower of fresh short/long evidence and the
bounded lower residual quantile. Insufficient, stale, invalid, duplicated or
identity-mismatched observations cannot raise a forecast.

Admission uses the exact projected cell when qualified. If it is absent, only
the nearest *heavier* qualified cell may supply a conservative bound. Evidence
from a lighter concurrency or pressure cell never extrapolates upward. Live
evidence can reduce the bound immediately; recovery above the identity-bound
prior requires the configured minimum exposure and remains multiplier-bounded.

The decision is:

```text
reference == 0                                      -> fit (offline observation)
qualified projected-cell lower bound >= reference  -> fit
qualified projected-cell lower bound <  reference  -> tps_risk / 429
no qualified projected-cell evidence               -> unknown / 429
TPS-fit projected_waiting > max_waiting             -> waiting_limit / 429
```

The TPS branch does not use native waiting telemetry as a performance proxy.
After a fit TPS result, the reservation ledger and current native backlog
jointly enforce the configured hard waiting bound. Conversely, `waiting == 0` does not bypass a low target-cell
forecast, and a TPS-risk rejection is never relabeled as a count rejection.

`max_waiting=0` pauses new native admission: every new request requires an
initial queue handoff. It does not silently permit cold arrivals or mean an
unlimited queue. Existing requests continue; authenticated CAS can restore a
positive value without restarting the backend. Reference zero disables only
the TPS forecast gate, not the waiting bound.

## Cold, stale and identity behavior

Production does not invent an online N+1 curve. The initial conservative
service surface is generated on the authorized C2 while Redpill is disabled,
using the exact composed image, model/revision, GPU class, topology, KV dtype,
speculative configuration, native runnable range and Governor predictor
version. Static identity fields match exactly. The probed `max_total_tokens`
value is a minimum capacity: a current runtime may reuse the profile only when
its actual capacity is at least the sampled value. The resulting profile is
reviewed, hash-pinned and loaded as data; production startup sends no completion
request.

`tps_reference=0` is the explicit offline observation mode used to populate
otherwise unseen cells. The final C2 state is hot-updated to 50 through the
same CAS API without clearing the surface. A profile missing required cells,
stale beyond its contract, or mismatched to runtime identity is `unknown` and
cannot grant headroom. Production rollout is blocked by that condition rather
than silently substituting a fixed concurrency.

Live history survives TPS reference updates. A backend/model identity change
starts a new surface epoch; old cells cannot relax admission. Low live evidence
ages out without creating token credit. Recovery requires fresh real Decode
evidence or a still-valid conservative profile and does not require a restart,
cooldown or unrelated business request.

## SGLang hook placement

Admission is after request construction, multimodal expansion and normal input
validation, but before `grammar_manager.process_req_with_grammar(req)`. Error
requests already marked terminal bypass Governor and use native error output.
`_add_request_to_queue` verifies an existing reservation for a new valid
request; its `is_retracted=True` path reuses the same reservation.

The 429 abort uses native `AbortReq`/finish-reason propagation so HTTP and
streaming clients receive the ordinary OpenAI error mapping. Tests must prove
that rejection leaves waiting, grammar, cache-prefetch, KV allocator, dispatch
and reservation state unchanged.

## Red/green gates

1. A test must first fail the current `aggregate/(N+1)` implementation: equal
   current aggregate TPS with different target-cell histories yields different
   decisions.
2. Waiting up to the configured bound with a qualified safe projected cell is
   fit; the next TPS-fit admission is `waiting_limit / 429`. Waiting zero with a
   qualified unsafe target cell is still `tps_risk / 429`.
3. Missing/stale/mismatched target evidence is observable `unknown`; reference
   zero can collect evidence; CAS 0->50 preserves it.
4. Same-loop batch admissions see committed reservations. Rejection changes no
   native queue, grammar queue, prefetch/KV state or dispatch counter.
5. Grammar, multimodal, parallel-sampling, batch, retraction, completion,
   cancel, timeout, disconnect, error and shutdown paths leave zero reservations
   and zero active Decode exposure.
6. Direct SGLang and SGLang-through-TAIL preserve the 429 status/body. A
   fallback fixture proves Redpill advances to the next backend only on 429.
7. Rust formatting/tests, Python/ABI/native focused tests, lifecycle/race tests,
   deterministic simulations and a clean Linux builder all pass from one exact
   source archive.
8. The composed image is published only as `ghcr.io/phala-network/sglang`, then
   tested on the sole authorized C2 with Redpill disabled. C2 must show real
   pre-enqueue 429, no dispatch for rejects, CAS preservation, protocol and
   attestation gates before route restoration or any later fleet rollout.

TAIL remains a thin transport/authentication/attestation layer. Legacy Guard
and the discarded static TAIL semaphore are not part of this design.

## Implementation status (2026-09-22)

The local source candidate is package 0.2.2 with C ABI v4 and a bounded
response surface:

- `pig_governor_observe_surface(now, delta, sequence_seconds, concurrency, pressure_class)`
  records real Decode evidence in the target cell. The mass is assigned to the
  observation bucket; it is never treated as elapsed wall-time or capped at 60.
- `pig_governor_admit(now, projected_concurrency, pressure_class)` consults the exact
  projected cell first, then only a jointly heavier qualified cell.
- The Scheduler keeps `outstanding/admitted_pressure_counts` separate from
  `active/active_pressure_counts`. Waiting requests can change the forecast, but
  cannot be recorded as runtime evidence.
- `SglangGovernor.after_result()` commits one forward batch before publishing
  active-state changes, so tokens are attributed to the batch-start cell.
- A mixed-pressure batch is recorded once at its aggregate batch-start pressure
  class, using total completion tokens and total sequence-seconds rather than
  splitting the same forward pass into per-request cells.
- `pig_governor_observe_batch()` commits the surface cell and aggregate window in
  one controller transaction. The surface receives the caller's complete
  sequence-second mass, while the aggregate window accrues only active time not
  already advanced by `snapshot`, `choose`, or admission, so an intermediate
  read cannot count the same exposure twice.
- `pig_governor_new()` receives the native max running request bound; surface
  observations and admission forecasts above that deployment bound are invalid.
- Surface qualification uses exposure in the current evidence window. A
  positive token delta with zero sequence-seconds is invalid and cannot enter
  either TPS numerator; the Scheduler buffers same-timestamp tokens in a
  bounded response-cell ledger and only joins them to positive exposure from
  that same cell within the 60-second evidence window. A zero-token/zero-duration event cannot
  refresh qualification or extend stale age.
- A queued or active abort releases its reservation whenever Governor progress or
  a reservation exists, so cancellation before first Decode cannot leak state.
- Admission is called before `grammar_manager.process_req_with_grammar(req)`.
  `_add_request_to_queue()` only verifies that a valid request already carries a
  reservation; retraction reuses that reservation.
- `reference=0` is the offline observation mode. A CAS update to 50 preserves the
  surface.
- Production startup with a positive reference requires a hash-pinned,
  unexpired profile whose engine, model, hardware and static scheduler identity
  fields match exactly, and whose sampled `max_total_tokens` does not exceed the
  current runtime capacity. Reference zero may start without it.
- `GET /admin/v1/predictive-profile?expected_epoch=<epoch>` exports a strict,
  non-cacheable envelope containing coverage and a loadable profile document.
- A runtime/model identity change clears live/prior evidence and rotates the CAS
  epoch while preserving request-attached lifecycle accounting.
- The ABI v4 SGLang hook patch was regenerated deterministically from complete
  engine parent `5871a82cb0ef1221126b8c9712881e497041bd38`, replayed cleanly and
  committed as `a43a9d30eb9879ec54c607d1d2edfdb5c536bb07` with tree
  `0dacab8ac525e59aa02875bfd83c1157637a4bc9`.

The Linux builder, full SGLang lifecycle/real-HTTP-429 suite and native common
HTTP propagation regression passed on September 21, 2026. Remaining gates are
GPU verification, image composition and the authorized C2 offline rollout.
GitHub CI is a final gate after local and builder checks; it is not the
development test environment.
