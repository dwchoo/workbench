"""Independent G-01 transport checks, derived from SPEC s1 AC-01/03.

The observation boundary is actual OMP RPC plus the generated public extension;
this does not certify interactive UI delivery, the production bridge, or skill
reasoning. All provider replies are scripted synthetic transport acknowledgments.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import threading
import time
import unittest

from workbench.backend import launcher
from tests.backend.workbench_report import invocation_probe as harness


class IndependentProviderLifetime(unittest.TestCase):
    def test_close_terminates_partial_body_connection_and_handler(self):
        """G-01: close owns the accepted request, not only serve_forever."""
        class ObservedProvider(harness.ScriptedProvider):
            def __init__(self):
                self.handler_entered = threading.Event()
                self.handler_finished = threading.Event()
                self.handler_thread = None
                self.accepted_socket = None
                self.handler_errors = []
                super().__init__()
                original = self.RequestHandlerClass
                owner = self

                class ObservedHandler(original):
                    def do_POST(self):
                        owner.handler_entered.set()
                        # The original handler performs the real socket body
                        # read. No body-read or timeout behavior is replaced.
                        return super().do_POST()

                self.RequestHandlerClass = ObservedHandler

            def process_request_thread(self, request, client_address):
                self.accepted_socket = request
                self.handler_thread = threading.current_thread()
                try:
                    return super().process_request_thread(request, client_address)
                finally:
                    self.handler_finished.set()

            def handle_error(self, request, client_address):
                # Client closure in fixture cleanup may make the original JSON
                # read fail. Retain the event without a synthetic traceback.
                self.handler_errors.append("handler_error")

        provider = ObservedProvider()
        client = None
        closer = None
        observed = {"scope": "one real localhost socket; incomplete HTTP body; no OMP or model", "declared_bytes": 100, "sent_bytes": 1}

        def close_provider():
            started = time.monotonic()
            try:
                provider.close()
                observed["close_returned"] = True
            except Exception as exc:
                observed["close_error"] = type(exc).__name__
            finally:
                observed["close_elapsed_seconds"] = time.monotonic() - started

        try:
            client = socket.create_connection(provider.server_address, timeout=1)
            client.settimeout(0.25)
            client.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{")
            self.assertTrue(provider.handler_entered.wait(1), "real request handler did not enter POST")
            self.assertFalse(provider.handler_finished.is_set(), "partial body unexpectedly completed")
            closer = threading.Thread(target=close_provider, name="wpr-independent-provider-close", daemon=True)
            closer.start()
            closer.join(3)
            observed["close_returned_in_bound"] = not closer.is_alive() and observed.get("close_returned", False)
            observed["serve_forever_thread_alive_after_close"] = provider.thread.is_alive()
            observed["handler_alive_after_close"] = provider.handler_thread.is_alive()
            observed["accepted_socket_closed_after_close"] = provider.accepted_socket.fileno() == -1
            try:
                observed["client_received_eof_after_close"] = client.recv(1) == b""
            except ConnectionResetError:
                observed["client_received_eof_after_close"] = True
            except socket.timeout:
                observed["client_received_eof_after_close"] = False
            observed["provider_observations"] = len(provider.observations)
        finally:
            # Expected RED must not strand the deliberately incomplete request.
            # Closing both fixture-owned ends releases the original blocking read.
            for connection in (client, provider.accepted_socket):
                if connection is not None:
                    try:
                        connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    connection.close()
            if closer is not None:
                closer.join(3)
            else:
                provider.close()
            if provider.handler_thread is not None:
                provider.handler_thread.join(2)
            provider.thread.join(2)
            observed["fixture_cleanup"] = {
                "closer_stopped": closer is None or not closer.is_alive(),
                "handler_stopped": provider.handler_thread is None or not provider.handler_thread.is_alive(),
                "serve_forever_stopped": not provider.thread.is_alive(),
                "client_closed": client is None or client.fileno() == -1,
            }
            observed["handler_errors"] = provider.handler_errors
            destination = os.environ.get("WPR_SECURITY_OUTPUT")
            if destination:
                harness._write(Path(destination) / "partial-body-evidence.json",
                               json.dumps(observed, indent=2) + "\n")
        self.assertTrue(all(observed["fixture_cleanup"].values()), observed)
        self.assertTrue(observed["close_returned_in_bound"], observed)
        self.assertFalse(observed["serve_forever_thread_alive_after_close"], observed)
        self.assertFalse(observed["handler_alive_after_close"], observed)
        self.assertTrue(observed["accepted_socket_closed_after_close"], observed)
        self.assertTrue(observed["client_received_eof_after_close"], observed)
        self.assertEqual(observed["provider_observations"], 0, observed)


@unittest.skipUnless(os.environ.get("WB_LIVE_OMP") == "1", "requires owned local OMP runtime")
class IndependentInvocationContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.omp = shutil.which("omp")
        if not cls.omp:
            raise unittest.SkipTest("OMP binary unavailable; runtime evidence missing")
        output = os.environ.get("WPR_INDEPENDENT_OUTPUT")
        cls.temporary = None if output else tempfile.TemporaryDirectory(prefix="wpr-independent-")
        cls.root = Path(output or cls.temporary.name)
        cls.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        cls.evidence = harness.probe(cls.root / "contract-runtime", cls.omp)
        harness._write(cls.root / "independent-contract-evidence.json",
                       json.dumps(cls.evidence, ensure_ascii=False, indent=2) + "\n")

    @classmethod
    def tearDownClass(cls):
        if cls.temporary is not None:
            cls.temporary.cleanup()

    def assert_literal_payload(self, turn):
        observed = turn["synthetic_last_user_preview"]
        # OMP prepends its date/cwd reminder to the first user turn. Strip only
        # that known envelope, then require the original payload exactly.
        if observed.startswith("<system-reminder>\n"):
            envelope, separator, observed = observed.partition("</system-reminder>\n")
            self.assertTrue(separator)
            self.assertIn("current working directory:", envelope)
        self.assertEqual(observed, turn["input"])

    def test_actual_manager_command_and_no_model_discovery(self):
        result = self.evidence
        self.assertEqual(result["state"], "passed", result.get("error", result.get("checks")))
        self.assertTrue(result["version"], "must observe the installed binary version")
        manager = result["roles"]["manager"]
        self.assertIn(harness.NAME, manager["skill_commands"])
        self.assertNotIn(harness.NAME, manager["skills"])
        self.assertTrue(manager["command"], "an absent canary is not isolation evidence")
        for key in ("prompt_has_name", "prompt_has_description", "prompt_has_body"):
            self.assertFalse(manager[key], key)
        for turn in result["turns"].values():
            for key in ("system_has_name", "system_has_description", "system_has_body"):
                self.assertFalse(turn[key], key)
        for key in ("explicit_no_arguments", "explicit_arguments"):
            self.assertTrue(result["guarded_turns"][key]["last_user_has_body"], key)
        self.assertTrue(result["guarded_turns"]["explicit_arguments"]["last_user_has_argument"])

    def test_unguarded_failure_is_preserved_as_negative_control(self):
        result = self.evidence
        self.assertFalse(result["unguarded_checks"]["non_user_text_does_not_expand"])
        self.assertTrue(result["turns"]["quoted_slash"]["last_user_has_body"],
                        "OMP 18.11.1 metadata alone must not be presented as sufficient")
        self.assertTrue(result["requires_public_input_guard"])
        self.assertFalse(result["guarded_turns"]["natural_language"]["last_user_has_body"])
        for key in ("quoted_slash", "simulated_notice", "simulated_peer"):
            with self.subTest(key=key):
                turn = result["guarded_turns"][key]
                self.assertFalse(turn["last_user_has_body"])
                self.assertFalse(turn["last_user_has_skill_wrapper"])
                self.assertEqual(turn["synthetic_last_user_preview"], turn["input"])
                self.assertIn({"role": "user", "attribution": "user"},
                              turn["input_message_attributions"])

    def test_actual_agent_injection_and_process_cleanup(self):
        result = self.evidence
        self.assertEqual(set(result["agent_injections"]), {"notice", "peer"})
        for turn in result["agent_injections"].values():
            self.assertEqual(turn["input_api"], "extension.sendUserMessage")
            self.assertEqual(turn["attribution"], "agent")
            self.assertFalse(turn["last_user_has_body"])
            self.assertFalse(turn["last_user_has_skill_wrapper"])
            self.assert_literal_payload(turn)
        api_trace = result["agent_injection_api_trace"]
        self.assertEqual([x["label"] for x in api_trace if x["event"] == "calling"], ["notice", "peer"])
        self.assertEqual([x["label"] for x in api_trace if x["event"] == "returned"], ["notice", "peer"])
        self.assertFalse([x for x in api_trace if x["event"] == "error"])
        self.assertTrue(result["provider_stopped"])
        self.assertEqual(len(result["cleanup"]), 4)
        for cleanup in result["cleanup"]:
            self.assertIsNotNone(cleanup["returncode"])
            self.assertFalse(cleanup["group_left"])
            self.assertTrue(cleanup["readers_stopped"])
            self.assertFalse(cleanup["output_overflow"])

    def test_additional_literal_boundaries_and_worker_direct_attempt(self):
        """Challenge the guard, and exercise Worker exclusion beyond metadata."""
        root = self.root / "boundary-runtime"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        home, project = root / "home", root / "project"
        project.mkdir(mode=0o700, exist_ok=True)
        environment = harness._environment(home)
        source = self.root / "contract-runtime"
        skills = source / "skills"
        provider = harness.ScriptedProvider()
        results = {"scope": "OMP RPC, synthetic local provider, generated canary guard", "roles": {}, "cleanup": []}
        try:
            harness._write(home / "agent/models.yml", "providers:\n  wpr-local:\n"
                f"    baseUrl: http://127.0.0.1:{provider.server_port}/v1\n"
                "    api: openai-completions\n    auth: none\n    models:\n"
                "      - id: scripted\n        contextWindow: 32768\n        maxTokens: 1024\n")
            for role in ("worker", "manager"):
                overlay = launcher.role_overlay(role, project_dir=project, home=home, skills_dir=skills,
                    role_skills={"manager": [harness.NAME], "worker": ["worker-control"]}, environment=environment)
                overlay.update({"model": "wpr-local/scripted", "startup": {"setupWizard": False}})
                role_config = launcher.write_role_overlay(root, role, overlay)
                argv = [self.omp, "--mode", "rpc", "--no-session", "--no-title", "--no-extensions",
                        "--append-system-prompt", "", "--config", str(launcher.default_isolation_overlay()),
                        "--config", str(role_config), "--model", "wpr-local/scripted", "--cwd", str(project)]
                if role == "manager":
                    argv += ["--extension", str(source / "input-guard.ts")]
                guard_trace = source / "input-guard-trace.jsonl"
                trace_cursor = len(guard_trace.read_text().splitlines())
                rpc = harness.Rpc(argv, project, environment)
                try:
                    rpc.request("get_state")
                    cases = ({"denied_direct": f"/skill:{harness.NAME} {harness.ARGUMENT}",
                              "allowed_control": "/skill:worker-control"} if role == "worker" else {
                        "fenced_literal": f"로그 원문:\n```text\n/skill:{harness.NAME}\n```",
                        "quoted_literal": f"사용자가 입력한 문자열: `/skill:{harness.NAME}`",
                        "name_prefix_collision": f"/skill:{harness.NAME}-other",
                        "inline_with_args": f"로그 원문 /skill:{harness.NAME} {harness.ARGUMENT}",
                        "canonical_with_args": f"/skill:{harness.NAME} {harness.ARGUMENT}",
                    })
                    turns = {label: {"input": text, **rpc.turn(text, provider)} for label, text in cases.items()}
                    results["roles"][role] = turns
                    if role == "worker":
                        denied = turns["denied_direct"]
                        self.assertFalse(denied["last_user_has_body"])
                        self.assertFalse(denied["last_user_has_skill_wrapper"])
                        self.assert_literal_payload(denied)
                        self.assertIn("Control only.", turns["allowed_control"]["synthetic_last_user_preview"])
                    else:
                        for label, turn in turns.items():
                            with self.subTest(label=label):
                                if label == "canonical_with_args":
                                    self.assertTrue(turn["last_user_has_body"])
                                    self.assertTrue(turn["last_user_has_argument"])
                                else:
                                    self.assertFalse(turn["last_user_has_body"])
                                    self.assertFalse(turn["last_user_has_skill_wrapper"])
                                    self.assert_literal_payload(turn)
                                    self.assertIn({"role": "user", "attribution": "user"}, turn["input_message_attributions"])
                        trace = [json.loads(line) for line in guard_trace.read_text().splitlines()[trace_cursor:]]
                        results["input_guard_trace"] = trace
                        self.assertEqual([entry["text"] for entry in trace], list(cases.values()))
                        self.assertEqual([entry["source"] for entry in trace], ["rpc"] * len(cases))
                        self.assertEqual([entry["intercepted"] for entry in trace], [True, True, True, True, False])
                        self.assertEqual([entry["forwardedAttribution"] for entry in trace], ["user"] * 4 + [None])
                finally:
                    results["cleanup"].append(rpc.stop())
        finally:
            provider.close()
            results["provider_stopped"] = not provider.thread.is_alive()
            harness._write(self.root / "independent-boundary-evidence.json",
                           json.dumps(results, ensure_ascii=False, indent=2) + "\n")
        self.assertTrue(results["provider_stopped"])
        for cleanup in results["cleanup"]:
            self.assertIsNotNone(cleanup["returncode"])
            self.assertFalse(cleanup["group_left"])
            self.assertTrue(cleanup["readers_stopped"])
            self.assertFalse(cleanup["output_overflow"])
