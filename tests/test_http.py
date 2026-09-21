"""HTTP contract tests for the authenticated, scheduler-owned PIG control route.

These tests deliberately use the production Rust governor and SGLang request
type.  ``FakeManager`` only replaces the scheduler transport: it executes the
same admin operation that the scheduler owner would execute, without starting
a server, GPU worker, or network listener.
"""
import asyncio
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from starlette.requests import Request

from pig_governor import Governor, RevisionConflict
from pig_governor.admin import execute
from pig_governor import http
from pig_governor.identity import build_runtime_identity
from pig_governor.profile import build_profile_document, coverage
from sglang.srt.managers.io_struct import SetInternalStateReq


def request(method, *, body=b"", authorization="Bearer admin", content_type=None,
            chunks=None, disconnect=False, query_string=b""):
    """Create a real Starlette request without opening an ASGI connection."""
    headers = []
    if authorization is not None:
        headers.append((b"authorization", authorization.encode("ascii")))
    if content_type is not None:
        headers.append((b"content-type", content_type.encode("ascii")))
    if isinstance(query_string, str):
        query_string = query_string.encode("ascii")
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": "/v1/pig-governor",
        "raw_path": b"/v1/pig-governor", "query_string": query_string, "headers": headers,
        "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
    }
    if disconnect:
        events = [{"type": "http.disconnect"}]
    else:
        chunks = [body] if chunks is None else chunks
        events = [
            {"type": "http.request", "body": chunk, "more_body": index + 1 < len(chunks)}
            for index, chunk in enumerate(chunks)
        ] or [{"type": "http.request", "body": b"", "more_body": False}]

    async def receive():
        if events:
            return events.pop(0)
        return {"type": "http.disconnect"}

    return Request(scope, receive=receive)


def patch_body(epoch, revision, reference):
    return json.dumps({
        "expected_epoch": epoch,
        "expected_revision": revision,
        "tps_reference": reference,
    }).encode("utf-8")


def document(response):
    return json.loads(response.body)


def profile_snapshot(epoch):
    runtime = {
        'attention_backend': 'fa3',
        'decode_attention_backend': None,
        'prefill_attention_backend': None,
        'chunked_prefill_size': 4096,
        'context_length': 262144,
        'cuda_graph_backend_decode': 'auto',
        'cuda_graph_backend_prefill': 'auto',
        'disable_cuda_graph': False,
        'disable_overlap_schedule': True,
        'disable_radix_cache': False,
        'disaggregation_mode': 'null',
        'dp_size': 1,
        'dtype': 'bfloat16',
        'enable_dp_attention': False,
        'enable_torch_compile': False,
        'kv_cache_dtype': 'bfloat16',
        'load_format': 'auto',
        'max_prefill_tokens': 16384,
        'max_running_requests': 4,
        'max_total_tokens': 262144,
        'mem_fraction_static': 0.8,
        'model_config_parser': 'auto',
        'model_impl': 'sglang',
        'page_size': 1,
        'pp_max_micro_batch_size': 4,
        'pp_size': 1,
        'quantization': None,
        'sampling_backend': 'pytorch',
        'schedule_policy': 'fcfs',
        'speculative_accept_threshold_acc': 0.9,
        'speculative_accept_threshold_single': 1.0,
        'speculative_algorithm': 'EAGLE',
        'speculative_draft_attention_backend': None,
        'speculative_draft_kv_cache_dtype': 'bfloat16',
        'speculative_eagle_topk': 1,
        'speculative_num_draft_tokens': 5,
        'speculative_num_steps': 3,
        'torch_compile_max_bs': 32,
        'tp_size': 1,
        'weight_version': 'default',
    }
    identity = build_runtime_identity(runtime, environ={
        'PIG_ENGINE_COMMIT': '1' * 40,
        'PIG_GOVERNOR_COMMIT': '2' * 40,
        'PIG_MODEL_ARTIFACT_ID': 'sha256:' + '3' * 64,
        'PIG_RUNTIME_HARDWARE_ID': 'h100-sxm-tp1-v1',
    })
    profile = build_profile_document(identity, 4, [])
    missing, count = coverage((), 4)
    return {
        'epoch': epoch,
        'runtime_identity_sha256': identity['sha256'],
        'coverage': {
            'count': count,
            'total': 16,
            'missing': [list(key) for key in missing],
        },
        'profile': profile,
    }


class FakeManager:
    """A one-rank scheduler transport that records real control commands."""

    def __init__(self, *, admin_api_key="admin", api_key="ordinary"):
        self.server_args = SimpleNamespace(
            admin_api_key="raw-admin-key",
            api_key="raw-api-key",
        )
        self.serving = SimpleNamespace(
            admin_api_key=admin_api_key,
            api_key=api_key,
        )
        self.core = Governor(35, max_running_requests=4)
        self.now = 3.0
        self.set_started = asyncio.Event()
        self.release_set = asyncio.Event()
        self.release_set.set()
        self.set_requests = []
        self.set_calls = 0
        self.get_calls = 0
        self.governor_available = True
        self.profile_available = True
        self.profile_ranks = 1
        self.profile = profile_snapshot(self.core.epoch)
        self.get_started = asyncio.Event()
        self.release_get = asyncio.Event()
        self.release_get.set()

    def close(self):
        self.core.close()

    async def set_internal_state(self, command):
        self.set_calls += 1
        self.set_requests.append(command)
        self.set_started.set()
        await self.release_set.wait()
        if not isinstance(command, SetInternalStateReq):
            return [False]
        try:
            execute(self.core, "patch", self.now, command.server_args["pig_governor"])
        except (KeyError, RevisionConflict, ValueError):
            return [False]
        return [True]

    async def get_internal_state(self):
        self.get_calls += 1
        self.get_started.set()
        await self.release_get.wait()
        if not self.governor_available:
            return [{}]
        state = {"pig_governor": execute(self.core, "get", self.now)}
        if self.profile_available:
            state["pig_governor_profile"] = self.profile
        return [dict(state) for _ in range(self.profile_ranks)]

    async def finish_control(self):
        """Release a deliberately delayed scheduler response and drain it."""
        self.release_set.set()
        task = getattr(self, "_pig_control_task", None)
        if task is not None:
            await asyncio.shield(task)


class GovernorHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = FakeManager()
        self.addCleanup(self.manager.close)
        self.runtime_context = patch(
            'pig_governor.http.get_serving', return_value=self.manager.serving
        )
        self.runtime_context.start()
        self.addCleanup(self.runtime_context.stop)

    async def get(self, *, authorization="Bearer admin"):
        return await http.endpoint(
            self.manager, request("GET", authorization=authorization)
        )

    async def patch(self, payload, *, authorization="Bearer admin", body=None,
                    content_type="application/json", **request_options):
        return await http.endpoint(
            self.manager,
            request(
                "PATCH",
                body=patch_body(**payload) if body is None else body,
                authorization=authorization,
                content_type=content_type,
                **request_options,
            ),
        )

    async def profile(self, *, epoch=None, authorization="Bearer admin",
                      method="GET", query_string=None):
        if query_string is None:
            expected = self.manager.profile["epoch"] if epoch is None else epoch
            query_string = "expected_epoch=" + expected
        return await http.profile_endpoint(
            self.manager,
            request(method, authorization=authorization, query_string=query_string),
        )

    async def test_missing_wrong_and_non_admin_tokens_cannot_dispatch(self):
        for token in (None, "Bearer wrong", "Bearer ordinary"):
            with self.subTest(token=token):
                response = await self.get(authorization=token)
                self.assertEqual(response.status_code, 401)
        self.assertEqual((self.manager.set_calls, self.manager.get_calls), (0, 0))

    async def test_admin_key_takes_priority_but_api_key_is_a_fallback(self):
        self.assertEqual((await self.get()).status_code, 200)
        self.manager.serving.admin_api_key = None
        self.assertEqual((await self.get(authorization="Bearer ordinary")).status_code, 200)

    async def test_auth_reads_resolved_serving_not_raw_manager_args(self):
        self.assertEqual((await self.get(authorization="Bearer admin")).status_code, 200)
        self.assertEqual((await self.get(authorization="Bearer raw-admin-key")).status_code, 401)

    async def test_get_returns_scheduler_snapshot_without_caching(self):
        response = await self.get()
        state = document(response)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(state["revision"], 1)
        self.assertEqual(state["mutable"]["tps_reference"], 35.0)
        self.assertEqual(state["reference_semantics"], "soft_target")

    async def test_patch_compare_and_swap_keeps_observation_history(self):
        self.manager.core.observe(0, 0, 2)
        self.manager.core.observe(1, 10, 1)
        self.manager.core.observe(3, 4, 0)
        before = document(await self.get())

        response = await self.patch({
            "epoch": before["epoch"], "revision": before["revision"], "reference": 50,
        })
        after = document(response)

        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(self.manager.set_requests[0], SetInternalStateReq)
        self.assertEqual(after["revision"], before["revision"] + 1)
        self.assertEqual(after["mutable"]["tps_reference"], 50.0)
        self.assertEqual(after["decode_tokens"], before["decode_tokens"])
        self.assertEqual(after["decode_sequence_seconds"], before["decode_sequence_seconds"])

    async def test_patch_rejects_a_stale_epoch_without_changing_policy(self):
        before = document(await self.get())
        response = await self.patch({
            "epoch": "0" * 32, "revision": before["revision"], "reference": 50,
        })
        after = document(await self.get())

        self.assertEqual(response.status_code, 409)
        self.assertEqual(after["epoch"], before["epoch"])
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["mutable"], before["mutable"])

    async def test_patch_rejects_duplicate_nonfinite_boolean_and_unknown_fields(self):
        valid = document(await self.get())
        invalid_bodies = {
            "duplicate": (
                b'{"expected_epoch":"' + valid["epoch"].encode() +
                b'","expected_revision":1,"expected_revision":1,"tps_reference":50}'
            ),
            "nan": patch_body(valid["epoch"], valid["revision"], float("nan")),
            "boolean": patch_body(valid["epoch"], valid["revision"], True),
            "unknown_field": (
                patch_body(valid["epoch"], valid["revision"], 50)[:-1] +
                b',"unexpected":1}'
            ),
        }
        for name, body in invalid_bodies.items():
            with self.subTest(name=name):
                response = await self.patch({}, body=body)
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.manager.set_calls, 0)

    async def test_patch_rejects_oversized_body_before_scheduler_dispatch(self):
        body = b"{" + b"x" * 4096 + b"}"
        response = await self.patch({}, body=body)
        self.assertEqual(response.status_code, 413)
        self.assertEqual((self.manager.set_calls, self.manager.get_calls), (0, 0))

    async def test_huge_integer_reference_is_bad_request_without_policy_mutation(self):
        self.manager.core.observe(0, 0, 2)
        self.manager.core.observe(1, 10, 0)
        before = self.manager.core.snapshot(self.manager.now)
        body = patch_body(before["epoch"], before["revision"], 10**1000)
        self.assertLess(len(body), 4096)
        response = await self.patch({}, body=body)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(document(response), {"error": "invalid_request"})
        self.assertEqual((self.manager.set_calls, self.manager.get_calls), (0, 0))
        self.assertEqual(self.manager.core.snapshot(self.manager.now), before)

    async def test_patch_rejects_client_disconnect_before_scheduler_dispatch(self):
        response = await self.patch({
            "epoch": "0" * 32, "revision": 1, "reference": 1,
        }, disconnect=True)
        self.assertEqual(response.status_code, 400)
        self.assertEqual((self.manager.set_calls, self.manager.get_calls), (0, 0))

    async def test_patch_requires_json_content_type(self):
        current = document(await self.get())
        response = await self.patch({
            "epoch": current["epoch"], "revision": current["revision"], "reference": 50,
        }, content_type="text/plain")
        self.assertEqual(response.status_code, 415)
        self.assertEqual(self.manager.set_calls, 0)

    async def test_get_reports_a_scheduler_without_governor_state(self):
        self.manager.governor_available = False
        response = await self.get()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(document(response)["error"], "governor_unavailable")

    async def test_profile_auth_uses_resolved_admin_or_api_key(self):
        for token in (None, "Bearer wrong", "Bearer ordinary"):
            with self.subTest(token=token):
                response = await self.profile(authorization=token)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.manager.get_calls, 0)

        self.manager.serving.admin_api_key = None
        self.assertEqual(
            (await self.profile(authorization="Bearer ordinary")).status_code, 200
        )

    async def test_profile_rejects_missing_duplicate_unknown_and_invalid_query(self):
        valid = self.manager.profile["epoch"]
        queries = (
            b"",
            b"expected_epoch=" + valid.encode() + b"&expected_epoch=" + valid.encode(),
            b"expected_epoch=" + valid.encode() + b"&unexpected=1",
            b"expected_epoch=" + ("A" * 32).encode(),
            b"expected_epoch=" + ("0" * 31).encode(),
            b"unexpected=1",
        )
        for query in queries:
            with self.subTest(query=query):
                response = await self.profile(query_string=query)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(document(response), {"error": "invalid_request"})
                self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.manager.get_calls, 0)

    async def test_profile_returns_scheduler_snapshot_without_caching(self):
        response = await self.profile()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(document(response), self.manager.profile)
        self.assertEqual(self.manager.get_calls, 1)

    async def test_profile_rejects_a_stale_epoch(self):
        response = await self.profile(epoch="0" * 32)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(document(response), {"error": "profile_epoch_mismatch"})
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_profile_rejects_malformed_scheduler_envelopes(self):
        base = copy.deepcopy(self.manager.profile)
        valid_epoch = base["epoch"]

        malformed = []
        extra = copy.deepcopy(base)
        extra["unexpected"] = True
        malformed.append(extra)
        missing = copy.deepcopy(base)
        del missing["coverage"]
        malformed.append(missing)
        digest = copy.deepcopy(base)
        digest["runtime_identity_sha256"] = "0" * 64
        malformed.append(digest)
        summary = copy.deepcopy(base)
        summary["coverage"]["count"] = 1
        malformed.append(summary)
        profile = copy.deepcopy(base)
        profile["profile"]["predictor"]["abi_version"] = 99
        malformed.append(profile)
        epoch = copy.deepcopy(base)
        epoch["epoch"] = "A" * 32
        malformed.append(epoch)

        for value in malformed:
            with self.subTest(value=value):
                self.manager.profile = value
                response = await self.profile(epoch=valid_epoch)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(document(response), {"error": "governor_unavailable"})
                self.assertEqual(response.headers["cache-control"], "no-store")
        self.manager.profile = base

    async def test_profile_method_and_timeout_are_not_cacheable(self):
        response = await self.profile(method="POST")
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.headers["cache-control"], "no-store")

        self.manager.get_started = asyncio.Event()
        self.manager.release_get.clear()
        with patch.object(http, "CONTROL_TIMEOUT", 0.001):
            timed = asyncio.create_task(self.profile())
            await self.manager.get_started.wait()
            response = await timed
        self.assertEqual(response.status_code, 503)
        self.assertEqual(document(response), {"error": "control_unavailable"})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.manager.release_get.set()
        task = getattr(self.manager, "_pig_control_task", None)
        if task is not None:
            await asyncio.shield(task)

    async def test_profile_requires_one_scheduler_with_profile_state(self):
        self.manager.profile_available = False
        response = await self.profile()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(document(response)["error"], "governor_unavailable")
        self.assertEqual(response.headers["cache-control"], "no-store")

        self.manager.profile_available = True
        self.manager.profile_ranks = 2
        response = await self.profile()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(document(response)["error"], "governor_unavailable")
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_profile_and_policy_share_control_busy_mutex(self):
        current = document(await self.get())
        payload = {
            "epoch": current["epoch"], "revision": current["revision"], "reference": 50,
        }
        self.manager.release_set.clear()
        patch_task = asyncio.create_task(self.patch(payload))
        await self.manager.set_started.wait()
        response = await self.profile()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(document(response), {"error": "control_busy"})
        self.assertEqual(response.headers["cache-control"], "no-store")
        await self.manager.finish_control()
        self.assertEqual((await patch_task).status_code, 200)

        self.manager.get_started = asyncio.Event()
        self.manager.release_get.clear()
        profile_task = asyncio.create_task(self.profile())
        await self.manager.get_started.wait()
        response = await self.get()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(document(response), {"error": "control_busy"})
        self.manager.release_get.set()
        self.assertEqual((await profile_task).status_code, 200)

    async def test_concurrent_same_revision_patch_dispatches_once(self):
        current = document(await self.get())
        payload = {
            "epoch": current["epoch"], "revision": current["revision"], "reference": 50,
        }
        self.manager.release_set.clear()
        first = asyncio.create_task(self.patch(payload))
        await self.manager.set_started.wait()
        second = await self.patch(payload)
        await self.manager.finish_control()
        first = await first

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 503)
        self.assertEqual(self.manager.set_calls, 1)

    async def test_timeout_and_cancelled_caller_leave_one_control_round_to_finish(self):
        current = document(await self.get())
        payload = {
            "epoch": current["epoch"], "revision": current["revision"], "reference": 50,
        }
        self.manager.release_set.clear()
        with patch.object(http, "CONTROL_TIMEOUT", 0.001):
            timed = asyncio.create_task(self.patch(payload))
            await self.manager.set_started.wait()
            timeout_response = await timed
        self.assertEqual(timeout_response.status_code, 503)
        await self.manager.finish_control()
        self.assertEqual(document(await self.get())["mutable"]["tps_reference"], 50.0)

        current = document(await self.get())
        self.manager.set_started = asyncio.Event()
        self.manager.release_set.clear()
        cancelled = asyncio.create_task(self.patch({
            "epoch": current["epoch"], "revision": current["revision"], "reference": 60,
        }))
        await self.manager.set_started.wait()
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        await self.manager.finish_control()
        self.assertEqual(document(await self.get())["mutable"]["tps_reference"], 60.0)
        self.assertEqual(self.manager.set_calls, 2)


if __name__ == "__main__":
    unittest.main()
