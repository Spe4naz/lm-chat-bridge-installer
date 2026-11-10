#!/usr/bin/env python3
"""Self-tests for lms_gpu_bridge.py (no LM Studio required).

Run: python3 tools/test_lms_gpu_bridge.py
"""

from __future__ import annotations

import importlib.util
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("lms_gpu_bridge.py")
SPEC = importlib.util.spec_from_file_location("lms_gpu_bridge", MODULE_PATH)
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)

TOKEN = "secret-token"


class FakeLmStudio(BaseHTTPRequestHandler):
    """Minimal streaming OpenAI-compatible endpoint."""

    protocol_version = "HTTP/1.1"
    calls: list[dict] = []
    delay = 0.0

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        FakeLmStudio.calls.append(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for content in ["При", "вет"]:
                if FakeLmStudio.delay:
                    time.sleep(FakeLmStudio.delay)
                chunk = json.dumps(
                    {"choices": [{"delta": {"content": content}}]},
                    ensure_ascii=False,
                )
                self.wfile.write(f"data: {chunk}\n\n".encode("utf-8"))
                self.wfile.flush()
            # LM Studio sends usage in a final chunk with empty choices.
            usage = json.dumps(
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 2,
                        "total_tokens": 7,
                    },
                }
            )
            self.wfile.write(f"data: {usage}\n\n".encode("utf-8"))
            self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except OSError:
            # The bridge may cancel the job and close the socket mid-write.
            pass
        self.close_connection = True

    def log_message(self, fmt, *args) -> None:  # noqa: A002
        pass


class BridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        bridge.TOKEN = TOKEN
        bridge.LMS_HOST = ""
        bridge.MAX_BODY = 65536
        cls.calls: list[list[str]] = []
        bridge.run_lms = cls._fake_run
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), bridge.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

        cls.lm = ThreadingHTTPServer(("127.0.0.1", 0), FakeLmStudio)
        cls.lm_port = cls.lm.server_address[1]
        cls.lm_thread = threading.Thread(target=cls.lm.serve_forever, daemon=True)
        cls.lm_thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.lm.shutdown()
        cls.lm.server_close()

    def use_fake_lms(self) -> None:
        bridge.LMS_HOST = f"127.0.0.1:{self.lm_port}"
        FakeLmStudio.calls.clear()
        FakeLmStudio.delay = 0.0

    @classmethod
    def _fake_run(cls, args, timeout=60):
        cls.calls.append(list(args))
        return "ok " + " ".join(args)

    def request(self, method: str, path: str, body=None, token=TOKEN):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_health_is_public(self) -> None:
        status, payload = self.request("GET", "/health", token=None)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["api"], bridge.API_VERSION)
        self.assertIn("chat", payload["features"])

    def test_load_requires_token(self) -> None:
        status, payload = self.request("POST", "/load", {"model": "m"}, token=None)
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], "unauthorized")

    def test_public_bind_requires_token(self) -> None:
        old_token, old_host = bridge.TOKEN, bridge.HOST
        try:
            bridge.TOKEN = ""
            bridge.HOST = "0.0.0.0"
            self.assertIsNotNone(bridge.startup_error())

            bridge.HOST = "127.0.0.1"
            self.assertIsNone(bridge.startup_error())

            bridge.TOKEN = "secret"
            bridge.HOST = "0.0.0.0"
            self.assertIsNone(bridge.startup_error())
        finally:
            bridge.TOKEN, bridge.HOST = old_token, old_host

    def test_load_max_with_context_and_ttl(self) -> None:
        status, payload = self.request(
            "POST",
            "/load",
            {
                "model": "google/gemma",
                "gpu": "max",
                "context_length": 8192,
                "ttl": 3600,
            },
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(
            self.calls[-1],
            [
                "load",
                "google/gemma",
                "--gpu",
                "max",
                "--context-length",
                "8192",
                "--ttl",
                "3600",
            ],
        )

    def test_load_ratio_and_identifier(self) -> None:
        status, payload = self.request(
            "POST",
            "/load",
            {"model": "m", "gpu": 0.5, "identifier": "phone"},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(
            self.calls[-1],
            ["load", "m", "--gpu", "0.5", "--identifier", "phone"],
        )

    def test_estimate_uses_estimate_only(self) -> None:
        status, payload = self.request(
            "POST", "/estimate", {"model": "m", "gpu": "max"}
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(
            self.calls[-1], ["load", "--estimate-only", "m", "--gpu", "max"]
        )

    def test_unload_model_and_all(self) -> None:
        status, _ = self.request("POST", "/unload", {"model": "m"})
        self.assertEqual(status, 200)
        self.assertEqual(self.calls[-1], ["unload", "m"])

        status, _ = self.request("POST", "/unload", {"all": True})
        self.assertEqual(status, 200)
        self.assertEqual(self.calls[-1], ["unload", "--all"])

    def test_invalid_gpu_is_rejected(self) -> None:
        status, payload = self.request(
            "POST", "/load", {"model": "m", "gpu": "nope"}
        )
        self.assertEqual(status, 400)
        self.assertIn("gpu", payload["error"])

    def test_missing_model_is_rejected(self) -> None:
        status, payload = self.request("POST", "/load", {"gpu": "max"})
        self.assertEqual(status, 400)
        self.assertIn("model", payload["error"])

    def test_lms_host_adds_flag(self) -> None:
        bridge.LMS_HOST = "192.168.1.50:1234"
        try:
            self.request("POST", "/load", {"model": "m", "gpu": "max"})
            self.assertEqual(
                self.calls[-1],
                ["load", "m", "--gpu", "max", "--host", "192.168.1.50:1234"],
            )
            self.request("GET", "/ps")
            self.assertIn("--host", self.calls[-1])
        finally:
            bridge.LMS_HOST = ""

    def test_ps_returns_parsed_json(self) -> None:
        status, payload = self.request("GET", "/ps")
        self.assertEqual(status, 200)
        # The fake lms prints plain text, so the bridge returns it as a string.
        self.assertIn("ps", payload["result"])

    def tearDown(self) -> None:
        bridge.LMS_HOST = ""
        bridge._jobs.clear()
        FakeLmStudio.delay = 0.0

    # ------------------------------------------------------------- chat jobs

    def _collect(self, job_id: str, since: int = 0):
        collected: list[str] = []
        next_seq = since
        payload: dict = {}
        for _ in range(30):
            status, payload = self.request(
                "GET", f"/chat/{job_id}?since={next_seq}&wait=5"
            )
            self.assertEqual(status, 200, payload)
            for event in payload["events"]:
                collected.append(event["content"])
            next_seq = payload["next"]
            if payload["done"]:
                break
        return "".join(collected), next_seq, payload

    def test_chat_job_streams_and_replays(self) -> None:
        self.use_fake_lms()
        status, payload = self.request(
            "POST",
            "/chat",
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "job": "job-1",
                "stream_options": {"include_usage": True},
            },
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["job"], "job-1")

        text, next_seq, last = self._collect("job-1")
        self.assertEqual(text, "Привет")
        self.assertTrue(last["done"])
        self.assertIsNone(last["error"])
        self.assertEqual(next_seq, 2)
        self.assertEqual(last["usage"]["completion_tokens"], 2)
        self.assertEqual(last["usage"]["total_tokens"], 7)
        self.assertGreaterEqual(last["elapsed_ms"], 0)
        self.assertEqual(FakeLmStudio.calls[0]["model"], "m")
        self.assertNotIn("job", FakeLmStudio.calls[0])
        self.assertEqual(
            FakeLmStudio.calls[0]["stream_options"], {"include_usage": True}
        )

        # Replaying from the beginning returns the same events.
        status, payload = self.request("GET", "/chat/job-1?since=0&wait=0")
        self.assertEqual(status, 200)
        self.assertEqual(
            [event["content"] for event in payload["events"]], ["При", "вет"]
        )

    def test_chat_is_idempotent_for_the_same_job(self) -> None:
        self.use_fake_lms()
        body = {
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "job": "job-same",
        }
        status, first = self.request("POST", "/chat", body)
        self.assertEqual(status, 200, first)
        status, second = self.request("POST", "/chat", body)
        self.assertEqual(status, 200, second)
        self.assertEqual(second["job"], "job-same")
        self.assertEqual(len(FakeLmStudio.calls), 1)

    def test_chat_requires_token(self) -> None:
        self.use_fake_lms()
        status, _ = self.request(
            "POST",
            "/chat",
            {"model": "m", "messages": [{"role": "user", "content": "x"}]},
            token=None,
        )
        self.assertEqual(status, 401)

    def test_chat_unknown_job_returns_404(self) -> None:
        status, payload = self.request("GET", "/chat/nope?since=0&wait=0")
        self.assertEqual(status, 404)
        self.assertFalse(payload["ok"])

    def test_chat_cancel_is_accepted(self) -> None:
        self.use_fake_lms()
        FakeLmStudio.delay = 0.2
        status, _ = self.request(
            "POST",
            "/chat",
            {
                "model": "m",
                "messages": [{"role": "user", "content": "x"}],
                "job": "job-cancel",
            },
        )
        self.assertEqual(status, 200)
        time.sleep(0.05)
        status, payload = self.request("POST", "/chat/job-cancel/cancel", {})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_chat_validates_job_id_and_messages(self) -> None:
        self.use_fake_lms()
        status, payload = self.request(
            "POST",
            "/chat",
            {
                "model": "m",
                "messages": [{"role": "user", "content": "x"}],
                "job": "../bad",
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("job", payload["error"])

        status, payload = self.request("POST", "/chat", {"model": "m"})
        self.assertEqual(status, 400)
        self.assertIn("messages", payload["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
