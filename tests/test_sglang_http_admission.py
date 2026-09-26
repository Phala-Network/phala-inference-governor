"""HTTP propagation for scheduler-side Governor admission rejection."""

import json
import unittest
from http import HTTPStatus
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import ORJSONResponse
from httpx import ASGITransport, AsyncClient

from utils import engine_chunk, make_serving
from sglang.srt.entrypoints.openai.protocol import ResponsesRequest
from sglang.srt.entrypoints.openai.serving_base import (
    OpenAIServingBase,
)
from sglang.srt.entrypoints.openai.serving_chat import OpenAIServingChat
from sglang.srt.entrypoints.openai.serving_completions import (
    OpenAIServingCompletion,
)
from sglang.srt.managers.tokenizer_manager import TokenizerManager
from sglang.srt.runtime_context import reset_context


ADMISSION_MESSAGE = "The request is rejected by Governor TPS admission."
WAITING_MESSAGE = "The request is rejected by the Governor waiting limit."


class _FakeManager:
    def __init__(self):
        self.reject = True
        self.message = ADMISSION_MESSAGE
        self.server_args = SimpleNamespace()
        self.request_logger = SimpleNamespace(log_requests=False, log_requests_level=0)

    async def generate_request(self, _adapted_request, _raw_request):
        if self.reject:
            raise HTTPException(
                status_code=HTTPStatus.TOO_MANY_REQUESTS,
                detail=self.message,
            )
        yield {"text": "ok"}

    def create_abort_task(self, _adapted_request):
        return None


class _OpenAIProbeServing(OpenAIServingBase):
    """Exercise the same common first-result path used by chat/completions."""

    def _request_id_prefix(self):
        return "probe-"

    def _convert_to_internal_request(self, request, raw_request=None):
        return SimpleNamespace(background=False), request

    def _validate_request(self, _request):
        return None

    async def _handle_streaming_request(self, adapted_request, request, raw_request):
        async def generate_sse():
            async for result in self.tokenizer_manager.generate_request(
                adapted_request, raw_request
            ):
                yield f'data: {result["text"]}\n\n'

        generator = generate_sse()
        return await self._streaming_response_before_headers(
            generator, adapted_request, raw_request
        )

    async def _handle_non_streaming_request(
        self, adapted_request, request, raw_request
    ):
        result = await self._first_generated_response(adapted_request, raw_request)
        return ORJSONResponse(result)


class TokenizerAdmissionMappingTests(unittest.IsolatedAsyncioTestCase):
    def _tokenizer_manager_and_state(self):
        manager = object.__new__(TokenizerManager)
        state = SimpleNamespace(obj=SimpleNamespace(rid="rid"))
        manager.rid_to_state = {"rid": state}
        manager.enable_lora = False
        return manager, state

    def _abort_output(self, status_code):
        return {
            "meta_info": {
                "finish_reason": {
                    "type": "abort",
                    "status_code": status_code,
                    "message": ADMISSION_MESSAGE,
                }
            }
        }

    async def test_admission_429_raises_for_streaming_and_non_streaming(self):
        for is_stream in (False, True):
            with self.subTest(is_stream=is_stream):
                manager, state = self._tokenizer_manager_and_state()
                output = self._abort_output(HTTPStatus.TOO_MANY_REQUESTS)
                if is_stream:
                    self.assertIs(
                        await manager._handle_abort_finish_reason(output, state, True),
                        output,
                    )
                else:
                    with self.assertRaises(HTTPException) as raised:
                        await manager._handle_abort_finish_reason(output, state, False)
                    self.assertEqual(raised.exception.status_code, 429)
                    self.assertEqual(raised.exception.detail, ADMISSION_MESSAGE)
                self.assertNotIn("rid", manager.rid_to_state)

    async def test_existing_streaming_503_remains_an_error_chunk(self):
        manager, state = self._tokenizer_manager_and_state()
        output = self._abort_output(HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertIs(
            await manager._handle_abort_finish_reason(output, state, True), output
        )


class ActualOpenAIServingFirstResultTests(unittest.IsolatedAsyncioTestCase):
    def _serving(self, serving_class, stream_generator_name):
        manager = _FakeManager()
        serving = object.__new__(serving_class)
        serving.tokenizer_manager = manager
        serving.allowed_custom_labels = None
        serving.chat_encoding_spec = None
        serving._validate_request = lambda _request: None
        serving._convert_to_internal_request = (
            lambda request, raw_request=None: (
                SimpleNamespace(background=False),
                request,
            )
        )

        async def generate_sse(_adapted_request, _request, _raw_request):
            async for result in manager.generate_request(
                _adapted_request, _raw_request
            ):
                yield f'data: {result["text"]}\n\n'

        setattr(serving, stream_generator_name, generate_sse)

        helper_calls = []
        original_helper = serving._streaming_response_before_headers

        async def tracked_helper(*args, **kwargs):
            helper_calls.append(True)
            return await original_helper(*args, **kwargs)

        serving._streaming_response_before_headers = tracked_helper
        return manager, serving, helper_calls

    async def test_chat_and_completion_use_real_first_result_path(self):
        cases = (
            (OpenAIServingChat, "_generate_chat_stream"),
            (OpenAIServingCompletion, "_generate_completion_stream"),
        )
        for serving_class, stream_generator_name in cases:
            with self.subTest(serving_class=serving_class.__name__, rejected=True):
                manager, serving, helper_calls = self._serving(
                    serving_class, stream_generator_name
                )
                response = await serving.handle_request(
                    SimpleNamespace(stream=True), None
                )
                self.assertEqual(response.status_code, 429)
                self.assertEqual(helper_calls, [True])
                error = response.body.decode()
                self.assertIn(ADMISSION_MESSAGE, error)
                self.assertIn('"code":429', error)

            with self.subTest(serving_class=serving_class.__name__, rejected=False):
                manager, serving, helper_calls = self._serving(
                    serving_class, stream_generator_name
                )
                manager.reject = False
                response = await serving.handle_request(
                    SimpleNamespace(stream=True), None
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.media_type, "text/event-stream")
                self.assertEqual(helper_calls, [True])
                chunks = [chunk async for chunk in response.body_iterator]
                self.assertEqual(chunks, ["data: ok\n\n"])


class AdmissionHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = _FakeManager()
        common_serving = _OpenAIProbeServing(self.manager)

        app = FastAPI()

        async def common_endpoint(raw_request: Request):
            payload = await raw_request.json()
            return await common_serving.handle_request(
                SimpleNamespace(stream=payload.get("stream", False)), raw_request
            )

        app.post("/v1/chat/completions")(common_endpoint)
        app.post("/v1/completions")(common_endpoint)

        self.client = AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_all_openai_generation_routes_return_real_429(self):
        for message in (ADMISSION_MESSAGE, WAITING_MESSAGE):
            self.manager.message = message
            for path in (
                "/v1/chat/completions",
                "/v1/completions",
            ):
                for stream in (False, True):
                    with self.subTest(path=path, stream=stream, message=message):
                        response = await self.client.post(path, json={"stream": stream})
                        self.assertEqual(response.status_code, 429)
                        self.assertNotEqual(
                            response.headers.get("content-type"), "text/event-stream"
                        )
                        body = response.json()
                        self.assertEqual(
                            set(body), {"object", "message", "type", "param", "code"}
                        )
                        self.assertEqual(body["object"], "error")
                        self.assertEqual(body["message"], message)
                        self.assertEqual(body["type"], "429")
                        self.assertIsNone(body["param"])
                        self.assertEqual(body["code"], 429)

    async def test_normal_streams_still_start_as_sse(self):
        self.manager.reject = False
        for path in (
            "/v1/chat/completions",
            "/v1/completions",
        ):
            with self.subTest(path=path):
                response = await self.client.post(path, json={"stream": True})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(
                    response.headers["content-type"].startswith("text/event-stream")
                )
                self.assertIn("ok", response.text)


class ResponsesAdmissionHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset_context()
        self.serving = make_serving()
        self.serving.use_harmony = False
        self.serving.default_chat_template_kwargs = {}
        self.serving.template_manager.chat_template_name = None
        self.serving.template_manager.jinja_template_content_format = "string"
        self.serving.tokenizer_manager.tokenizer.apply_chat_template.return_value = [1, 2, 3]
        self.serving.reasoning_parser = None
        self.serving.tool_call_parser = None
        self.reject = True
        self.message = ADMISSION_MESSAGE

        async def generate(*args, **kwargs):
            if self.reject:
                raise HTTPException(status_code=429, detail=self.message)
            yield engine_chunk("ok", finish=True)

        self.serving.tokenizer_manager.generate_request = generate

    async def asyncTearDown(self):
        reset_context()

    async def test_responses_rejection_precedes_headers(self):
        for message in (ADMISSION_MESSAGE, WAITING_MESSAGE):
            self.message = message
            for stream in (False, True):
                with self.subTest(stream=stream, message=message):
                    request = ResponsesRequest(model="x", input="hi", stream=stream)
                    response = await self.serving.create_responses(request)
                    self.assertEqual(response.status_code, 429)
                    self.assertNotEqual(response.media_type, "text/event-stream")
                    self.assertEqual(json.loads(response.body), {
                        "error": {
                            "message": message,
                            "type": "invalid_request_error",
                            "param": None,
                            "code": 429,
                        },
                    })

    async def test_normal_responses_stream_starts_as_sse(self):
        self.reject = False
        request = ResponsesRequest(model="x", input="hi", stream=True)
        response = await self.serving.create_responses(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.media_type, "text/event-stream")
        events = [event async for event in response.body_iterator]
        self.assertTrue(events[0].startswith("event: response.created"))
        self.assertTrue(any("response.completed" in event for event in events))


if __name__ == "__main__":
    unittest.main()
