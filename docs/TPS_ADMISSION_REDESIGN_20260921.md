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
- a count-only `running_limit`, waiting limit or TAIL semaphore used instead of
  the TPS forecast: counts remain secondary safety bounds and cannot approve a
  request whose physical projected TPS cell is unsafe or unknown.
- one admission credit per poll or a 500 ms pacer: it limits growth rate, not
  final Decode concurrency, and can still accumulate delayed waiting.
- a decision made after Router/SGLang metrics observe the new request: the
  request is already accepted and cannot drive fallback.
- active startup completions: cold JIT and warm backend runs produced different
  policy; production startup must not send synthetic inference.
- a simulator that counts the same request as both running and waiting.

## Product contract

- `PIG_TPS_REFERENCE`, `PIG_MAX_RUNNING` and `PIG_MAX_WAITING` default to
  `50`, `43` and `3` for the Qwen3.8-27B deployment. All three remain
  atomically hot-updatable through authenticated `GET/PATCH
  /admin/v1/predictive-policy` with one epoch/revision CAS.
- The reference is an average per-user Decode TPS target with normal variation,
  not a per-request or per-token deadline.
- `max_running` is the logical admission capacity boundary and cannot exceed
  immutable `native_max_running_requests`. `max_waiting` is a hard post-admit
  backlog bound with an implementation ceiling of `3`. The hard decision uses
  the reservation ledger; the Scheduler also supplies its real ordinary queue,
  grammar queue and pending chunk count for telemetry. Queue population, queue
  time and TTFT do not independently change the decision.
- TPS remains the first gate. A request is rejected when its conservative
  physical post-admit TPS forecast is below the reference or its evidence is
  unqualified. A TPS-fit request is then rejected when its projected waiting
  backlog exceeds `max_waiting`.
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
waiting_before = ordinary_waiting + grammar_waiting + pending_chunk
logical_projected_waiting = max(0, outstanding_after - max_running)
projected_waiting = logical_projected_waiting
candidate_context_upper =
    exact_tokenized_input + validated_max_new_tokens
projected_pressure_class =
    conservative class of admitted work plus candidate_context_upper
```

`native_max_running_requests` describes the immutable engine runnable range and
selects the TPS response-surface cell. Mutable `max_running` never selects a
lighter TPS cell; it only defines where admitted work becomes projected waiting.
This separation prevents a hot-lowered logical limit from approving a heavier
physical state with evidence from a lighter state.

The check, decision, reservation commit and native enqueue are one Scheduler
transaction. A rejected request commits no reservation. Batch inputs are
decided sequentially in that same owner, so an earlier fit is visible to the
next candidate. Grammar, multimodal and ordinary requests share the same
ledger. A retracted request already owns a reservation and is never admitted a
second time.

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
TPS fit and logical_projected_waiting > max_waiting  -> waiting_limit / 429
```

The waiting branch runs only after TPS fit, so a request that fails both gates
retains TPS provenance. `max_waiting=3` allows post-admit waiting values 0..3;
the next candidate returns 429 without a reservation. Lowering either logical
limit does not abort accepted requests; new admission remains closed until the
ledger drains inside the updated bounds.

## Cold, stale and identity behavior

Production does not invent an online N+1 curve. The initial conservative
service surface is generated on the authorized C2 while Redpill is disabled,
using the exact composed image, model/revision, GPU class, topology, KV dtype,
speculative configuration, native runnable range and Governor predictor
version. The resulting profile is reviewed, hash-pinned and loaded as data;
production startup sends no completion request.

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
request; its `is_retracted=True` path reuses the same reservation. Grammar or
ordinary-queue handoff failure releases the reservation before propagating the
error, and native priority/queue rejection releases it exactly once.

Governor opt-in is limited to generation models. A defensive embedding request
on a Governor-enabled generation scheduler returns HTTP 400 without entering a
native queue. `beam_width > 1` is rejected before beam-group initialization: the
current response surface and reservation ledger describe one native Decode row
per request and cannot safely account for internally spawned beam rows.

The 429 abort uses native `AbortReq`/finish-reason propagation so HTTP and
streaming clients receive the ordinary OpenAI error mapping. Tests must prove
that rejection leaves waiting, grammar, cache-prefetch, KV allocator, dispatch
and reservation state unchanged.

## Red/green gates

1. A test must first fail the current `aggregate/(N+1)` implementation: equal
   current aggregate TPS with different target-cell histories yields different
   decisions.
2. A safe cell is admitted through projected waiting 3 and rejected at 4 with
   distinct `waiting_limit` provenance; an unsafe/unknown TPS cell still wins
   provenance when both gates fail.
3. Missing/stale/mismatched target evidence is observable `unknown`; reference
   zero can collect evidence; CAS 0->50 preserves it.
4. Same-loop batch admissions see committed reservations. Rejection changes no
   native queue, grammar queue, prefetch/KV state or dispatch counter. Grammar,
   prefetch, priority and queue-limit handoff failures release exactly once.
5. Grammar, multimodal, parallel-sampling, batch, retraction, completion,
   cancel, timeout, disconnect, error and shutdown paths leave zero reservations
   and zero active Decode exposure.
6. Direct SGLang and SGLang-through-TAIL preserve the 429 status/body. A
   fallback fixture proves Redpill advances to the next backend only on 429.
7. Rust formatting/tests, Python/ABI/native focused tests, lifecycle/race tests,
   deterministic simulations and a clean Linux builder all pass from one exact
   source archive.
8. Non-generation startup, defensive embedding input and beam search fail closed
   with HTTP/startup errors and no admission or native resource ownership.
9. The composed image is published only as `ghcr.io/phala-network/sglang`, then
   tested on the sole authorized C2 with Redpill disabled. C2 must show real
   pre-enqueue 429, no dispatch for rejects, CAS preservation, protocol and
   attestation gates before route restoration or any later fleet rollout.

TAIL remains a thin transport/authentication/attestation layer. Legacy Guard
and the discarded static TAIL semaphore are not part of this design.

## Implementation status (2026-09-21)

The local source candidate is package 0.2.1 with C ABI v4 and a bounded
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
- `tps_reference`, `max_running` and `max_waiting` share one Scheduler-owned
  epoch/revision CAS. `max_running` is bounded by the native runnable range;
  policy updates preserve the response surface and existing reservations.
- Production startup with a positive reference requires a hash-pinned,
  unexpired profile whose exact runtime identity matches all resolved engine,
  model, hardware and scheduler fields. Reference zero may start without it.
- `GET /admin/v1/predictive-profile?expected_epoch=<epoch>` exports a strict,
  non-cacheable envelope containing coverage and a loadable profile document.
- A runtime/model identity change clears live/prior evidence and rotates the CAS
  epoch while preserving request-attached lifecycle accounting.
- The prior ABI v4 hook was generated from complete engine parent
  `5871a82cb0ef1221126b8c9712881e497041bd38` and committed as `a43a9d30...`
  with tree `0dacab8...`. Its first September 21, 2026 Linux record is
  historical and does not cover the current waiting, generation/beam or handoff
  repairs.
- The current deterministic hook is
  `52087d47d575c95351e17e5335ac6ca5a3d5b6bb63548438502521ca524117ea`.
  It applies directly to `5871a82...` and produces clean complete-engine commit
  `354d47922eafa95ebc2d7bd63b7627d780a01c26`, tree
  `2733a3bae11d56e4bf3f694fbb49f8e9e156086d`. The r2 Linux record binds the
  tested Governor source `74e981760572420329468eef663e5dfcf4ba8f6b` and the
  28 Rust, 133 Governor/FFI/SGLang and 10 native pre-header tests.

GPU verification, immutable image composition and the authorized C2 offline
rollout remain pending. GitHub CI is a final gate after local and builder checks;
it is not the development test environment.
