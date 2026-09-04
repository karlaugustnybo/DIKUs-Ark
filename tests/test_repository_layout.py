"""Keep the code release independent of local archives and private artifacts."""

from __future__ import annotations

import ast
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ark_pipeline.runtime.checkpoints import pipeline_code
from scripts.check_release import REQUIRED_FILES, worktree_errors

ROOT = Path(__file__).resolve().parents[1]


class RepositoryLayoutTests(unittest.TestCase):
    @unittest.skipUnless((ROOT / ".git").exists(), "Git ignore audit requires a checkout")
    def test_supported_source_files_are_not_gitignored(self):
        paths = [
            path.relative_to(ROOT).as_posix()
            for directory in ("ark_pipeline", "backend", "scripts", "tests")
            for path in (ROOT / directory).rglob("*.py")
        ]
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--stdin", "-z"],
            input="\0".join(paths) + "\0", text=True,
            capture_output=True, cwd=ROOT, check=False,
        )
        self.assertEqual(result.returncode, 1, result.stdout or result.stderr)
        self.assertEqual(result.stdout, "")

    def test_supported_python_does_not_import_retired_packages(self):
        for directory in ("ark_pipeline", "backend", "scripts", "tests"):
            for path in (ROOT / directory).rglob("*.py"):
                for node in ast.walk(ast.parse(path.read_text())):
                    modules = []
                    if isinstance(node, ast.ImportFrom):
                        modules = [node.module or ""]
                    elif isinstance(node, ast.Import):
                        modules = [alias.name for alias in node.names]
                    for module in modules:
                        with self.subTest(path=path, module=module):
                            self.assertNotIn(module.split(".")[0], {"archive", "research", "app"})

    def test_documentation_links_resolve_without_the_local_archive(self):
        documents = [ROOT / name for name in ("README.md", "NOTICE.md", "DATA_POLICY.md", "ai_declaration.md")]
        documents.extend((ROOT / "docs").rglob("*.md"))
        documents.extend((ROOT / "ark_pipeline").rglob("*.md"))
        for document in documents:
            for target in re.findall(r"\]\(([^)\s]+)\)", document.read_text()):
                if re.match(r"^[a-z]+:|^#|^/", target, re.IGNORECASE):
                    continue
                path = (document.parent / target.split("#")[0]).resolve()
                with self.subTest(document=document, target=target):
                    self.assertTrue(path.exists(), f"Broken documentation link: {target}")
                    self.assertFalse(path.is_relative_to(ROOT / "archive"))

    def test_checkpoints_cover_nested_builders_and_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "justfile").write_text("# fixture\n")
            for directory in ("builders", "cli", "runtime", "spatial", "aggregation"):
                path = root / "ark_pipeline" / directory / "fixture.py"
                path.parent.mkdir(parents=True)
                path.write_text("# before\n")
                before = pipeline_code(root)
                path.write_text("# after\n")
                self.assertNotEqual(before, pipeline_code(root))


class ReleaseGuardTests(unittest.TestCase):
    def check_paths(self, names):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = []
            for name in [*REQUIRED_FILES, *names]:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("safe fixture\n")
                paths.append(path)
            with patch("scripts.check_release.ROOT", root):
                return worktree_errors(paths)

    def test_environment_files_are_blocked_even_if_force_added(self):
        for name in (".env", ".env.production", "frontend/.env.local"):
            with self.subTest(name=name):
                self.assertTrue(any("environment secrets" in error for error in self.check_paths([name])))

    def test_example_environment_files_are_allowed(self):
        self.assertEqual(self.check_paths([".env.example", "frontend/.env.example"]), [])

    def test_local_only_directories_are_blocked_even_if_force_added(self):
        for name in ("archive/experiment.py", "acquisition/manifest.json", "frontend/node_modules/index.js", ".tmp/report.md"):
            with self.subTest(name=name):
                self.assertTrue(any("local-only artifact" in error for error in self.check_paths([name])))


if __name__ == "__main__":
    unittest.main()
