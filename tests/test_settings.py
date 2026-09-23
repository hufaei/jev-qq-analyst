"""Decision Infra settings persistence and a local gateway probe."""

import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import settings_config as config
import userconfig


class SettingsFiles(unittest.TestCase):
    def test_preserves_unrelated_lines_and_shell_safe_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env"
            original = '# note\nexport DECISION_INFRA_MODEL="old" # route\nCUSTOM=keep\n'
            path.write_text(original)
            model = 'route-$(echo SHOULD_NOT_RUN)'
            text = config.write_settings(path, original, {
                "DECISION_INFRA_BASE_URL": "http://127.0.0.1:8080",
                "DECISION_INFRA_MODEL": model,
            })
            self.assertIn("CUSTOM=keep\n", text)
            self.assertIn("# route", text)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(userconfig.parse_env_file(path)["DECISION_INFRA_MODEL"], model)

    def test_conflicting_edit_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env"
            path.write_text("new content")
            with self.assertRaises(ValueError):
                config.write_settings(path, "old content", {"DECISION_INFRA_MODEL": "jev-latest"})
            self.assertEqual(path.read_text(), "new content")

    def test_invalid_value_does_not_create_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env"
            with self.assertRaises(ValueError):
                config.write_settings(path, "", {"DECISION_INFRA_MODEL": "a\nb"})
            self.assertFalse(path.exists())

    def test_only_gateway_settings_can_be_written(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env"
            with self.assertRaises(ValueError):
                config.write_settings(path, "", {"OPENAI_API_KEY": "example"})
            self.assertFalse(path.exists())

    def test_validate_endpoint_rejects_credentials_and_query(self):
        self.assertEqual(config.validate_endpoint("http://127.0.0.1:8080/"), "http://127.0.0.1:8080")
        for address in ("https://user:pass@example.org", "https://example.org/?key=secret", "file:///tmp/gateway"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                config.validate_endpoint(address)


class Gateway(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append((self.path, dict(self.headers), body))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({
            "model": "jev-latest",
            "answers": {
                "intent": {"type": "choice", "choice": "闲聊", "probabilities": {"闲聊": 1.0}, "confidence": 1.0},
                "risk": {"type": "score", "score": 1.0, "probabilities": {"1": 1.0}, "confidence": 1.0, "legend": {"1": "基本没风险"}},
            },
        }).encode())


class SettingsNetwork(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join()

    def setUp(self):
        Gateway.requests = []

    def test_connection_uses_exact_route_without_provider_key(self):
        config.test_connection(self.base, "jev-latest")
        path, headers, body = Gateway.requests[-1]
        self.assertEqual(path, "/v1/systemone")
        self.assertEqual(body["model"], "jev-latest")
        self.assertNotIn("authorization", {key.lower() for key in headers})

    def test_wrong_route_is_rejected(self):
        with self.assertRaises(ValueError):
            config.test_connection(self.base, "other-route")


if __name__ == "__main__":
    unittest.main()
