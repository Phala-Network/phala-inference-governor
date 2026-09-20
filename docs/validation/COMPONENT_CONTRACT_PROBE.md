# Component probe preflight

`scripts/check_component_contract.py` was executed on the approved remote805
fixed C4 image, with one CPU, no network, GPU void, read-only source evidence
mount and `PYTHONOPTIMIZE=1`. This was source-bound preflight, not a final-image
test. The script's five groups exercise a private real-library controller:

- decode-token/sequence-time accounting and soft reference semantics;
- hot policy update preserving epoch and measured history;
- stale revision and epoch rejection;
- invalid reference rejection without controller mutation;
- closed-handle rejection.

The original pre-55e3aa2 Python implementation failed on the 1001-digit integer
with OverflowError. The implementation from 55e3aa2 passed all five groups.
The real Rust library ABI remained v1 and its SHA-256 was
`b2c02bae95b1fe730a7e22806cfdf483bab85bd4b419f85f82f4f563bb4ec5ad`.
That library's Rust source equality was established by the numeric-input
regression evidence; no Rust behavior change was made here.

Probe SHA-256:
`ed58a7e7e380c66a2081289be3b767e399364692c6270334b7ec9a0846b6e881`.
Evidence `component-contract-evidence-r1.tgz` SHA-256:
`8880bf872039310ef2eee51d095ec419d25fabd86a00244b048d1e25e7155a8d`.

The release owner still needs to execute the probe against installed packages
in the actual immutable final image without a source mount, as described in
`scripts/README.md`. Neither this source preflight nor that CPU component probe
replaces HTTP/authentication, model serving, resource-drain or attestation gates.
