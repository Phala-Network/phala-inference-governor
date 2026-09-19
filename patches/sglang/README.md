# SGLang integration patch

The current supported candidate targets official v0.5.20. Use the ordered
[v0.5.20 patch series](v0.5.20/README.md), its pinned commit and release manifest.
The combined development-baseline patch below is retained historical evidence;
do not apply it in addition to the versioned series.

## Historical development baseline

Development baseline: `1a56fbb0dc48ec3fb2b4b629fc4e74a3b57c639a`.
This is a development revision, not a claim that it is pristine upstream.

`0001-governor-and-serving-fixes.patch` contains explicit Governor hooks and
independent serving fixes. No native-QoS experiment modules are included.

The patch expects LF text. The recorded baseline contains CRLF text;
normalize those files to LF before `git apply --check` and `git apply`.
The reproduction check compared all 25 selected source/test files with the
validated candidate. See `docs/validation/governor-plugin-patch-reproduction-r5.json`.

Controller hooks are in `scheduler.py`, `http_server.py`, and the namespaced
control-message type in `io_struct.py`. Remaining changed files preserve generic
disconnect/shared-memory cleanup, worker metadata handoff and grammar behavior.
These are independent of the Rust controller and should be reviewed separately
when moving to a newer upstream revision. The complete per-file line inventory
is in `docs/validation/governor-plugin-source-audit-r5.json`.

CPU tests passed. GPU serving acceptance, upstream-version migration and
publication as a release remain pending.

Native get/set control IPC also uses per-call nonces in communicator and tokenizer control initialization. Replies must echo the matching nonce; tokenizer and scheduler must be upgraded together. This prevents cancelled native callers from contaminating later Governor CAS results.
