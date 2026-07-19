from __future__ import annotations

import unittest
from pathlib import Path


class DockerBuildContextRegressionTestCase(unittest.TestCase):
    def test_runtime_entrypoint_is_available_to_docker_build(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        dockerfile = (repo_root / "Dockerfile").read_text(encoding="utf-8")
        dockerignore_lines = {
            line.strip()
            for line in (repo_root / ".dockerignore").read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        entrypoint = repo_root / "scripts" / "rocm-entrypoint.sh"

        self.assertTrue(entrypoint.is_file())
        self.assertIn(
            "COPY --chmod=755 scripts/rocm-entrypoint.sh "
            "/usr/local/bin/entrypoint.sh",
            dockerfile,
        )
        self.assertNotIn("scripts/", dockerignore_lines)
        self.assertIn("scripts/*", dockerignore_lines)
        self.assertIn("!scripts/rocm-entrypoint.sh", dockerignore_lines)


if __name__ == "__main__":
    unittest.main()
