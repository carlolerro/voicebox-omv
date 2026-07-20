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

    def test_omv_scripts_verify_all_story_tools_and_optional_smoke_profiles(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        verify_path = repo_root / "scripts" / "verify_mcp_profile_management_omv.sh"
        deploy_path = repo_root / "scripts" / "deploy_mcp_profile_management_omv.sh"
        if not verify_path.is_file() or not deploy_path.is_file():
            # Production image intentionally excludes source deployment scripts.
            return

        verify_script = verify_path.read_text(encoding="utf-8")
        deploy_script = deploy_path.read_text(encoding="utf-8")
        for tool_name in (
            "voicebox.create_story",
            "voicebox.get_story_status",
            "voicebox.get_story",
            "voicebox.resume_story",
        ):
            self.assertIn(tool_name, verify_script)
            self.assertIn(tool_name, deploy_script)
        self.assertIn("VERIFY_STORY_PROFILE_A", deploy_script)
        self.assertIn("VERIFY_STORY_PROFILE_B", deploy_script)
        self.assertIn("RIFF", deploy_script)
        self.assertIn("WAVE", deploy_script)


if __name__ == "__main__":
    unittest.main()
