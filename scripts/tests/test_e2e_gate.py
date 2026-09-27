"""Release-gate regressions; runnable with stdlib unittest without services."""
import ast
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
from typing import Optional
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InstallerTests(unittest.TestCase):
    def test_served_script_reads_coordinator_repo_and_tag(self):
        installer = load_module("installer", ROOT / "coordinator/services/agent_installer.py")
        source = ROOT / "coordinator/main.py"
        node = next(n for n in ast.parse(source.read_text()).body
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "agent_bootstrap_script")
        node.decorator_list = []
        scope = {"Request": object, "settings": SimpleNamespace(writ_public_url="http://localhost:18000"),
                 "os": os, "agent_installer": installer}
        response_module = SimpleNamespace(PlainTextResponse=lambda text, **kwargs: text)
        with patch.dict(os.environ, {"WRIT_AGENT_REPO": "fork/writ-agent", "WRIT_AGENT_TAG": "v1.2.3"}), \
             patch.dict("sys.modules", {"fastapi.responses": response_module}):
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), scope)
            script = asyncio.run(scope["agent_bootstrap_script"](SimpleNamespace(base_url="http://wrong")))
        # Execute the variable declarations the served script actually sends.
        declarations = script[:script.index("# --- Download-only mode")]
        output = subprocess.check_output(["sh", "-s"], input=declarations + '\nprintf "%s\\n" "$RELEASE_API" "$COORDINATOR"\n', text=True)
        self.assertEqual(output.splitlines(), ["https://api.github.com/repos/fork/writ-agent/releases/tags/v1.2.3", "http://localhost:18000"])

    def test_installer_uses_exact_tag_and_fork_but_defaults_to_latest(self):
        installer = load_module("installer", ROOT / "coordinator/services/agent_installer.py")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bin").mkdir()
            curl = root / "bin/curl"
            curl.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$REQUESTS"\nprintf "{}"\n')
            curl.chmod(0o755)
            for tag, suffix in (("v1.2.3", "tags/v1.2.3"), ("release/1", "tags/release%2F1"), ("", "latest")):
                with self.subTest(tag=tag):
                    log = root / "requests"
                    log.unlink(missing_ok=True)
                    script = installer.render("http://localhost:8000", "fork-owner/writ-agent", tag)
                    subprocess.run(["sh", "-s", "--", "--download-only"], input=script,
                                   text=True, capture_output=True, env={**os.environ,
                                   "PATH": f"{root / 'bin'}:{os.environ['PATH']}",
                                   "WRIT_HOME": str(root / "home"), "REQUESTS": str(log)})
                    self.assertEqual(log.read_text().splitlines(),
                                     [f"-fsSL https://api.github.com/repos/fork-owner/writ-agent/releases/{suffix}"])


class McpDispatchTests(unittest.TestCase):
    def run_handler(self, responses, *, dispatch=None, wait=True):
        """Only the HTTP boundary is fake; execute the shipped async handler."""
        source = ROOT / "coordinator/routers/mcp_server.py"
        tree = ast.parse(source.read_text())
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_workflow_id")
        terminal = next(n for n in tree.body if isinstance(n, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "_TERMINAL" for t in n.targets))
        clock = [1000.0]
        calls = []

        async def call(method, path, token, **kwargs):
            calls.append((method, path, kwargs))
            if method == "POST":
                return dispatch if dispatch is not None else {"task_id": 42}
            if path == "/api/runs":
                return [{"id": "workflow-41", "status": "success", "started_at": "2020-01-01T00:00:00Z"}]
            if path.endswith("/data"):
                return {"columns": ["value"], "rows": [{"value": kwargs["params"]["run_id"]}]}
            self.assertEqual(path, "/api/automation/tasks/42/results")
            return responses.pop(0) if len(responses) > 1 else responses[0]

        async def sleep(seconds):
            clock[0] += seconds

        scope = {"Optional": Optional, "time": SimpleNamespace(time=lambda: clock[0]),
                 "_call": call, "_sleep": sleep, "_Upstream": RuntimeError,
                 }
        exec(compile(ast.Module(body=[terminal, node], type_ignores=[]), str(source), "exec"), scope)
        result = asyncio.run(scope["_run_workflow_id"]("token", {"id": 7}, {}, wait, 5))
        return result, calls

    def test_previous_rest_success_cannot_satisfy_new_mcp_dispatch(self):
        result, _ = self.run_handler([{"task_id": 42, "status": "failed", "error": "did not complete"}])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "did not complete")

    def test_successful_data_belongs_to_dispatched_task(self):
        result, _ = self.run_handler([{"task_id": 42, "status": "running"}, {"task_id": 42, "status": "success"}])
        self.assertEqual(result["rows"], [{"value": 42}])

    def test_dispatch_without_identity_fails_closed(self):
        result, _ = self.run_handler([], dispatch={})
        self.assertEqual(result["status"], "failed")
        self.assertIn("error", result)

    def test_wait_budget_does_not_turn_old_success_into_new_success(self):
        result, _ = self.run_handler([{"task_id": 42, "status": "running"}])
        self.assertEqual(result["status"], "running")
        self.assertTrue(result["retryable"])

    def test_async_dispatch_does_not_poll(self):
        result, calls = self.run_handler([], wait=False)
        self.assertEqual(result["status"], "dispatched")
        self.assertEqual(len(calls), 1)

    def test_terminal_timeout_is_returned_without_retry_hint(self):
        result, calls = self.run_handler([{"task_id": 42, "status": "timeout", "error": "timed out"}])
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("retryable", result)

    def test_mismatched_task_result_is_not_accepted(self):
        result, _ = self.run_handler([{"task_id": 41, "status": "success"}])
        self.assertEqual(result["status"], "running")
        self.assertNotIn("rows", result)


class GateHelperTests(unittest.TestCase):
    def setUp(self):
        self.helper = load_module("release_e2e", ROOT / "scripts/release_e2e.py")

    def test_mcp_requires_successful_terminal_json_not_hopeful_words(self):
        for data in ({"status": "failed", "error": "did not complete", "task_id": 42},
                     {"status": "running", "note": "success soon", "task_id": 42},
                     {"status": "success", "error": "bad", "task_id": 42},
                     {"status": "success", "task_id": 42, "_cache": {"hit": True}}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.helper.validate_run(self.helper.mcp_payload({"result": {
                    "content": [{"type": "text", "text": json.dumps(data)}]}}))

    def test_rpc_and_tool_errors_and_malformed_content_fail_closed(self):
        for response in ({"error": {"message": "complete"}},
                         {"result": {"isError": True, "content": []}},
                         {"result": {"content": [{"type": "text", "text": "success"}]}},
                         {"result": {"content": []}}):
            with self.subTest(response=response), self.assertRaises(ValueError):
                self.helper.mcp_payload(response)

    def test_fresh_task_identity_and_workflow_must_match(self):
        data = {"status": "success", "task_id": 42, "workflow_id": 7}
        self.assertEqual(self.helper.validate_run(data, previous_task=41, workflow_id=7), 42)
        with self.assertRaises(ValueError):
            self.helper.validate_run(data, previous_task=42, workflow_id=7)
        with self.assertRaises(ValueError):
            self.helper.validate_run(data, previous_task=41, workflow_id=8)
        for identity in (None, 0, True, ""):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                self.helper.validate_run({**data, "task_id": identity})

    def test_env_config_replaces_stale_repo_and_doc_url_and_preserves_secrets(self):
        configured = self.helper.configure_env(
            "WRIT_AGENT_REPO=old/repo\nWRIT_PUBLIC_URL=https://old\nAPI_SECRET_KEY=secret\nDOC_EXTRACT_URL=http://old:8092\n",
            "http://localhost:18000", "18000", "18092", "fork/writ-agent", "v1.2.3")
        values = dict(line.split("=", 1) for line in configured.splitlines())
        self.assertEqual(values["WRIT_AGENT_REPO"], "fork/writ-agent")
        self.assertEqual(values["WRIT_AGENT_TAG"], "v1.2.3")
        self.assertEqual(values["API_SECRET_KEY"], "secret")
        self.assertEqual(values["WRIT_PUBLIC_URL"], "http://localhost:18000")
        self.assertEqual(values["DOC_EXTRACT_URL"], "http://127.0.0.1:18092")
        self.assertEqual(values["WRIT_HOST_PORT"], "18000")
        self.assertEqual(values["WRIT_DOC_EXTRACT_HOST_PORT"], "18092")

    def test_release_inputs_cannot_be_mutable_alias_or_env_injection(self):
        for repo, tag in (("fork/writ-agent", "latest"), ("fork/writ-agent\nX=y", "v1"),
                          ("fork/writ-agent", "v1\nX=y")):
            with self.subTest(repo=repo, tag=tag), self.assertRaises(ValueError):
                self.helper.validate_release(repo, tag)


class ScriptLifecycleTests(unittest.TestCase):
    def exercise_build_failure(self, *, original_env, tag="v1.2.3", signal=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            for filename in ("release-e2e.sh", "release_e2e.py", "gen-env.sh"):
                shutil.copy2(ROOT / "scripts" / filename, root / "scripts" / filename)
            shutil.copy2(ROOT / ".env.example", root / ".env.example")
            if original_env:
                (root / ".env").write_text("API_SECRET_KEY=original-secret\nWRIT_AGENT_REPO=old/repo\n")
            original = (root / ".env").read_bytes() if original_env else None
            bin_dir = root / "bin"
            bin_dir.mkdir()
            docker = bin_dir / "docker"
            docker.write_text('''#!/usr/bin/env python3
import json, os, pathlib, signal, sys
root = pathlib.Path(os.environ["TEST_ROOT"])
with (root / "docker-calls").open("a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if "up" in sys.argv:
    (root / "configured.env").write_bytes((root / ".env").read_bytes())
    override = pathlib.Path(sys.argv[sys.argv.index("-f", sys.argv.index("-f") + 1) + 1])
    (root / "override.json").write_bytes(override.read_bytes())
    home = pathlib.Path(os.environ["AGENT_HOME"])
    (home / "agent.log").write_text("agent diagnostic before cleanup")
    print("deliberate Docker build failure", flush=True)
    if os.environ.get("TEST_SIGNAL") == "1":
        os.kill(os.getppid(), signal.SIGTERM)
    sys.exit(9)
if "logs" in sys.argv:
    print("compose diagnostic before teardown")
''')
            curl = bin_dir / "curl"
            curl.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_ROOT/curl-calls"\nprintf \'{"tag_name":"v9.8.7"}\'\n')
            lsof = bin_dir / "lsof"
            lsof.write_text("#!/bin/sh\nexit 1\n")
            for binary in (docker, curl, lsof):
                binary.chmod(0o755)
            env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "TEST_ROOT": str(root),
                   "AGENT_HOME": str(root / "agent-home"), "WRIT_E2E_LOG_DIR": str(root / "logs"),
                   "WRIT_AGENT_REPO": "fork/writ-agent", "WRIT_AGENT_TAG": tag,
                   "WRIT_HOST_PORT": "18000", "WRIT_DOC_EXTRACT_HOST_PORT": "18092",
                   "TEST_SIGNAL": "1" if signal else "0"}
            result = subprocess.run(["bash", str(root / "scripts/release-e2e.sh")],
                                    env=env, text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 143 if signal else 1, result.stdout + result.stderr)
            self.assertEqual((root / ".env").read_bytes() if (root / ".env").exists() else None, original)
            self.assertFalse((root / "agent-home").exists())
            logs = root / "logs"
            self.assertIn("deliberate Docker build failure", (logs / "compose-build.log").read_text())
            self.assertIn("compose diagnostic before teardown", (logs / "compose.log").read_text())
            self.assertIn("agent diagnostic before cleanup", (logs / "agent.log").read_text())
            configured = dict(line.split("=", 1) for line in (root / "configured.env").read_text().splitlines()
                              if "=" in line and not line.startswith("#"))
            self.assertEqual(configured["WRIT_AGENT_REPO"], "fork/writ-agent")
            self.assertEqual(configured["WRIT_AGENT_TAG"], tag or "v9.8.7")
            override = json.loads((root / "override.json").read_text())
            self.assertEqual(override["services"]["coordinator"]["environment"]["WRIT_AGENT_TAG"], tag or "v9.8.7")
            self.assertEqual(override["services"]["coordinator"]["environment"]["WRIT_AGENT_REPO"], "fork/writ-agent")
            calls = [json.loads(line) for line in (root / "docker-calls").read_text().splitlines()]
            lifecycle = [args for args in calls if args[0] == "compose" and "version" not in args]
            self.assertTrue(lifecycle)
            projects = {args[args.index("--project-name") + 1] for args in lifecycle}
            self.assertEqual(len(projects), 1)
            self.assertTrue(next(iter(projects)).startswith("writ-release-e2e-"))
            self.assertEqual(sum("down" in args for args in lifecycle), 1)
            self.assertLess(next(i for i, args in enumerate(lifecycle) if "logs" in args),
                            next(i for i, args in enumerate(lifecycle) if "down" in args))
            curl_calls = (root / "curl-calls").read_text().splitlines() if (root / "curl-calls").exists() else []
            self.assertEqual(curl_calls, [] if tag else ["-fsSL --max-time 30 https://api.github.com/repos/fork/writ-agent/releases/latest"])

    def test_failure_keeps_diagnostics_and_restores_existing_env(self):
        self.exercise_build_failure(original_env=True)

    def test_latest_resolves_once_and_generated_env_is_removed(self):
        self.exercise_build_failure(original_env=False, tag="")

    def test_term_keeps_diagnostics_and_restores_existing_env(self):
        self.exercise_build_failure(original_env=True, signal=True)


if __name__ == "__main__":
    unittest.main()
