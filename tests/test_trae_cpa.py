"""Exercise the actual entrypoint and gmcli renderer without any live account."""

import json
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import platform
import ssl
import subprocess
import tempfile
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
GMCLI = os.environ.get("GMCLI_BIN", str(ROOT / "target/release/gmcli"))
NODE_SECRET = "trae-test-node-secret-0001"
FAKE_KEY = "test-only-cpa-credential"


def render(extra):
    with tempfile.TemporaryDirectory(prefix="gm-trae-render-") as directory:
        output = Path(directory) / "envoy.json"
        env = {
            "PATH": os.environ["PATH"],
            "HOME": directory,
            "TMPDIR": directory,
            "GMCLI_BIN": GMCLI,
            "GM_START_RENDER_ONLY": "1",
            "GM_ENVOY_TEMPLATE_DIR": str(ROOT / "image/envoy"),
            "GM_RENDERED_CONFIG": str(output),
            "GM_NETWORK": "testnet",
            "GM_NODE_SECRET": NODE_SECRET,
            **extra,
        }
        result = subprocess.run(
            ["bash", str(ROOT / "image/start.sh")],
            env=env, capture_output=True, text=True, timeout=20,
        )
        text = output.read_text() if output.exists() else ""
        leftovers = [p.name for p in Path(directory).iterdir() if p.is_dir()]
        return result, text, leftovers


def ingress(config):
    return config["static_resources"]["listeners"][0]["filter_chains"][0]["filters"][0]["typed_config"]


class TraeRenderTests(unittest.TestCase):
    def enabled(self, **extra):
        return render({
            "TRAE_CPA_BASE_URL": "https://cpa.example.test/v1",
            "TRAE_CPA_API_KEY": FAKE_KEY,
            **extra,
        })

    def test_trae_only_renders_with_verified_tls_and_separate_identity(self):
        result, text, leftovers = self.enabled()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(FAKE_KEY, text + result.stdout + result.stderr)
        self.assertEqual(leftovers, [])
        config = json.loads(text)
        cluster = next(c for c in config["static_resources"]["clusters"] if c["name"] == "trae")
        tls = cluster["transport_socket"]["typed_config"]
        self.assertEqual(tls["sni"], "cpa.example.test")
        self.assertEqual(tls["common_tls_context"]["validation_context"]["match_typed_subject_alt_names"][0]["matcher"], {"exact": "cpa.example.test"})
        routes = [r for r in ingress(config)["route_config"]["virtual_hosts"][0]["routes"] if r.get("route", {}).get("cluster") == "trae"]
        self.assertEqual({r["match"]["path"] for r in routes}, {"/v1/models", "/v1/chat/completions"})
        for route in routes:
            self.assertIn({"name": "x-gm-provider", "string_match": {"exact": "trae"}}, route["match"]["headers"])
            self.assertNotIn("retry_policy", route["route"])
            self.assertNotIn("regex_rewrite", route["route"])
            self.assertIn("x-gm-node-key", route["request_headers_to_remove"])
            self.assertIn("x-gm-provider", route["request_headers_to_remove"])
        self.assertIn("TRAE_CPA_API_KEY", text)
        self.assertNotIn("request_body", text)

    def test_https_port_and_trailing_slash(self):
        result, text, _ = self.enabled(TRAE_CPA_BASE_URL="https://cpa.example.test:8443/v1/")
        self.assertEqual(result.returncode, 0, result.stderr)
        cluster = next(c for c in json.loads(text)["static_resources"]["clusters"] if c["name"] == "trae")
        address = cluster["load_assignment"]["endpoints"][0]["lb_endpoints"][0]["endpoint"]["address"]["socket_address"]
        self.assertEqual(address, {"address": "cpa.example.test", "port_value": 8443})

    def test_disabled_has_no_trae_route_and_matches_original(self):
        result, text, _ = render({"OPENAI_API_KEY": "test-openai"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('"name": "trae"', text)
        self.assertNotIn("TRAE_CPA_API_KEY", text)

    def test_rejects_incomplete_or_unauthenticated_configuration(self):
        for values in [
            {"TRAE_CPA_BASE_URL": "https://cpa.example.test/v1"},
            {"TRAE_CPA_API_KEY": FAKE_KEY},
            {"TRAE_CPA_BASE_URL": "https://cpa.example.test/v1", "TRAE_CPA_API_KEY": FAKE_KEY, "GM_NODE_SECRET": ""},
        ]:
            with self.subTest(values=list(values)):
                result, text, _ = render({"OPENAI_API_KEY": "test-openai", **values})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(text, "")
                self.assertNotIn(FAKE_KEY, result.stdout + result.stderr)

    def test_rejects_bad_urls_without_echoing_credentials(self):
        for url in [
            "http://cpa.example.test/v1", "https://user:secret@cpa.example.test/v1",
            "https://cpa.example.test/v1?key=secret", "https://cpa.example.test/v1#fragment",
            "https://cpa.example.test/admin", "https://cpa.example.test:0/v1",
            "https://cpa.example.test:65536/v1", "https://cpa.example.test:00080/v1",
            "https://cpa..example/v1", 'https://cpa.example.test/\"\ninjected',
            "https://[::1]/v1", "https://cpa.example.test:abc/v1",
        ]:
            with self.subTest(url=url):
                result, text, _ = self.enabled(TRAE_CPA_BASE_URL=url)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(text, "")
                self.assertNotIn(url, result.stderr)

    def test_rejects_multiple_keys_and_header_injection(self):
        for key in ["one;two", "one\ntwo", "one\rtwo", "one two", " "]:
            with self.subTest(key=repr(key)):
                result, text, _ = self.enabled(TRAE_CPA_API_KEY=key)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(text, "")
                if key.strip():
                    self.assertNotIn(key, result.stderr)

    def test_renderer_failure_cleans_temporary_templates(self):
        result, _, leftovers = self.enabled(GMCLI_BIN="/usr/bin/false")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(leftovers, [])


@unittest.skipUnless(os.environ.get("GM_TRAE_RUNTIME_TEST") == "1", "set GM_TRAE_RUNTIME_TEST=1 to run real Envoy/TLS tests")
class TraeRuntimeTests(unittest.TestCase):
    """Real pinned Envoy, a local TLS mock CPA, and only synthetic credentials."""

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix="gm-trae-runtime-")
        cls.directory = Path(cls.work.name)
        cls.container = None
        cls.server = None
        cls.requests = []
        cls.finish_stream = threading.Event()
        cls.addClassCleanup(cls.cleanup)
        cert = cls.directory / "cert.pem"
        key = cls.directory / "key.pem"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
            "-nodes", "-days", "1", "-subj", "/CN=gm-trae-test",
            "-addext", "subjectAltName=DNS:host.docker.internal,IP:127.0.0.1",
            "-keyout", str(key), "-out", str(cert),
        ], check=True, capture_output=True)

        class MockCPA(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_GET(self):
                self.respond({})

            def do_POST(self):
                self.respond(json.loads(self.rfile.read(int(self.headers["content-length"]))))

            def respond(self, body):
                cls.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                if self.headers.get("Authorization") != "Bearer " + FAKE_KEY:
                    self.send_error(401)
                    return
                if body.get("messages", [{}])[0].get("content") in ("return429", "return500"):
                    status = 429 if body["messages"][0]["content"] == "return429" else 500
                    payload = json.dumps({"error": {"type": "rate_limit" if status == 429 else "mock_error"}}).encode()
                    self.send_response(status)
                    if status == 429:
                        self.send_header("Retry-After", "60")
                elif body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(b'data: {"model":"deepseek-v4.1-flash","choices":[{"delta":{"content":"first"}}]}\n\n')
                    self.wfile.flush()
                    cls.finish_stream.wait(5)
                    self.wfile.write(b'data: [DONE]\n\n')
                    self.wfile.flush()
                    self.close_connection = True
                    return
                else:
                    payload = json.dumps(
                        {"object": "list", "data": [{"id": "deepseek-v4.1-flash"}]} if self.command == "GET"
                        else {"model": body["model"], "choices": [{"message": {"content": "mock-ok"}}],
                              "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}}
                    ).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("X-CPA-TRACE-ID", "synthetic-trace-only")
                self.end_headers()
                self.wfile.write(payload)

        cls.server = ThreadingHTTPServer(("0.0.0.0", 0), MockCPA)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        cls.server.socket = tls.wrap_socket(cls.server.socket, server_side=True)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        result, rendered, _ = render({
            "TRAE_CPA_BASE_URL": f"https://host.docker.internal:{cls.server.server_port}/v1",
            "TRAE_CPA_API_KEY": FAKE_KEY,
        })
        if result.returncode:
            raise AssertionError(result.stderr)
        (cls.directory / "envoy.json").write_text(rendered)
        cls.image = next(line.split()[1] for line in (ROOT / "image/Dockerfile").read_text().splitlines()
                         if line.startswith("FROM envoyproxy/envoy:"))
        command = ["docker", "run", "-d", "--rm", "-e", "ENVOY_UID=0", "-e", "TRAE_CPA_API_KEY",
                   "-v", f"{cls.directory}:/tmp/gm-ratls:ro",
                   "-v", f"{cert}:/etc/ssl/certs/ca-certificates.crt:ro",
                   "-p", "127.0.0.1::8080"]
        if platform.system() == "Linux":
            command += ["--add-host", "host.docker.internal:host-gateway"]
        command += [cls.image, "-c", "/tmp/gm-ratls/envoy.json", "--concurrency", "1", "--log-level", "error"]
        started = subprocess.run(command, env={**os.environ, "TRAE_CPA_API_KEY": FAKE_KEY},
                                 capture_output=True, text=True, check=True)
        cls.container = started.stdout.strip()
        cls.port = int(subprocess.check_output(["docker", "port", cls.container, "8080/tcp"], text=True).strip().rsplit(":", 1)[1])
        cls.client_context = ssl.create_default_context(cafile=str(cert))
        for _ in range(60):
            try:
                status, _, _ = cls.request("GET", "/v1/models", node_key="wrong-key")
                if status in (401, 403):
                    break
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.1)
        else:
            raise AssertionError(subprocess.check_output(["docker", "logs", cls.container], text=True, stderr=subprocess.STDOUT))

    @classmethod
    def cleanup(cls):
        cls.finish_stream.set()
        if cls.container:
            subprocess.run(["docker", "stop", "--time", "1", cls.container], capture_output=True, timeout=20)
        if cls.server:
            cls.server.shutdown()
            cls.server.server_close()
        cls.work.cleanup()

    @classmethod
    def connection(cls):
        return http.client.HTTPSConnection("127.0.0.1", cls.port, timeout=8, context=cls.client_context)

    @classmethod
    def request(cls, method, path, body=None, node_key=NODE_SECRET, extra=None):
        connection = cls.connection()
        try:
            connection.request(method, path, body=json.dumps(body) if body is not None else None,
                               headers={"Content-Type": "application/json", "x-gm-node-key": node_key,
                                        "x-gm-provider": "trae", **(extra or {})})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_chat_preserves_deepseek_model_usage_and_trace(self):
        body = {"model": "deepseek-v4.1-flash", "messages": [{"role": "user", "content": "test"}]}
        status, headers, payload = self.request("POST", "/v1/chat/completions", body,
                                             extra={"Authorization": "Bearer inbound-must-not-leak", "x-api-key": "inbound-other-key"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(json.loads(payload)["model"], body["model"])
        self.assertEqual(json.loads(payload)["usage"]["total_tokens"], 8)
        self.assertEqual({k.lower(): v for k, v in headers.items()}["x-cpa-trace-id"], "synthetic-trace-only")
        forwarded = self.requests[-1]
        self.assertEqual(forwarded["body"], body)
        self.assertEqual(forwarded["path"], "/v1/chat/completions")
        self.assertEqual(forwarded["headers"]["authorization"], "Bearer " + FAKE_KEY)
        self.assertFalse(any(k.lower().startswith("x-gm-") for k in forwarded["headers"]))
        self.assertNotIn("inbound-must-not-leak", json.dumps(forwarded))
        self.assertNotIn("inbound-other-key", json.dumps(forwarded))

    def test_models_uses_cpa_without_model_mapping(self):
        status, _, body = self.request("GET", "/v1/models")
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["data"][0]["id"], "deepseek-v4.1-flash")

    def test_upstream_identifies_application_instead_of_forwarding_library_ua(self):
        status, _, body = self.request("GET", "/v1/models", extra={"User-Agent": "Python-urllib/3.11"})
        self.assertEqual(status, 200, body)
        self.assertEqual({k.lower(): v for k, v in self.requests[-1]["headers"].items()}["user-agent"],
                         "gm-miner-trae/0.5.12")

    def test_bad_auth_and_unsupported_endpoints_do_not_reach_cpa(self):
        before = len(self.requests)
        self.assertIn(self.request("GET", "/v1/models", node_key="wrong-key")[0], (401, 403))
        self.assertEqual(self.request("GET", "/v0/management/auth-files")[0], 501)
        self.assertEqual(self.request("POST", "/v1/models", {})[0], 501)
        self.assertEqual(self.request("GET", "/v1/models", extra={"x-gm-upstream-slot": "unknown"})[0], 421)
        self.assertEqual(len(self.requests), before)

    def test_rate_limit_is_preserved_without_retry(self):
        before = len(self.requests)
        status, headers, body = self.request("POST", "/v1/chat/completions",
            {"model": "deepseek-v4.1-flash", "messages": [{"content": "return429"}]},
            extra={"x-envoy-retry-on": "retriable-status-codes", "x-envoy-max-retries": "3",
                   "x-envoy-retriable-status-codes": "429"})
        self.assertEqual(status, 429, body)
        self.assertEqual({k.lower(): v for k, v in headers.items()}["retry-after"], "60")
        self.assertEqual(json.loads(body)["error"]["type"], "rate_limit")
        self.assertEqual(len(self.requests), before + 1)

    def test_sse_first_chunk_arrives_before_upstream_finishes(self):
        self.finish_stream.clear()
        connection = self.connection()
        try:
            connection.request("POST", "/v1/chat/completions", json.dumps({
                "model": "deepseek-v4.1-flash", "stream": True, "messages": [{"content": "stream"}],
            }), headers={"Content-Type": "application/json", "x-gm-node-key": NODE_SECRET, "x-gm-provider": "trae"})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn(b'"content":"first"', response.readline())
            self.assertFalse(self.finish_stream.is_set())
            self.finish_stream.set()
            self.assertIn(b"data: [DONE]", response.read())
        finally:
            self.finish_stream.set()
            connection.close()

    def test_server_error_cannot_be_retried_by_inbound_headers(self):
        before = len(self.requests)
        status, _, body = self.request("POST", "/v1/chat/completions",
            {"model": "deepseek-v4.1-flash", "messages": [{"content": "return500"}]},
            extra={"x-envoy-retry-on": "5xx", "x-envoy-max-retries": "3"})
        self.assertEqual(status, 500, body)
        self.assertEqual(len(self.requests), before + 1)


if __name__ == "__main__":
    unittest.main()
