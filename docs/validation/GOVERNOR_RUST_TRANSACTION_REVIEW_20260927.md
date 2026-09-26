# Rust observation transaction review, 2026-09-27

## Finding and change

The C ABI `transaction()` already clones `State` before invoking a fallible
operation and publishes the clone only on success. The Decode observation
methods cloned it again; replacement cloned once more through batch. This
duplicated copying of the rolling buckets and response-surface map on the
observation path.

The C ABI now calls in-place observation methods inside its existing
transaction. Test-only direct `State` wrappers retain atomic failure behavior
for Rust unit tests. The ABI, policy, response-surface rules and output format
are unchanged. A late-overflow regression exercises all three FFI observation
entry points and checks that their failures publish no partial history.

## Exact validation

- Base Governor commit: `7ba058ea647ad64efa07bf25a06ccbfc2b6cb593`.
- Reviewed `rust/src/lib.rs` SHA256:
  `ae80bf2307a44e89e9e7291f3c4403a970a9888bc8218a6e30dbcd0b8364c9fa`.
  Local and remote copies matched before the final run.
- Remote CPU builder: `phala@10.80.10.201`, fixed image ID
  `sha256:c4fe40487178fd7738600562c114e2198d281d5ca6c0f5a265019369f6af76c4`,
  Docker `--network none --cpus 1`; isolated source under
  `/opt/dstack/models/governor-rust-review-20260927`.
- `cargo test --locked --offline --quiet`: 51 passed, 0 failed; exit 0.
- `cargo build --release --locked --offline --quiet`: exit 0. Resulting library
  SHA256 `7fb2f5dd154e7cfdfb55709955fa001c6317a574cd435c86c93521a74940be44`.
- Against that library, `python3 -m unittest discover -s tests -p test_ffi.py -q`:
  22 passed, exit 0.
- Local `cargo fmt -- --check`, `cargo check --tests` and `git diff --check`:
  all exit 0.

## Scope and handoff

This removes redundant state copies by inspection of the C ABI call graph;
no serving latency or throughput claim is made without a paired measurement.
The ongoing R5 image uses Governor `219ca9a` and does not contain this source
change. Integrate and retest this later Governor revision as a separate image
input after the current R5 qualification. Historical integration CI against
engine `f1a2a743` fails its 429 response-format expectations; its `core-tests`
job passed on `7ba058e`, and that older integration result is not acceptance
for the repaired R5 engine or this new source revision.
