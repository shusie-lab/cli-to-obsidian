import ast
import copy
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import sync_shared


class TestSyncShared(unittest.TestCase):
    def copy_generation_source(self, root: Path) -> None:
        (root / "shared").mkdir()
        for source in (sync_shared.ROOT / "shared").iterdir():
            shutil.copy2(source, root / "shared" / source.name)
        for target in {item for values in sync_shared.BLOCK_TARGETS.values() for item in values}:
            shutil.copy2(sync_shared.ROOT / target, root / target)

    def test_checked_in_scripts_match_shared_sources(self):
        self.assertEqual(sync_shared.check(), [])
        for targets in sync_shared.BLOCK_TARGETS.values():
            for target in targets:
                content = (sync_shared.ROOT / target).read_text(encoding="utf-8")
                self.assertNotIn("import shared", content)
                self.assertNotIn("from shared", content)

    def test_no_identical_top_level_function_remains_outside_generated_blocks(self):
        implementations = {}
        for script_name in ("agy_save.py", "claude_save.py", "codex_save.py", "opencode_save.py", "pi_save.py"):
            content = (sync_shared.ROOT / script_name).read_text(encoding="utf-8")
            generated_ranges = []
            start = None
            for line_number, line in enumerate(content.splitlines(), 1):
                if line.startswith("# BEGIN GENERATED:"):
                    start = line_number
                elif line.startswith("# END GENERATED:") and start is not None:
                    generated_ranges.append((start, line_number))
                    start = None
            for node in ast.parse(content).body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if any(first <= node.lineno <= last for first, last in generated_ranges):
                    continue
                implementation = ast.dump(node, include_attributes=False)
                implementations.setdefault(implementation, []).append(
                    f"{script_name}:{node.lineno}:{node.name}"
                )

        duplicates = [locations for locations in implementations.values() if len(locations) > 1]
        self.assertEqual(duplicates, [])

    def test_equivalent_functions_have_one_generated_source(self):
        class Canonical(ast.NodeTransformer):
            def visit_FunctionDef(self, node):
                node = self.generic_visit(node)
                node.returns = None
                node.type_comment = None
                if (node.body and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and isinstance(node.body[0].value.value, str)):
                    node.body.pop(0)
                return node

            def visit_arg(self, node):
                node.annotation = None
                node.type_comment = None
                return node

            def visit_AnnAssign(self, node):
                return ast.Assign(targets=[self.visit(node.target)], value=self.visit(node.value))

        implementations = {}
        for script in sorted({name for targets in sync_shared.BLOCK_TARGETS.values() for name in targets}):
            content = (sync_shared.ROOT / script).read_text(encoding="utf-8")
            source_by_line = {}
            block = None
            for index, line in enumerate(content.splitlines(), 1):
                if line.startswith("# BEGIN GENERATED: "):
                    block = line.split(": ", 1)[1]
                source_by_line[index] = block
                if line.startswith("# END GENERATED: "):
                    block = None
            for node in ast.parse(content).body:
                if not isinstance(node, ast.FunctionDef):
                    continue
                signature = ast.dump(Canonical().visit(copy.deepcopy(node)), include_attributes=False)
                implementations.setdefault(signature, []).append((script, node.name, source_by_line[node.lineno]))
        for copies in implementations.values():
            if len(copies) < 2:
                continue
            sources = {item[2] for item in copies}
            self.assertNotIn(None, sources, copies)
            self.assertEqual(len(sources), 1, copies)

    def test_common_infrastructure_is_registered_for_all_five(self):
        scripts = {"codex_save.py", "agy_save.py", "claude_save.py", "opencode_save.py", "pi_save.py"}
        for block in ("utilities", "logging-setup", "session-storage", "note-metadata",
                      "find-existing-markdown", "markdown-rendering", "full-session-display"):
            self.assertEqual(set(sync_shared.BLOCK_TARGETS[block]), scripts, block)
        self.assertEqual(set(sync_shared.BLOCK_TARGETS["usage-summary"]), scripts - {"agy_save.py"})
        self.assertEqual(set(sync_shared.BLOCK_TARGETS["usage-reference-cost"]),
                         scripts - {"agy_save.py"})

    def test_check_detects_a_stale_generated_block(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.copy_generation_source(root)

            stale = root / "claude_save.py"
            stale.write_text(
                stale.read_text(encoding="utf-8").replace(
                    "連続発言の判定にも使うUser calloutヘッダー",
                    "古いUser calloutヘッダー",
                    1,
                ),
                encoding="utf-8",
            )
            self.assertEqual(sync_shared.check(root), ["claude_save.py"])

    def test_invalid_shared_source_does_not_modify_any_script(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.copy_generation_source(root)
            targets = [root / target for values in sync_shared.BLOCK_TARGETS.values() for target in values]
            before = {path: path.read_bytes() for path in set(targets)}
            source = root / "shared" / "markdown_rendering.py.inc"
            source.write_text("def broken(:\n    pass\n", encoding="utf-8")

            with self.assertRaises(SyntaxError):
                sync_shared.write(root)
            self.assertEqual({path: path.read_bytes() for path in before}, before)

    def standalone_case(self, script_name: str, root: Path) -> tuple:
        transcript = root / f"{script_name}.jsonl"
        if script_name == "codex_save.py":
            records = [
                {
                    "type": "event_msg",
                    "timestamp": "2026-09-14T10:00:00+09:00",
                    "payload": {"type": "user_message", "message": "Standalone question"},
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-09-14T10:00:01+09:00",
                    "payload": {"type": "agent_message", "message": "Standalone answer"},
                },
            ]
            hook = {
                "session_id": "standalone-codex",
                "transcript_path": str(transcript),
                "cwd": str(root / "project"),
                "model": "codex-test",
            }
        elif script_name == "claude_save.py":
            records = [
                {
                    "type": "user",
                    "timestamp": "2026-09-14T10:00:00+09:00",
                    "message": {"content": [{"type": "text", "text": "Standalone question"}]},
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-09-14T10:00:01+09:00",
                    "message": {
                        "model": "claude-test",
                        "content": [{"type": "text", "text": "Standalone answer"}],
                    },
                },
            ]
            hook = {
                "session_id": "standalone-claude",
                "transcript_path": str(transcript),
                "cwd": str(root / "project"),
            }
        elif script_name == "agy_save.py":
            records = [
                {
                    "step_index": 1,
                    "type": "USER_INPUT",
                    "created_at": "2026-09-14T10:00:00+09:00",
                    "content": "<USER_REQUEST>Standalone question</USER_REQUEST>",
                },
                {
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-09-14T10:00:01+09:00",
                    "content": "Standalone answer",
                    "tool_calls": [],
                },
            ]
            hook = {
                "conversationId": "standalone-agy",
                "transcriptPath": str(transcript),
                "workspacePaths": [str(root / "project")],
            }
        elif script_name == "pi_save.py":
            entries = [
                {"id": "u1", "type": "message", "timestamp": "2026-10-05T01:00:00Z",
                 "message": {"role": "user", "content": "Standalone question"}},
                {"id": "a1", "type": "message", "timestamp": "2026-10-05T01:00:01Z",
                 "message": {"role": "assistant", "model": "gpt-6-luna",
                             "content": [{"type": "text", "text": "Standalone answer"}],
                             "usage": {"input": 10, "output": 2}}},
            ]
            return None, {"session_id": "standalone-pi", "cwd": str(root / "project"),
                          "created": entries[0]["timestamp"], "entries": entries, "branch": entries}
        else:
            return None, {
                "session_id": "standalone-opencode",
                "cwd": str(root / "project"),
                "created": 1789347600000,
                "messages": [
                    {
                        "info": {"id": "u1", "role": "user", "time": {"created": 1789347600000}},
                        "parts": [{"type": "text", "text": "Standalone question"}],
                    },
                    {
                        "info": {
                            "id": "a1",
                            "role": "assistant",
                            "modelID": "opencode-test",
                            "time": {"created": 1789347601000},
                        },
                        "parts": [{"type": "text", "text": "Standalone answer"}],
                    },
                ],
            }

        transcript.write_text(
            "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
            encoding="utf-8",
        )
        return transcript, hook

    def test_each_generated_script_saves_and_deduplicates_without_shared_files(self):
        for script_name in ("agy_save.py", "claude_save.py", "codex_save.py", "opencode_save.py", "pi_save.py"):
            with self.subTest(script=script_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                script = root / script_name
                shutil.copy2(sync_shared.ROOT / script_name, script)
                _transcript, hook = self.standalone_case(script_name, root)
                environment = os.environ.copy()
                environment["HOME"] = str(root / "home")
                environment["OBSIDIAN_VAULT"] = str(root / "vault")
                environment["OBSIDIAN_OUTPUT_DIR"] = "ChatLog"
                environment["PATH"] = "/usr/bin:/bin"
                environment.pop("OPENROUTER_API_KEY", None)

                def run_script():
                    return subprocess.run(
                        ["/usr/bin/python3", str(script)],
                        cwd=str(root),
                        env=environment,
                        input=json.dumps(hook, ensure_ascii=False),
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=10,
                        check=False,
                    )

                first = run_script()
                self.assertEqual(first.returncode, 0, first.stderr)
                self.assertNotIn("Traceback", first.stderr)
                self.assertNotIn("保存に失敗", first.stderr)

                markdown_files = list((root / "vault").rglob("*.md"))
                state_files = list((root / "home").rglob("*.json"))
                self.assertEqual(len(markdown_files), 1, markdown_files)
                self.assertEqual(len(state_files), 1, state_files)
                state = json.loads(state_files[0].read_text(encoding="utf-8"))
                self.assertEqual(Path(state["output_path"]), markdown_files[0])
                before = markdown_files[0].read_bytes()
                content = before.decode("utf-8")
                self.assertIn('title: "Standalone question"', content)
                self.assertIn("# Standalone question", content)
                self.assertNotIn("# User: Standalone question", content)
                self.assertEqual(content.count("> Standalone question"), 1)
                self.assertEqual(content.count("Standalone answer"), 1)
                self.assertIn("message_count: 2", content)

                second = run_script()
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertNotIn("Traceback", second.stderr)
                self.assertNotIn("保存に失敗", second.stderr)
                self.assertEqual(list((root / "vault").rglob("*.md")), markdown_files)
                self.assertEqual(list((root / "home").rglob("*.json")), state_files)
                self.assertEqual(markdown_files[0].read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
