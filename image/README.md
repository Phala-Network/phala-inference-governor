# Immutable serving image

The Dockerfile builds the Rust library with pinned Rust 1.98.1 and uses the
official SGLang v0.5.20 amd64 image as its final base. Dependencies remain those
of that immutable base. Governor installs offline during the image build;
runtime startup uses the official `sglang serve` entrypoint.

The external build context must contain `build-context/upstream-preimages.tar`,
generated from the 41 existing official Git blobs selected by the versioned
release manifest. Added files are supplied by the patches. The currently verified
archive SHA256 is
`769548241d9a4046b840c7b6a49672ad3064a0c512d02313dd3f1c8d6f6eb1d6`.
Pass that value as `UPSTREAM_PREIMAGES_SHA256`, and the clean Governor source
commit as `SOURCE_REVISION`. Freeze both in the pre-build input manifest.
The generated archive stays outside Git and must accompany the frozen context.

`install_sglang_overlay.py` reproduces all seven patches and checks all 59 source
hashes, validates the installed official preimages before writing, then installs
36 runtime modules. The remaining files are regression tests, retained in the
patch series. Existing native extensions and dependencies in the official image
are preserved. SGLang's distribution version remains the upstream 0.5.20;
Governor's version, source commit and overlay receipt identify the derived image.

The CPU installer regression verifies successful installation, rejection of a
changed installed preimage without partial writes, and rejection of a wrong
archive digest. It does not prove the official image's filesystem layout or the
final Docker build. The recorded candidate Dockerfile later gained `--no-index`
to explicitly forbid dependency downloads; installer bytes are unchanged.

Final release gates remain mandatory: exact-image `hf --help` and `hf download
--help`, official CLI/authentication, Rust ABI loading, source hash receipt,
required health tools, SBOM/provenance annotations, clean rebuild comparison and
registry readback. `command -v` in the Dockerfile is only an early check, not the
HF executable gate. The production Compose must explicitly enable Governor,
provide the unified TOKEN through environment, and preserve the qualified model,
topology and cache settings. Do not mount executable source over this image.
