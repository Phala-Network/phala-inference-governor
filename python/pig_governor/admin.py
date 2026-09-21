"""Framework-neutral admin operation, dispatched on the scheduler owner.

The authenticated HTTP layer transports this command. It must not retry PATCH
on uncertain transport outcome, and must never expose an unauthenticated route.
"""
from .core import _finite
from .scheduler import MAX_WAITING_LIMIT


_REQUIRED = {"expected_epoch", "expected_revision"}
_MUTABLE = {"tps_reference", "max_waiting", "max_running"}


def execute(owner, operation, now, payload=None):
    if operation == "get":
        if payload not in (None, {}):
            raise ValueError("GET has no policy payload")
        return owner.policy_snapshot(now)
    if operation != "patch" or type(payload) is not dict:
        raise ValueError("Invalid policy operation")
    keys = set(payload)
    changes = keys & _MUTABLE
    if not changes or not _REQUIRED <= keys or keys - _REQUIRED - _MUTABLE:
        raise ValueError("Invalid policy operation")
    epoch, revision = (payload[k] for k in (
        "expected_epoch", "expected_revision"))
    if not isinstance(epoch, str) or len(epoch) != 32 or any(c not in "0123456789abcdef" for c in epoch):
        raise ValueError("Invalid epoch")
    if type(revision) is not int or not 0 < revision < 2**53:
        raise ValueError("Invalid revision")
    if "tps_reference" in changes:
        _finite(payload["tps_reference"])
    for name in changes & {"max_waiting", "max_running"}:
        value = payload[name]
        minimum = 0 if name == "max_waiting" else 1
        maximum = MAX_WAITING_LIMIT if name == "max_waiting" else 2**32 - 1
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"Invalid {name}")
    owner.update_policy(
        now,
        expected_epoch=epoch,
        expected_revision=revision,
        **{name: payload[name] for name in changes},
    )
    return owner.policy_snapshot(now)
