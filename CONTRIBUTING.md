# Contributing

Maintain Governor's Rust core, Python adapter, policy API and minimal SGLang
hooks here. Use meaningful controller/integration regressions. Governor never
owns native request/KV/cache cleanup or TAIL attestation.

Develop independent serving fixes in real source files and record them in the
single ordered stack maintained by sglang-serving-patches. Group changes and
reviews by coherent behavior and regression coverage; the release workflow does
not require one PR per patch. Keep common fixes single-copy, and place only true
model or family differences behind their relevant module/model/configuration.
Do not hand-edit the same serving implementation here.

Governor publishes its component source and minimal hook patch. Phala-Network/sglang
composes the ordered serving stack and pinned Governor input on the fixed upstream
baseline, producing the complete source commit/tree used for release. Deployment
configuration in phala-models-compose consumes that fixed complete source and
publishes composed images under ghcr.io/phala-network/sglang. It records the exact
final engine tree and separates prebuild inputs from postbuild image digests.

Keep upstream, patch-set, Governor and model-profile versions distinct. Preserve
historical tags and artifacts; never force-update them to rename ownership.
Qualified TP1/PP1/DP1/non-overlap behavior does not establish broader support.
Select actual maintainers for CODEOWNERS only after verifying responsibility;
do not inherit unrelated upstream ownership rules automatically.
