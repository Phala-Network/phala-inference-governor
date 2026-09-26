# Scheduler ownership and batch accounting review, 2026-09-27

## Findings and fixes

- `admit_request()` reused an attached reservation before checking its owner.
  A request admitted by a different Governor instance could therefore bypass
  the current instance's forecast and reservation ledger. It now rejects that
  owner mismatch before returning a cached decision.
- `commit_batch()` accepted the same `Progress` object twice in one batch,
  counting its enter/leave and token delta twice while publishing the object
  only once. It now rejects duplicate object identities before core observation.
- A batch could supply a pressure class different from the class bound to its
  `Progress`, corrupting active pressure counts on entry or exit. The supplied
  class must now match the bound class.
- The matching R5 engine's `PrefillAdder` reads the allocator page size. A
  native-running-policy test fixture constructed with `__new__` now supplies
  that field; its behavioral assertions are unchanged.

The duplicate-progress and foreign-owner regressions first failed against the
prior implementation. The three new scheduler tests verify that rejected
inputs do not change the core, reservation ledger, active counts, or progress
state.

## Validation

- Base Governor commit: `6fedae879b48f425a880f3f6b64e3fa47456afda`.
- CPU builder: `phala@10.80.10.201`; final installed test image
  `sha256:4cda8f4e747741cee11a21ec7ae4eef69a7fd25dff78dc7b03d2ca7d1b27af58`.
  Container used `--network none --cpus 1 --read-only` with private tmpfs for
  `/tmp` and `/root/.cache`, and a read-only source bind mount.
- Exact focused command in the container:
  `python3 -m unittest tests.test_scheduler tests.test_sglang tests.test_native_running_policy tests.test_native_scheduler -q`
  with `PYTHONPATH=/review/python:/review/tests`: 91 tests passed, exit 0.
- Local `python -m unittest tests.test_scheduler -q`: 43 passed, exit 0.
  Local SGLang suites cannot import without the pinned engine dependencies.
- `git diff --check`: exit 0.
- Local and builder SHA-256 matched for `scheduler.py`
  (`f4f2143654738467ebc42671a2bfab8ab03a0a854530510ec3ddf674d072a84e`),
  `test_scheduler.py`
  (`e4c7447c5fb9364649ac1246e2ce1517e2945ea4e2cb3c10586dc8c5398189d3`),
  and `test_native_running_policy.py`
  (`78d6da8fc437cc6139d33e145a5e984a8425ba405935c2359fb47a6693d82a95`).

## Boundary

The R5 image contains Governor `219ca9a`, not this revision or the preceding
Rust transaction optimization. The passing CPU tests verify the revised source
against the matching R5 engine; they do not establish a rebuilt image, GPU
behavior, serving performance, or R5 enabled acceptance. The GPU805 enabled
gate's frozen-profile identity mismatch is being investigated separately.
