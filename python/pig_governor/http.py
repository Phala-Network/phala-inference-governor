"""Thin admin route over SGLang's existing scheduler control transport."""
import asyncio
import json
import secrets
from fastapi.responses import JSONResponse
from sglang.srt.runtime_context import get_serving
from starlette.requests import ClientDisconnect
from .admin import validate_patch
from .profile import coverage, validate_profile

CONTROL_TIMEOUT = 5.0
NO_STORE = {"Cache-Control": "no-store"}


def _response(document, status_code=200):
    return JSONResponse(document, status_code=status_code, headers=NO_STORE)


def _lower_hex(value, length):
    return (
        type(value) is str
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _finished(task):
    # Retrieve an abandoned task exception without logging request credentials.
    if not task.cancelled():
        task.exception()


def _authorized(request):
    # Check explicitly even if the host's optional auth middleware is absent.
    serving = get_serving()
    key = serving.admin_api_key or serving.api_key
    expected = "Bearer " + key if key else None
    return expected is not None and secrets.compare_digest(
        request.headers.get("authorization", "").encode(), expected.encode()
    )


def _expected_epoch(request):
    items = list(request.query_params.multi_items())
    if len(items) != 1:
        raise ValueError("expected exactly one query parameter")
    name, epoch = items[0]
    if name != "expected_epoch" or not _lower_hex(epoch, 32):
        raise ValueError("invalid expected epoch")
    return epoch


async def _control(manager, operation):
    pending = getattr(manager, "_pig_control_task", None)
    if pending is not None and not pending.done():
        return _response({"error": "control_busy"}, status_code=503)

    # Finish the existing communicator round even after client disconnection;
    # do not let a cancelled waiter misattribute a late reply to the next call.
    task = asyncio.create_task(operation())
    manager._pig_control_task = task
    task.add_done_callback(_finished)
    try:
        return await asyncio.wait_for(asyncio.shield(task), CONTROL_TIMEOUT)
    except (TimeoutError, OSError):
        return _response({"error": "control_unavailable"}, status_code=503)


async def endpoint(manager, request):
    if not _authorized(request):
        return _response({"error": "unauthorized"}, status_code=401)
    if request.method not in ("GET", "PATCH"):
        return _response({"error": "method_not_allowed"}, status_code=405)
    patch = None
    if request.method == "PATCH":
        if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
            return _response({"error": "invalid_content_type"}, status_code=415)
        try:
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > 4096:
                    return _response({"error": "body_too_large"}, status_code=413)
                raw.extend(chunk)
            def unique(pairs):
                result = {}
                for name, value in pairs:
                    if name in result: raise ValueError("duplicate key")
                    result[name] = value
                return result
            patch = json.loads(raw, object_pairs_hook=unique)
            validate_patch(patch)
        except (ValueError, UnicodeError, ClientDisconnect):
            return _response({"error": "invalid_request"}, status_code=400)
    async def operation():
        if patch is not None:
            if "max_running" in patch:
                states = await manager.get_internal_state()
                if (not isinstance(states, (list, tuple)) or len(states) != 1
                        or not isinstance(states[0], dict)
                        or not isinstance(states[0].get("pig_governor"), dict)):
                    return _response({"error": "governor_unavailable"}, status_code=503)
                maximum = states[0]["pig_governor"].get("native_max_running_requests")
                if type(maximum) is not int or maximum < 1:
                    return _response({"error": "governor_unavailable"}, status_code=503)
                if patch["max_running"] > maximum:
                    return _response({"error": "invalid_request"}, status_code=400)
                # The scheduler still revalidates limits and owns the atomic CAS.
            from sglang.srt.managers.io_struct import SetInternalStateReq
            changed = await manager.set_internal_state(SetInternalStateReq(server_args={"pig_governor": patch}))
            if changed != [True]:
                return _response({"error": "policy_conflict_or_invalid"}, status_code=409)
        states = await manager.get_internal_state()
        if len(states) != 1 or "pig_governor" not in states[0]:
            return _response({"error": "governor_unavailable"}, status_code=503)
        return _response(states[0]["pig_governor"])
    return await _control(manager, operation)


def _profile_envelope(value):
    if type(value) is not dict or set(value) not in ({
        "epoch", "runtime_identity_sha256", "coverage", "profile"
    }, {
        "epoch", "runtime_identity_sha256", "coverage", "profile", "availability"
    }):
        raise ValueError("Invalid profile envelope fields")
    if not _lower_hex(value["epoch"], 32):
        raise ValueError("Invalid profile epoch")
    if not _lower_hex(value["runtime_identity_sha256"], 64):
        raise ValueError("Invalid runtime identity digest")

    document = value["profile"]
    if document is None:
        if value.get("availability") != "online_identity":
            raise ValueError("Invalid online profile availability")
        summary = value["coverage"]
        if type(summary) is not dict or set(summary) != {"count", "total", "missing"}:
            raise ValueError("Invalid online profile coverage fields")
        total = summary["total"]
        missing = summary["missing"]
        if (type(total) is not int or not 0 < total < 2**32 * 4 or total % 4
                or type(summary["count"]) is not int
                or type(missing) is not list
                or len(missing) > total
                or summary["count"] != total - len(missing)):
            raise ValueError("Invalid online profile coverage")
        maximum = total // 4
        previous = None
        for key in missing:
            if (type(key) is not list or len(key) != 2
                    or type(key[0]) is not int or type(key[1]) is not int
                    or not 1 <= key[0] <= maximum or not 0 <= key[1] < 4
                    or (previous is not None and tuple(key) <= previous)):
                raise ValueError("Invalid online profile missing cells")
            previous = tuple(key)
        return value
    if "availability" in value:
        raise ValueError("Loadable profile cannot have online availability")
    if type(document) is not dict:
        raise ValueError("Invalid profile document")
    maximum = document.get("max_running_requests")
    identity = document.get("runtime_identity")
    loaded = validate_profile(
        document,
        max_running_requests=maximum,
        current_identity=identity,
    )
    if loaded.metadata["runtime_identity_sha256"] != value["runtime_identity_sha256"]:
        raise ValueError("Profile identity digest mismatch")

    summary = value["coverage"]
    if type(summary) is not dict or set(summary) != {"count", "total", "missing"}:
        raise ValueError("Invalid profile coverage fields")
    missing, count = coverage(loaded.cells, maximum)
    expected_missing = [list(key) for key in missing]
    if (
        type(summary["count"]) is not int
        or type(summary["total"]) is not int
        or type(summary["missing"]) is not list
        or summary["count"] != count
        or summary["total"] != maximum * 4
        or summary["missing"] != expected_missing
    ):
        raise ValueError("Profile coverage does not match the document")
    return value


async def profile_endpoint(manager, request):
    if not _authorized(request):
        return _response({"error": "unauthorized"}, status_code=401)
    if request.method != "GET":
        return _response({"error": "method_not_allowed"}, status_code=405)
    try:
        expected_epoch = _expected_epoch(request)
    except (ValueError, UnicodeError):
        return _response({"error": "invalid_request"}, status_code=400)

    async def operation():
        states = await manager.get_internal_state()
        if not isinstance(states, (list, tuple)) or len(states) != 1:
            return _response({"error": "governor_unavailable"}, status_code=503)
        state = states[0]
        if not isinstance(state, dict) or "pig_governor_profile" not in state:
            return _response({"error": "governor_unavailable"}, status_code=503)
        try:
            profile = _profile_envelope(state["pig_governor_profile"])
        except (KeyError, TypeError, ValueError):
            return _response({"error": "governor_unavailable"}, status_code=503)
        if profile["epoch"] != expected_epoch:
            return _response({"error": "profile_epoch_mismatch"}, status_code=409)
        return _response(profile)

    return await _control(manager, operation)
