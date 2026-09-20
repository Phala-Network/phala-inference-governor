# Governor component contract probe

`check_component_contract.py` creates a private handle in the real Rust library
and checks measured decode accounting, the soft TPS reference, hot-update
history preservation, stale revision/epoch rejection, invalid numeric input
rejection and closed-handle behavior. Checks remain active under Python `-O`.
It never contacts a running scheduler or modifies a live policy.

For an already built immutable image, the release owner can run this script
through standard input using the image's installed interpreter and package:

```sh
docker run --rm -i --pull=never --network=none --runtime=runc \
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --cpus=1 --memory=512m -e NVIDIA_VISIBLE_DEVICES=void \
  --entrypoint python "$IMMUTABLE_IMAGE" -I - \
  < scripts/check_component_contract.py
```

Use an exact local image ID or accepted registry digest supplied by the release
owner. Do not mount source, replace installed packages, install dependencies,
or supply real service credentials. The image must already set its actual
`PIG_GOVERNOR_LIBRARY` path; a missing library fails rather than being repaired
by the probe. Record the externally inspected image/config identity with the
probe's imported binding path, library hash, ABI and result.

This complements the HF/CLI, source-receipt and actual API gates. It does not
verify image provenance, HTTP authentication/routing, GPU serving, lifecycle
drain, attestation, performance or deployment. Source-bound preflight of the
probe is not final-image acceptance. The script performs no Docker operation
itself and is not a new build or publication framework.
