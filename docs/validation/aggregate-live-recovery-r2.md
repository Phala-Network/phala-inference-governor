# Aggregate live recovery follow-up

2026-09-24. Development CPU evidence only; no final-image or GPU qualification.

The surface recovery change in `33fafd6` did not update
`State::aggregate_live_lower_bound`. Consequently the `949f1ec` installed
native library could reject a healthy short window because a cold transient
remained in its long window. The earlier r1 receipt concerns surface recovery;
its test count alone is not evidence of aggregate recovery.

The follow-up in `2562a97` applies the existing qualified-short-window admission
rule to the aggregate veto. Profile export remains conservative. Unqualified
bursts retain the previous conservative fallback. No policy defaults, hard
limits, request accounting, or cancellation handling change.

The regression keeps one active sequence and feeds `(time, tokens)`:
`(1,5), (1.5,5), (2,55), (2.5,55)`, then admits projected concurrency two
at time 4 with a safe 56 TPS prior and target 50.

- Original installed native `2435f62892776934e749e16780fd2ccf7f0363cbcd8646adac832fb01db251b0`
  rejects with aggregate reason6 and 30 TPS.
- Development native `7a9de412eef37e379ae34424767048aee366e564dbfdccc8225a795371419401`
  admits using the 56 TPS prior after aggregate recovery.
- A control with four five-token samples continues to reject with reason6
  and 5 TPS. The cross-cell low-rate bypass test also continues to reject;
  its numeric forecast now reflects the qualified short window.

An additional real-clock boundary regression checks that 0.05 seconds of
active exposure does not engage the aggregate veto, while 0.1 seconds with
the same one-token history rejects at 10 TPS. No bucket state is synthesized.

On the authorized remote CPU builder, 45 Rust tests and 170 Python/SGLang
tests passed. Rust builder:
`sha256:2775a09d208ff0d7c1f50490c45b62db929e87ba1dcbc3f2132ac71a704bcdd3`.
Python/SGLang environment:
`sha256:b4c43ad6601180f57f31193f19439a0583c922d31b4567a9137a152374074b67`,
with the development library explicitly selected.

Verified source SHA-256:

- `rust/src/lib.rs`: `60a47a8ce5157e433da09c527e3e7945fe62acf8e153a8cf520135e684d6cc43`
- `tests/test_ffi.py`: `618e9c4161ebd458e01d890ef9c366b6b27456973ab2cca72d45fc94a55b784d`

These hashes match local source and remote tested files. A future immutable
image must pass the same regression against its own installed library without
an overlay; development GREEN cannot be carried over by identity assertion.
