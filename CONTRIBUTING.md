# Contributing

Maintain Governor's Rust core, Python adapter, policy API and minimal SGLang
hooks here. Use meaningful controller/integration regressions. Governor never
owns native request/KV/cache cleanup or TAIL attestation.

Implement independent serving fixes in real source files in
Phala-Network/sglang, one problem and regression per PR. Deterministic patch
exports, applicability, explicit model profiles and dependency order belong to
sglang-serving-patches. Do not hand-edit the same serving implementation here.

A generated engine branch may expose Governor hooks as readable source diffs,
but it is a derived view; the hook's human-maintained source remains here.
Deployment configuration in phala-models-compose pins the independent sources
and publishes composed images under ghcr.io/phala-network/sglang. It records the
exact final engine tree and separates prebuild inputs from postbuild image
digests.

Keep upstream, patch-set, Governor and model-profile versions distinct. Preserve
historical tags and artifacts; never force-update them to rename ownership.
Qualified TP1/PP1/DP1/non-overlap behavior does not establish broader support.
Select actual maintainers for CODEOWNERS only after verifying responsibility;
do not inherit unrelated upstream ownership rules automatically.
