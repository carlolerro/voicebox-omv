from __future__ import annotations

import os
import unittest
from pathlib import Path


class DockerBuildContextRegressionTestCase(unittest.TestCase):
    def test_runtime_entrypoint_is_available_to_docker_build(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        dockerfile_path = repo_root / "Dockerfile"

        if dockerfile_path.is_file():
            dockerfile = dockerfile_path.read_text(encoding="utf-8")
            dockerignore_lines = {
                line.strip()
                for line in (repo_root / ".dockerignore").read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            }
            source_entrypoint = repo_root / "scripts" / "rocm-entrypoint.sh"

            self.assertTrue(source_entrypoint.is_file())
            self.assertIn(
                "COPY --chmod=755 scripts/rocm-entrypoint.sh "
                "/usr/local/bin/entrypoint.sh",
                dockerfile,
            )
            self.assertNotIn("scripts/", dockerignore_lines)
            self.assertIn("scripts/*", dockerignore_lines)
            self.assertIn("!scripts/rocm-entrypoint.sh", dockerignore_lines)
            return

        # The production image intentionally does not copy Dockerfile or the
        # source scripts directory. When this test runs inside that image,
        # verify the artifact produced by the Docker COPY instruction instead.
        runtime_entrypoint = Path("/usr/local/bin/entrypoint.sh")
        self.assertTrue(runtime_entrypoint.is_file())
        self.assertTrue(os.access(runtime_entrypoint, os.X_OK))
        self.assertIn(
            'exec gosu voicebox "$@"',
            runtime_entrypoint.read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
