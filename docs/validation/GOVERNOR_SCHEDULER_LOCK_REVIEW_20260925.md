# Governor scheduler lock review, 2026-09-25

## Finding and change

`SchedulerGovernor.commit_batch()` mutated `active`, pressure counters,
pending evidence, and progress objects without the `_policy_lock` used by
admission, policy CAS, and management snapshots. A concurrent `/v1/loads` or
policy operation could therefore observe a partially committed batch while the
native scheduler was publishing it.

The method now takes `_policy_lock` and delegates the existing mutation body to
`_commit_batch_locked()`. The native core lock remains unchanged, and the
existing mutation order is preserved. A focused regression test blocks the core
observation call and proves that `policy_snapshot()` waits until the batch
commit releases the scheduler lock.

## Verification

- Source: Governor `e5ced35` plus this local review change.
- Remote environment: GPU805 `805bfe7c-00f5-4e14-995a-8f25631b7703`, fixed CPU
  image ID `sha256:c4fe40487178fd7738600562c114e2198d281d5ca6c0f5a265019369f6af76c4`,
  Docker `--network none`, one CPU, no GPU.
- Scheduler regression: `40 passed`.
- Matching-engine focused suites (`test_scheduler.py`, `test_sglang.py`,
  `test_native_running_policy.py`, `test_native_scheduler.py`): `88 passed`,
  `26 subtests passed`, 16 warnings, exit 0.
- An initial test run reported one failure because the new test asserted a
  field absent from its fake core; the fixture assertion was corrected and the
  same container rerun passed. No implementation failure remained.

## Boundary

This is a Python scheduler lifecycle safety fix. It does not change the Rust
response surface, admission thresholds, profile format, or SGLang source
combination. The final unified image and compatible remote integration CI still
need to consume this commit. GPU805 serving containers were not restarted or
replaced.

## Follow-up Rust boundary review, 2026-09-25

The C ABI observer accepted `active_after` above the configured native running
capacity in `observe` and `observe_batch`. The direct observer also advanced its
clock before an overflow or other late validation error could be returned. The
observer now validates the capacity and commits through a cloned state, so a
rejected call leaves the clock and evidence unchanged. The batch path applies
the same capacity guard; replacement's zero-exposure path uses the in-place
helper to avoid a redundant clone.

- `cargo check --tests`: passed on Windows.
- `cargo test`: could not link locally because the MSVC `link.exe` tool is not
  installed; this is an environment limitation, not a test failure.
- Python scheduler regression suite: `40 passed` with `PYTHONPATH=python`.
- The new Rust regression covers both observer entry points and atomic rollback;
  it still requires the fixed Linux/GPU805 image for executable Rust testing.
