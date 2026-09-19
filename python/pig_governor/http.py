"""Thin admin route over SGLang's existing scheduler control transport."""
import asyncio
import json
import math
import secrets
from fastapi.responses import JSONResponse
from sglang.srt.runtime_context import get_serving
from starlette.requests import ClientDisconnect

CONTROL_TIMEOUT = 5.0


def _finished(task):
    # Retrieve an abandoned task exception without logging request credentials.
    if not task.cancelled():
        task.exception()


async def endpoint(manager, request):
    # Check explicitly even if the host's optional auth middleware is absent.
    serving = get_serving()
    key = serving.admin_api_key or serving.api_key
    expected = "Bearer " + key if key else None
    if expected is None or not secrets.compare_digest(request.headers.get("authorization", "").encode(), expected.encode()):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if request.method not in ("GET", "PATCH"):
        return JSONResponse({"error": "method_not_allowed"}, status_code=405)
    patch = None
    if request.method == "PATCH":
        if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
            return JSONResponse({"error": "invalid_content_type"}, status_code=415)
        try:
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > 4096:
                    return JSONResponse({"error": "body_too_large"}, status_code=413)
                raw.extend(chunk)
            def unique(pairs):
                result = {}
                for name, value in pairs:
                    if name in result: raise ValueError("duplicate key")
                    result[name] = value
                return result
            patch = json.loads(raw, object_pairs_hook=unique)
            if type(patch) is not dict or set(patch) != {"expected_epoch", "expected_revision", "tps_reference"}:
                raise ValueError("invalid fields")
            value = patch["tps_reference"]
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError("invalid reference")
            epoch, revision = patch["expected_epoch"], patch["expected_revision"]
            if type(epoch) is not str or len(epoch) != 32 or any(c not in "0123456789abcdef" for c in epoch):
                raise ValueError("invalid epoch")
            if type(revision) is not int or not 0 < revision < 2**53:
                raise ValueError("invalid revision")
        except (ValueError, UnicodeError, ClientDisconnect):
            return JSONResponse({"error": "invalid_request"}, status_code=400)
    pending = getattr(manager, "_pig_control_task", None)
    if pending is not None and not pending.done():
        return JSONResponse({"error": "control_busy"}, status_code=503)

    async def operation():
        if patch is not None:
            from sglang.srt.managers.io_struct import SetInternalStateReq
            changed = await manager.set_internal_state(SetInternalStateReq(server_args={"pig_governor": patch}))
            if changed != [True]:
                return JSONResponse({"error": "policy_conflict_or_invalid"}, status_code=409)
        states = await manager.get_internal_state()
        if len(states) != 1 or "pig_governor" not in states[0]:
            return JSONResponse({"error": "governor_unavailable"}, status_code=503)
        return JSONResponse(states[0]["pig_governor"], headers={"Cache-Control": "no-store"})
    # Finish the existing communicator round even after client disconnection;
    # do not let a cancelled waiter misattribute a late reply to the next call.
    task = asyncio.create_task(operation())
    manager._pig_control_task = task
    task.add_done_callback(_finished)
    try:
        return await asyncio.wait_for(asyncio.shield(task), CONTROL_TIMEOUT)
    except (TimeoutError, OSError):
        return JSONResponse({"error": "control_unavailable"}, status_code=503)
