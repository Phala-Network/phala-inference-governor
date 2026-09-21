"""Actual SGLang auth middleware plus plugin handler, without a model."""
import ast
import json
import unittest
import sglang
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sglang.srt.utils.auth import AuthLevel, auth_level, add_api_key_middleware
from pig_governor.http import endpoint, profile_endpoint
from test_http import FakeManager


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_middleware_api_key_fallback_and_admin_priority(self):
        for api, admin, token, status in (
            ('api', None, 'api', 200), ('api', 'admin', 'api', 401),
            ('api', 'admin', 'admin', 200), (None, None, '', 401),
        ):
            with self.subTest(api=bool(api), admin=bool(admin), status=status):
                manager = FakeManager(api_key=api, admin_api_key=admin)
                app = FastAPI()
                @app.get('/admin/v1/predictive-policy')
                @auth_level(AuthLevel.ADMIN_OPTIONAL)
                async def route(request: Request):
                    return await endpoint(manager, request)
                add_api_key_middleware(app, api_key=api, admin_api_key=admin)
                try:
                    serving = SimpleNamespace(admin_api_key=admin, api_key=api)
                    with patch('pig_governor.http.get_serving', return_value=serving):
                        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
                            response = await client.get('/admin/v1/predictive-policy', headers={'Authorization':'Bearer '+token})
                            self.assertEqual(response.status_code, status)
                            if status==200: self.assertEqual(response.json()['mutable']['tps_reference'],35)
                finally:
                    manager.close()

    def test_upstream_endpoint_uses_the_verified_auth_level(self):
        path = Path(sglang.__file__).resolve().parent / 'srt/entrypoints/http_server.py'
        module=ast.parse(path.read_text())
        endpoint_node=next(n for n in module.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='governor_policy')
        decorators=[ast.unparse(n) for n in endpoint_node.decorator_list]
        self.assertIn('auth_level(AuthLevel.ADMIN_OPTIONAL)',decorators)
        self.assertTrue(any('/admin/v1/predictive-policy' in d for d in decorators))

        profile_node=next(n for n in module.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='governor_profile')
        profile_decorators=[ast.unparse(n) for n in profile_node.decorator_list]
        self.assertIn('auth_level(AuthLevel.ADMIN_OPTIONAL)',profile_decorators)
        self.assertTrue(any('/admin/v1/predictive-profile' in d for d in profile_decorators))

    async def test_profile_route_uses_the_same_resolved_admin_auth(self):
        manager = FakeManager(api_key='api', admin_api_key='admin')
        app = FastAPI()

        @app.get('/admin/v1/predictive-profile')
        @auth_level(AuthLevel.ADMIN_OPTIONAL)
        async def route(request: Request):
            return await profile_endpoint(manager, request)

        add_api_key_middleware(app, api_key='api', admin_api_key='admin')
        try:
            serving = SimpleNamespace(admin_api_key='admin', api_key='api')
            with patch('pig_governor.http.get_serving', return_value=serving):
                async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
                    query='?expected_epoch='+manager.profile['epoch']
                    self.assertEqual((await client.get('/admin/v1/predictive-profile'+query,
                                                       headers={'Authorization':'Bearer api'})).status_code,401)
                    response=await client.get('/admin/v1/predictive-profile'+query,
                                              headers={'Authorization':'Bearer admin'})
                    self.assertEqual(response.status_code,200)
                    self.assertEqual(response.json(),manager.profile)
        finally:
            manager.close()


if __name__=='__main__':unittest.main()
