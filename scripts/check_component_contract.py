#!/usr/bin/env python3
"""CPU component contract probe; safe to feed to an installed image's python -I.

Creates only a private Governor handle with synthetic observations. No server,
model, socket, credential, source patch, policy mutation on a live service, or
publication. Image identity and actual HTTP/serving acceptance remain external.
"""
import hashlib
import json
from pathlib import Path

import pig_governor.core as binding
from pig_governor import Governor, RevisionConflict
from pig_governor.admin import execute


def require(condition, message):
    # Do not rely on assert: final images may set PYTHONOPTIMIZE.
    if not condition:
        raise RuntimeError(message)


def rejected(operation, exception):
    try:
        operation()
    except exception:
        return
    raise RuntimeError("invalid operation was accepted")


def main():
    core = Governor(35, max_running_requests=4)
    checks = []
    try:
        core.observe(0, 0, 2)
        core.observe(1, 10, 1)
        core.observe(3, 4, 0)
        before = execute(core, "get", 3)
        require(before["decode_tokens"] == 14, "decode token accounting differs")
        require(before["decode_sequence_seconds"] == 4, "sequence-time accounting differs")
        require(before["average_tps"] == 3.5, "average TPS differs")
        require(before["reference_semantics"] == "soft_target", "reference must remain soft")
        require(before["individual_tps_binding"] is False, "per-request binding introduced")
        checks.append("real_decode_accounting_and_soft_reference")

        after = execute(core, "patch", 3, {
            "expected_epoch": before["epoch"],
            "expected_revision": before["revision"],
            "tps_reference": 50,
        })
        require(after["epoch"] == before["epoch"], "hot update reset epoch")
        require(after["revision"] == before["revision"] + 1, "CAS revision did not advance once")
        require(after["mutable"]["tps_reference"] == 50, "reference update missing")
        for key in ("decode_tokens", "decode_sequence_seconds", "average_tps",
                    "active_decode_sequences"):
            require(after[key] == before[key], "hot update reset observation: " + key)
        checks.append("hot_update_preserves_epoch_and_history")

        rejected(lambda: core.update_reference(before["epoch"], before["revision"], 60),
                 RevisionConflict)
        wrong_epoch = ("1" if after["epoch"][0] == "0" else "0") + after["epoch"][1:]
        rejected(lambda: core.update_reference(wrong_epoch, after["revision"], 60),
                 RevisionConflict)
        require(core.snapshot(3) == after, "failed CAS mutated controller")
        checks.append("stale_revision_and_epoch_rejected")

        for reference in (True, -1, float("nan"), float("inf"), 10**1000):
            rejected(lambda: execute(core, "patch", 3, {
                "expected_epoch": after["epoch"],
                "expected_revision": after["revision"],
                "tps_reference": reference,
            }), ValueError)
            require(core.snapshot(3) == after, "invalid reference mutated controller")
        checks.append("invalid_reference_rejected_without_mutation")

        admission = core.admit(3, 1, 0)
        require(admission["reason"] == 4, "missing surface evidence must be unknown")
        rejected(lambda: core.observe_surface(3, 1, 1.0, 5, 0), ValueError)
        rejected(lambda: core.admit(3, 5, 0), ValueError)
        core.observe_surface(3, 100, 1.0, 1, 0)
        admission = core.admit(3, 1, 0)
        require(admission["allowed"] is True, "safe exact surface cell was rejected")
        require(admission["reason"] == 0, "safe exact surface reason differs")
        require(admission["evidence_concurrency"] == 1, "surface evidence concurrency differs")
        checks.append("response_surface_admission_contract")

        batch_core = Governor(50, max_running_requests=4)
        try:
            batch_core.observe_batch(3, 100, 1.0, 1, 0, 1)
            batch_admission = batch_core.admit(3, 1, 0)
            require(batch_admission["allowed"] is True, "atomic batch surface was rejected")
            require(batch_admission["reason"] == 0, "atomic batch reason differs")
            checks.append("atomic_batch_observation_contract")
        finally:
            batch_core.close()

        core.close()
        rejected(lambda: core.snapshot(3), RuntimeError)
        checks.append("closed_handle_rejected")
        library = Path(core._lib._name).resolve()
        result = {
            "schema": "phala.governor.component-contract.v1",
            "passed": True,
            "checks": checks,
            "abi_version": core._lib.pig_governor_abi_version(),
            "binding_path": str(Path(binding.__file__).resolve()),
            "library_path": str(library),
            "library_sha256": hashlib.sha256(library.read_bytes()).hexdigest(),
            "scope": "Private real-library CPU handle; no HTTP, model, live policy or image identity acceptance.",
        }
        require(result["abi_version"] == 4, "unexpected ABI")
        print(json.dumps(result, sort_keys=True))
    finally:
        core.close()


if __name__ == "__main__":
    main()
