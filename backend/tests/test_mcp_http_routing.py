from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Route

from backend.mcp_server.context import MCPPathMiddleware
from backend.routes.oauth_discovery import router as oauth_discovery_router


class MCPHttpRoutingTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.frontend_dir = Path(self.temp_dir.name) / "frontend"
        self.frontend_dir.mkdir(parents=True)
        (self.frontend_dir / "index.html").write_text(
            "<html><body>Voicebox</body></html>", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _client(self) -> TestClient:
        application = FastAPI()
        application.include_router(oauth_discovery_router)

        @application.get("/{full_path:path}")
        async def serve_spa(full_path: str):
            return HTMLResponse("<html><body>Voicebox</body></html>")

        return TestClient(application)

    def test_oauth_protected_resource_metadata_is_not_served_by_spa(self) -> None:
        client = self._client()
        for path in (
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/mcp",
        ):
            response = client.get(path)
            self.assertEqual(response.status_code, 404, path)
            self.assertNotIn("text/html", response.headers.get("content-type", ""))

    def test_normal_spa_routes_still_return_index_html(self) -> None:
        client = self._client()
        response = client.get("/voices")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers.get("content-type", ""))
        self.assertIn("Voicebox", response.text)

    def test_exact_mcp_path_reaches_mounted_app_without_trailing_slash(self) -> None:
        async def initialize(_request):
            return JSONResponse({"transport": "mcp"})

        mcp_app = Starlette(
            routes=[Route("/", initialize, methods=["POST"])]
        )
        application = FastAPI()
        application.add_middleware(MCPPathMiddleware)
        application.mount("/mcp", mcp_app)

        @application.get("/{full_path:path}")
        async def serve_spa(full_path: str):
            return HTMLResponse("<html><body>Voicebox</body></html>")

        response = TestClient(application).post("/mcp")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"transport": "mcp"})

    def test_mcp_mount_precedes_spa_catch_all(self) -> None:
        app_source = (
            Path(__file__).resolve().parent.parent / "app.py"
        ).read_text(encoding="utf-8")
        self.assertLess(
            app_source.index('application.mount("/mcp", mcp_app)'),
            app_source.index("_mount_frontend(application)"),
        )


if __name__ == "__main__":
    unittest.main()
