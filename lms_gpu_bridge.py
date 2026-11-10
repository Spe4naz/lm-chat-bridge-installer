#!/usr/bin/env python3
"""LAN bridge for controlling LM Studio GPU offload through the `lms` CLI.

LM Studio's REST API (0.4.x) has no GPU offload parameter, but the CLI does:

    lms load 'google/gemma-3n-e4b' --gpu max --context-length 8192

This service exposes that command over HTTP so the Android client (or curl)
can load a model with all layers on the GPU.

It also proxies chat completions as resumable jobs: the generation runs on the
server, the client polls for new tokens and can reconnect after being
backgrounded, losing the network or being killed.

Only the Python standard library is required.

Environment variables:
    LMS_BIN              path to the lms executable (default: "lms")
    LMS_HOST             remote LM Studio instance (optional, adds --host)
    LMS_API_KEY          API token of LM Studio, if auth is enabled
    BRIDGE_TOKEN         bearer token clients must send. Required unless
                         BRIDGE_HOST is a loopback address
    BRIDGE_HOST          bind address (default: 0.0.0.0)
    BRIDGE_PORT          port (default: 1235)
    BRIDGE_LOAD_TIMEOUT  seconds to wait for a load (default: 900)
    BRIDGE_CHAT_TIMEOUT  seconds to wait for chat tokens (default: 1800)
    BRIDGE_JOB_TTL       seconds to keep a finished chat job (default: 3600)
    BRIDGE_MAX_JOBS      maximum chat jobs kept in memory (default: 8)
    BRIDGE_MAX_BODY      maximum request body size in bytes (default: 4 MiB)

Endpoints:
    GET  /health
    GET  /ps
    POST /load      {"model": "...", "gpu": "max"|"off"|0.75,
                     "context_length": 8192, "ttl": 3600, "identifier": "phone"}
    POST /unload    {"model": "..."} or {"all": true}
    POST /estimate  same body as /load, prints memory estimate only

    POST /chat      same body as /v1/chat/completions plus optional "job"
                    -> {"ok": true, "job": "<id>"} immediately
    GET  /chat/<id>?since=<n>&wait=<seconds>
                    -> {"ok": true, "events": [...], "next": n,
                        "done": bool, "error": null,
                        "usage": {...}|null, "elapsed_ms": n}
                    `usage` is the exact OpenAI token usage when the client
                    sends stream_options.include_usage.
    POST /chat/<id>/cancel

Run:
    BRIDGE_TOKEN=change-me python3 lms_gpu_bridge.py
"""

from __future__ import annotations

import hmac
import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

LMS_BIN = os.environ.get("LMS_BIN", "lms")
LMS_HOST = os.environ.get("LMS_HOST", "").strip()
LMS_API_KEY = os.environ.get("LMS_API_KEY", "").strip()
TOKEN = os.environ.get("BRIDGE_TOKEN", "").strip()
HOST = os.environ.get("BRIDGE_HOST", "0.0.0.0")
PORT = int(os.environ.get("BRIDGE_PORT", "1235"))
LOAD_TIMEOUT = int(os.environ.get("BRIDGE_LOAD_TIMEOUT", "900"))
CHAT_TIMEOUT = int(os.environ.get("BRIDGE_CHAT_TIMEOUT", "1800"))
JOB_TTL = int(os.environ.get("BRIDGE_JOB_TTL", "3600"))
MAX_JOBS = int(os.environ.get("BRIDGE_MAX_JOBS", "8"))
MAX_BODY = int(os.environ.get("BRIDGE_MAX_BODY", str(4 * 1024 * 1024)))

AUTH_SCHEME = "Bearer "

# Bumped when the HTTP shape changes; clients can use /health to discover it.
API_VERSION = 2
FEATURES = ["chat", "estimate"]
VERSION_TTL = 60.0
_version_cache: tuple[float, str] | None = None


class LmsError(RuntimeError):
    """An `lms` invocation failed."""


def with_host(args: list[str]) -> list[str]:
    """Adds `--host` when the bridge talks to a remote LM Studio instance."""
    if not LMS_HOST:
        return args
    return [*args, "--host", LMS_HOST]


def run_lms(args: list[str], timeout: int = 60) -> str:
    try:
        proc = subprocess.run(
            [LMS_BIN, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise LmsError(
            f"lms не найден: {LMS_BIN}. Укажите путь в LMS_BIN."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise LmsError(f"lms не ответил за {timeout} с") from exc

    output = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        raise LmsError(output or f"lms завершился с кодом {proc.returncode}")
    return output


def lms_version() -> str:
    """Returns the CLI version, cached briefly to keep /health cheap."""
    global _version_cache
    now = time.time()
    if _version_cache is not None and now - _version_cache[0] < VERSION_TTL:
        return _version_cache[1]
    version = run_lms(["--version"], timeout=15)
    _version_cache = (now, version)
    return version


def _gpu_arg(value: Any) -> str:
    if isinstance(value, bool):
        raise ValueError("gpu должен быть числом, 'max' или 'off'")
    if isinstance(value, (int, float)):
        ratio = max(0.0, min(1.0, float(value)))
        return f"{ratio:g}"

    text = str(value).strip().lower()
    if text in {"max", "off"}:
        return text
    try:
        ratio = max(0.0, min(1.0, float(text)))
    except ValueError as exc:
        raise ValueError("gpu должен быть числом 0..1, 'max' или 'off'") from exc
    return f"{ratio:g}"


def _positive_int(value: Any, name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} должен быть целым числом") from exc
    if number <= 0:
        raise ValueError(f"{name} должен быть больше нуля")
    return number


def load_args(payload: dict[str, Any], *, estimate: bool = False) -> list[str]:
    model = str(payload.get("model", "")).strip()
    if not model:
        raise ValueError("model обязателен")

    args = ["load"]
    if estimate:
        args.append("--estimate-only")
    args.append(model)

    if payload.get("gpu") is not None:
        args.extend(["--gpu", _gpu_arg(payload["gpu"])])

    context = payload.get("context_length")
    if context not in (None, ""):
        args.extend(["--context-length", str(_positive_int(context, "context_length"))])

    ttl = payload.get("ttl")
    if ttl not in (None, "", 0):
        args.extend(["--ttl", str(_positive_int(ttl, "ttl"))])

    identifier = str(payload.get("identifier", "")).strip()
    if identifier:
        args.extend(["--identifier", identifier])

    return args


def unload_args(payload: dict[str, Any]) -> list[str]:
    if payload.get("all") is True:
        return ["unload", "--all"]
    model = str(payload.get("model", "")).strip()
    if not model:
        raise ValueError("нужен model или all=true")
    return ["unload", model]


# --------------------------------------------------------------------- chat jobs


class ChatJob:
    """A chat completion that keeps running on the server."""

    def __init__(self, job_id: str, payload: dict[str, Any]):
        self.id = job_id
        self.payload = payload
        self.events: list[dict[str, Any]] = []
        self.done = False
        self.error: str | None = None
        self.cancelled = False
        self.created = time.time()
        self.finished: float | None = None
        self.usage: dict[str, Any] | None = None
        self.cond = threading.Condition()
        self.conn: http.client.HTTPConnection | None = None

    def add(self, content: str, reasoning: str) -> None:
        if not content and not reasoning:
            return
        with self.cond:
            self.events.append(
                {
                    "seq": len(self.events) + 1,
                    "content": content,
                    "reasoning": reasoning,
                }
            )
            self.cond.notify_all()

    def finish(self, error: str | None = None) -> None:
        with self.cond:
            self.done = True
            self.error = error
            self.finished = time.time()
            self.cond.notify_all()

    def wait_for(self, since: int, seconds: float) -> None:
        """Blocks until there are events after `since` or the job is done."""
        deadline = time.time() + max(0.0, seconds)
        with self.cond:
            while len(self.events) <= since and not self.done and not self.cancelled:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self.cond.wait(timeout=min(remaining, 5.0))


_jobs: dict[str, ChatJob] = {}
_jobs_lock = threading.Lock()


def _cleanup_jobs() -> None:
    now = time.time()
    with _jobs_lock:
        for job_id, job in list(_jobs.items()):
            if job.finished is not None and now - job.finished > JOB_TTL:
                _jobs.pop(job_id, None)
        if len(_jobs) < MAX_JOBS:
            return
        finished = sorted(
            (job for job in _jobs.values() if job.done),
            key=lambda job: job.finished or 0,
        )
        while len(_jobs) >= MAX_JOBS and finished:
            job = finished.pop(0)
            _jobs.pop(job.id, None)


def _job(job_id: str) -> ChatJob | None:
    with _jobs_lock:
        return _jobs.get(job_id)


def _split_host(value: str) -> tuple[str, int, str]:
    """`192.168.1.5:1234` or `https://host:1234` -> (host, port, scheme)."""
    text = value.strip() or "127.0.0.1:1234"
    if "://" in text:
        parsed = urlparse(text)
        port = parsed.port or (443 if parsed.scheme == "https" else 1234)
        return parsed.hostname or "127.0.0.1", port, parsed.scheme
    host, _, port = text.partition(":")
    try:
        number = int(port) if port else 1234
    except ValueError:
        number = 1234
    return host or "127.0.0.1", number, "http"


def _upstream_error(status: int, body: str) -> str:
    detail = body.strip()
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            error = parsed.get("error")
            if isinstance(error, dict) and error.get("message"):
                detail = str(error["message"])
            elif isinstance(error, str):
                detail = error
            elif parsed.get("message"):
                detail = str(parsed["message"])
    except json.JSONDecodeError:
        pass
    if len(detail) > 300:
        detail = detail[:300] + "…"
    return f"LM Studio ответил {status}{': ' + detail if detail else ''}"


def _run_chat(job: ChatJob) -> None:
    host, port, scheme = _split_host(LMS_HOST)
    connection_class = (
        http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    )
    conn = connection_class(host, port, timeout=30)
    try:
        body = json.dumps(
            {key: value for key, value in job.payload.items() if key != "job"},
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        if LMS_API_KEY:
            headers["Authorization"] = f"Bearer {LMS_API_KEY}"

        conn.request("POST", "/v1/chat/completions", body=body, headers=headers)
        job.conn = conn
        response = conn.getresponse()
        if response.status != 200:
            text = response.read().decode("utf-8", "replace")
            raise LmsError(_upstream_error(response.status, text))

        if conn.sock is not None:
            conn.sock.settimeout(CHAT_TIMEOUT)

        while not job.cancelled:
            raw = response.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                if data == "[DONE]":
                    break
                continue
            try:
                parsed = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue

            error = parsed.get("error")
            if error:
                if isinstance(error, dict):
                    message = error.get("message") or error.get("code") or "ошибка"
                else:
                    message = error
                raise LmsError(str(message))

            usage = parsed.get("usage")
            if isinstance(usage, dict):
                with job.cond:
                    job.usage = usage
                    job.cond.notify_all()

            choices = parsed.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            first = choices[0]
            if not isinstance(first, dict):
                continue
            delta = first.get("delta")
            if not isinstance(delta, dict):
                continue
            job.add(
                str(delta.get("content") or ""),
                str(
                    delta.get("reasoning_content")
                    or delta.get("reasoning")
                    or ""
                ),
            )
        job.finish()
    except Exception as exc:  # noqa: BLE001 - reported to the client as text
        if job.cancelled:
            job.finish()
        else:
            job.finish(str(exc) or exc.__class__.__name__)
    finally:
        job.conn = None
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _cancel_job(job: ChatJob) -> None:
    with job.cond:
        job.cancelled = True
        job.cond.notify_all()
    conn = job.conn
    if conn is not None and conn.sock is not None:
        try:
            conn.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def _valid_job_id(value: str) -> bool:
    return 1 <= len(value) <= 64 and all(
        char.isalnum() or char in "-_" for char in value
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "LmsGpuBridge/1.4"
    protocol_version = "HTTP/1.1"

    def _authorized(self) -> bool:
        if not TOKEN:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith(AUTH_SCHEME):
            return False
        return hmac.compare_digest(header[len(AUTH_SCHEME):], TOKEN)

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("некорректный Content-Length") from exc
        if length > MAX_BODY:
            raise ValueError(f"тело запроса больше {MAX_BODY} байт")
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("тело запроса должно быть JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("JSON должен быть объектом")
        return parsed

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed_url = urlparse(self.path)
        path = parsed_url.path

        if path == "/health":
            try:
                version = lms_version()
                self._send(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "lms": version,
                        "host": LMS_HOST or None,
                        "api": API_VERSION,
                        "features": FEATURES,
                    },
                )
            except LmsError as exc:
                self._send(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"ok": False, "error": str(exc)},
                )
            return

        if path == "/ps":
            if not self._authorized():
                self._send(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            try:
                output = run_lms(with_host(["ps", "--json"]), timeout=30)
                try:
                    result: Any = json.loads(output)
                except json.JSONDecodeError:
                    result = output
                self._send(HTTPStatus.OK, {"ok": True, "result": result})
            except LmsError as exc:
                self._send(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"ok": False, "error": str(exc)},
                )
            return

        if path.startswith("/chat/"):
            if not self._authorized():
                self._send(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            job_id = path[len("/chat/"):].strip("/")
            job = _job(job_id)
            if job is None:
                self._send(
                    HTTPStatus.NOT_FOUND,
                    {"ok": False, "error": "job not found"},
                )
                return
            query = parse_qs(parsed_url.query)
            try:
                since = max(0, int(query.get("since", ["0"])[0] or 0))
                wait = max(0, min(30, int(query.get("wait", ["0"])[0] or 0)))
            except ValueError:
                self._send(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "since/wait должны быть числами"},
                )
                return
            job.wait_for(since, wait)
            with job.cond:
                total = len(job.events)
                since = min(since, total)
                events = list(job.events[since:])
                done = job.done or job.cancelled
                error = job.error
                usage = job.usage
                elapsed_ms = int(
                    ((job.finished or time.time()) - job.created) * 1000
                )
            self._send(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "events": events,
                    "next": total,
                    "done": done,
                    "error": error,
                    "usage": usage,
                    "elapsed_ms": elapsed_ms,
                },
            )
            return

        self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        if not self._authorized():
            self._send(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return

        path = urlparse(self.path).path

        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return

        try:
            if path == "/load":
                output = run_lms(
                    with_host(load_args(payload)),
                    timeout=LOAD_TIMEOUT,
                )
                self._send(HTTPStatus.OK, {"ok": True, "output": output})
            elif path == "/unload":
                output = run_lms(with_host(unload_args(payload)), timeout=120)
                self._send(HTTPStatus.OK, {"ok": True, "output": output})
            elif path == "/estimate":
                output = run_lms(
                    with_host(load_args(payload, estimate=True)),
                    timeout=LOAD_TIMEOUT,
                )
                self._send(HTTPStatus.OK, {"ok": True, "output": output})
            elif path == "/chat":
                self._start_chat(payload)
            elif path.startswith("/chat/") and path.endswith("/cancel"):
                job_id = path[len("/chat/"):-len("/cancel")].strip("/")
                job = _job(job_id)
                if job is None:
                    self._send(
                        HTTPStatus.NOT_FOUND,
                        {"ok": False, "error": "job not found"},
                    )
                    return
                _cancel_job(job)
                self._send(HTTPStatus.OK, {"ok": True})
            else:
                self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except ValueError as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        except LmsError as exc:
            self._send(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": str(exc)},
            )

    def _start_chat(self, payload: dict[str, Any]) -> None:
        model = str(payload.get("model", "")).strip()
        messages = payload.get("messages")
        if not model:
            raise ValueError("model обязателен")
        if not isinstance(messages, list) or not messages:
            raise ValueError("messages обязателен")

        job_id = str(payload.get("job", "")).strip() or uuid.uuid4().hex
        if not _valid_job_id(job_id):
            raise ValueError("job должен содержать только буквы, цифры, - и _")

        existing = _job(job_id)
        if existing is not None:
            self._send(HTTPStatus.OK, {"ok": True, "job": job_id, "done": existing.done})
            return

        _cleanup_jobs()
        with _jobs_lock:
            if len(_jobs) >= MAX_JOBS:
                self._send(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    {"ok": False, "error": "слишком много заданий, попробуйте позже"},
                )
                return
            job = ChatJob(job_id, payload)
            _jobs[job_id] = job

        thread = threading.Thread(target=_run_chat, args=(job,), daemon=True)
        thread.start()
        self._send(HTTPStatus.OK, {"ok": True, "job": job_id})

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)


LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def startup_error() -> str | None:
    """Rejects an unauthenticated bridge listening on the local network."""
    if TOKEN or HOST.strip().lower() in LOOPBACK_HOSTS:
        return None
    return (
        "BRIDGE_TOKEN не задан. Мостик не запущен: без токена любой в сети "
        "мог бы загружать и выгружать модели. Задайте BRIDGE_TOKEN или "
        "привяжите мостик к 127.0.0.1 через BRIDGE_HOST."
    )


def main() -> int:
    error = startup_error()
    if error is not None:
        print(error, file=sys.stderr)
        return 2
    if not TOKEN:
        print(
            "ВНИМАНИЕ: BRIDGE_TOKEN не задан, мостик слушает только "
            "loopback-адрес. Для доступа с телефона задайте токен.",
            file=sys.stderr,
        )
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"LM Studio GPU bridge: http://{HOST}:{PORT}", flush=True)
    if LMS_HOST:
        print(f"LM Studio host: {LMS_HOST}", flush=True)
    print("Проверка: curl http://127.0.0.1:%d/health" % PORT, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
