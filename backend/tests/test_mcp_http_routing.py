from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.spa_frontend import mount_frontend


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
        mount_frontend(application, self.frontend_dir)
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
