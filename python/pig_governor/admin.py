"""Framework-neutral admin operation, dispatched on the scheduler owner.

The authenticated HTTP layer transports this command. It must not retry PATCH
on uncertain transport outcome, and must never expose an unauthenticated route.
"""
from .core import _finite


def execute(core, operation, now, payload=None):
    if operation == "get":
        if payload not in (None, {}):
            raise ValueError("GET has no policy payload")
        return core.snapshot(now)
    if operation != "patch" or type(payload) is not dict or set(payload) != {
        "expected_epoch", "expected_revision", "tps_reference"
    }:
        raise ValueError("Invalid policy operation")
    epoch, revision, reference = (payload[k] for k in (
        "expected_epoch", "expected_revision", "tps_reference"))
    if not isinstance(epoch, str) or len(epoch) != 32 or any(c not in "0123456789abcdef" for c in epoch):
        raise ValueError("Invalid epoch")
    if type(revision) is not int or not 0 < revision < 2**53:
        raise ValueError("Invalid revision")
    _finite(reference)
    core.update_reference(epoch, revision, reference)
    return core.snapshot(now)
