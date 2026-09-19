# Repository boundary

Phala Inference Governor is maintained in its own repository:
https://github.com/Phala-Network/phala-inference-governor

It is distinct from Phala Inference Guard. Governor owns the independent Rust
control core, thin Python bindings, explicit SGLang integration patches, and
their tests. It does not absorb Guard's history or its TEE/attestation layer.

The SGLang tree remains an upstream dependency. Keep the version-pinned patch in
`patches/sglang/`; do not vendor the full SGLang checkout or use runtime method
replacement to hide integration changes. Generic serving fixes are separately
identified in the validation report so they can be upstreamed or retired.

Development evidence from the earlier native-QoS experiments is retained outside
this repository. No old planning/qualification/admission/journal implementation
is required by the new core. The current public repository is a development
candidate, not a release or production qualification.
