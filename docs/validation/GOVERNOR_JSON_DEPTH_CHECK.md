# Deep JSON input check — no runtime change

On 2026-09-20, checked whether a 3001-byte, 1500-level nested JSON array could
escape the admin endpoint's validation exception handling. Against source
55e3aa20757a58c945048e286e27b6e0ae0db17c and the frozen composed SGLang source
828500b641b57a3e67508aad327397657f8be1f9, the probe returned HTTP 400,
performed no scheduler get/set call, preserved the policy and observation
snapshot, and allowed a subsequent valid CAS update. All 15 HTTP methods
including this temporary probe passed.

No defect was reproduced in this environment. The provisional exception-handler
change and temporary test were removed from the working tree. The test source
and actual log are retained only as investigation evidence; this is not a new
runtime fix or proof for every interpreter/environment.

Execution: remote805, fixed C4 image
sha256:c4fe40487178fd7738600562c114e2198d281d5ca6c0f5a265019369f6af76c4,
runc, one CPU, GPU void, network none. Real Rust library from the prior
source-equal numeric-input validation was reused. No live model, image build,
publication, deployment or routing change occurred.

Evidence: governor-json-depth-evidence-r1.tgz
SHA-256: c46c1a26ca26fdcf15abd69228c9ccda1caaa266b90290c71a1b3bb890a8e006.
