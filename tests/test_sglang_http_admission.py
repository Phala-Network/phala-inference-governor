"""HTTP propagation for scheduler-side Governor admission rejection."""

import unittest
from http import HTTPStatus
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import ORJSONResponse
from httpx import ASGITransport, AsyncClient

from sglang.srt.entrypoints.openai.serving_base import (
    GenerationStreamingResponse,
    OpenAIServingBase,
)
from sglang.srt.entrypoints.openai.serving_chat import OpenAIServingChat
from sglang.srt.entrypoints.openai.serving_completions import (
    OpenAIServingCompletion,
)
from sglang.srt.entrypoints.openai.serving_responses import OpenAIServingResponses
from sglang.srt.managers.tokenizer_manager import TokenizerManager


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
                with self.assertRaises(HTTPException) as raised:
                    await manager._handle_abort_finish_reason(
                        self._abort_output(HTTPStatus.TOO_MANY_REQUESTS),
                        state,
                        is_stream,
                    )
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
        responses_serving = object.__new__(OpenAIServingResponses)
        responses_serving.tokenizer_manager = self.manager

        app = FastAPI()

        async def common_endpoint(raw_request: Request):
            payload = await raw_request.json()
            return await common_serving.handle_request(
                SimpleNamespace(stream=payload.get("stream", False)), raw_request
            )

        app.post("/v1/chat/completions")(common_endpoint)
        app.post("/v1/completions")(common_endpoint)

        @app.post("/v1/responses")
        async def responses_endpoint(raw_request: Request):
            payload = await raw_request.json()
            adapted_request = SimpleNamespace(background=False)
            generator = self.manager.generate_request(adapted_request, raw_request)
            if not payload.get("stream", False):
                try:
                    result = await responses_serving._first_generated_response(
                        adapted_request, raw_request
                    )
                except HTTPException as exc:
                    return responses_serving.create_error_response(
                        exc.detail, status_code=exc.status_code
                    )
                return ORJSONResponse(result)

            try:
                generator = await responses_serving._generator_after_first_item(
                    generator, raw_request
                )
            except HTTPException as exc:
                return responses_serving.create_error_response(
                    exc.detail, status_code=exc.status_code
                )

            async def events():
                yield 'event: response.created\ndata: {"type":"response.created"}\n\n'
                async for result in generator:
                    yield f'event: response.completed\ndata: {result["text"]}\n\n'

            return GenerationStreamingResponse(
                events(), generation=generator, media_type="text/event-stream"
            )

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
                "/v1/responses",
            ):
                for stream in (False, True):
                    with self.subTest(path=path, stream=stream, message=message):
                        response = await self.client.post(path, json={"stream": stream})
                        self.assertEqual(response.status_code, 429)
                        self.assertNotEqual(
                            response.headers.get("content-type"), "text/event-stream"
                        )
                        body = response.json()
                        error = body.get("error", body)
                        self.assertEqual(error["message"], message)
                        self.assertEqual(error["code"], 429)

    async def test_normal_streams_still_start_as_sse(self):
        self.manager.reject = False
        for path in (
            "/v1/chat/completions",
            "/v1/completions",
            "/v1/responses",
        ):
            with self.subTest(path=path):
                response = await self.client.post(path, json={"stream": True})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(
                    response.headers["content-type"].startswith("text/event-stream")
                )
                self.assertIn("ok", response.text)


if __name__ == "__main__":
    unittest.main()
