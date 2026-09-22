# Native running policy candidate — 2026-09-22

The installed `354d47922eafa95ebc2d7bd63b7627d780a01c26` engine accepted
Governor `max_running` CAS updates but kept allocating native running slots
against physical capacity. The CPU reproduction changed 43 to 1 and observed
43/42/41 allocatable slots for 0/1/2 running requests rather than 1/0/0.

This candidate clamps actual scheduler slots, passes the mutable limit to
prefill negotiation, and makes the new-owner cap non-bypassable by priority
preemption. Successful cap changes clear the single supported scheduler's
stale `batch_is_full` flag; rejected CAS leaves that flag unchanged.
Lowering the cap drains existing work without aborting it. An existing chunked
owner retains the continuation exception and physical KV budget checks, while
additional waiting owners remain blocked. Retracted work uses the same ordinary
queue selection cap and retains its reservation.

The physical `Scheduler.max_running_requests`, adapter
`native_max_running_requests`, forecast concurrency ceiling, runtime identity,
and profile identity remain unchanged. TP1/PP1/DP1, non-overlap and no
disaggregation remain required. HiSparse, PD multiplexing and diffusion
scheduler paths now fail closed when Governor is enabled because they can
bypass ordinary prefill selection; this candidate does not qualify those modes.

`test_native_running_policy.py` exercises real scheduler slot calculation,
selection and msgspec CAS dispatch with the actual Rust controller. GPU batch
allocation and telemetry are mocked. The separate real `PrefillAdder` test
checks chunk continuation despite a delayer denial, and stops continuation when
physical KV slack is exhausted. Existing adapter tests verify forecast
concurrency remains physical after policy changes.

The full three-file hook cleanly applies to the declared serving parent.
The resulting local engine commit is
`e02dfa1256c5d7d6e8b731d439229d5aca72cbe5`, tree
`ae0e7307e3dcf2314b7eb51e26a17207073e0f0a`. Its scheduler is byte-identical
to the development candidate. The full hook remains independently replayable;
an incremental scheduler-only patch is also provided alongside the extracted
installed source under the task's supplemental evidence directory.

Local checks: scheduler-only full-patch reverse/replay is byte-exact with
`core.autocrlf=false`; candidate and test syntax parse; both patch-manifest
tests pass. The first CPU source-overlay suite ran 151 tests: 150 passed and
one new identity-preservation fixture omitted required identity fields.
That fixture has been corrected; the combined suite must pass before freezing
the candidate. No image build, immutable-image qualification, GPU acceptance
or production acceptance is claimed.
