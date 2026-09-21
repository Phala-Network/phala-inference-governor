# Governor finite numeric input regression

An authenticated PATCH with a 1001-digit integer TPS reference fits inside the
4096-byte body limit but previously raised uncaught OverflowError in
math.isfinite. The Python ABI wrapper and scheduler admin operation shared the
same failure. HTTP now reports invalid_request/400 before scheduler dispatch;
all three paths use the same finite nonnegative double validation.

Red evidence: original runtime source with new regressions produced two FFI
subtest errors (positive/negative unrepresentable integers), and one HTTP error.
Green evidence: 4 FFI methods, 14 HTTP methods and 2 routing methods passed.
Checks cover no scheduler dispatch for invalid HTTP input, unchanged policy
revision/reference and observation history, existing CAS and authentication.
This historical numeric-input result predates the 2026-09-21 mutable
`max_running`/`max_waiting` admission policy; it remains evidence only for the
numeric validation paths it exercised.

Execution used remote805, fixed C4 image
sha256:c4fe40487178fd7738600562c114e2198d281d5ca6c0f5a265019369f6af76c4,
runc, GPU void, network none, one CPU. The real existing Rust library was reused
only after its source lib.rs compared equal to this source; library hash is in
the evidence. No Rust or ABI change was made.

The first routing run imported the CPU image's unpatched SGLang and failed the
source endpoint presence assertion (middleware test passed). That failed log is
preserved. The corrected run imported an offline Git archive of composed engine
828500b641b57a3e67508aad327397657f8be1f9 and both routing methods passed.
This is source-bound CPU validation, not installed final-image or live serving
acceptance. No build, publication, deployment or model request was performed.

Evidence: governor-numeric-evidence-r1.tgz
SHA-256: 5d4c4a0464cbff1fe6557988f97069c38b350ff46e60dc9d8e383774ffb43f05.
