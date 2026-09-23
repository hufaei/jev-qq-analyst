import json
import sys
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import decision_infra


class Server(BaseHTTPRequestHandler):
    requests = []
    code = 200

    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append((self.path, dict(self.headers), body))
        self.send_response(self.code)
        self.end_headers()
        if self.code >= 300:
            self.wfile.write(b'{"error":"provider_unavailable","model":"jev-latest"}')
            return
        self.wfile.write(json.dumps({
            "model": "jev-latest",
            "answers": {
                "intent": {
                    "type": "choice",
                    "choice": "催进度",
                    "probabilities": {"催进度": 0.87, "问进度": 0.13},
                    "confidence": 0.87,
                },
                "risk": {
                    "type": "score",
                    "score": 4.2,
                    "probabilities": {"4": 0.8, "5": 0.2},
                    "confidence": 0.8,
                    "legend": {"4": "有点敏感", "5": "需要谨慎"},
                },
            },
        }, ensure_ascii=False).encode())


class DecisionInfraTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join()

    def setUp(self):
        Server.requests = []
        Server.code = 200

    def test_url_composition(self):
        self.assertEqual(decision_infra.systemone_url(self.base), self.base + "/v1/systemone")
        self.assertEqual(decision_infra.systemone_url(self.base + "/v1"), self.base + "/v1/systemone")
        self.assertEqual(decision_infra.systemone_url(self.base + "/v1/systemone"), self.base + "/v1/systemone")
        self.assertEqual(decision_infra.health_url(self.base + "/v1/systemone"), self.base + "/healthz")

    def test_health(self):
        self.assertTrue(decision_infra.gateway_health(self.base))

    def test_exact_route_structured_state_and_no_app_key(self):
        result = decision_infra.DecisionInfraJudge(self.base, "jev-latest").judge(
            "这个事情今天能搞定吗？", "对方：上周安排的任务", "上周说今天完成")
        path, headers, body = Server.requests[0]
        self.assertEqual(path, "/v1/systemone")
        self.assertNotIn("authorization", {k.lower(): v for k, v in headers.items()})
        self.assertEqual(body["model"], "jev-latest")
        self.assertEqual(body["state"], {
            "message": "这个事情今天能搞定吗？",
            "context": "对方：上周安排的任务",
            "quote": "上周说今天完成",
        })
        expected_types = {
            "intent": "choice", "risk": "score", "behavior": "choice",
            "emotion": "choice", "need": "choice", "reply_needed": "noul",
            "indirect_request": "noul", "pressure": "noul", "boundary": "noul",
            **{f"action_{name}": "score" for name in (
                "pause", "empathize", "answer", "clarify", "update", "schedule",
                "help", "repair", "boundary", "decline", "arrange", "appreciate",
            )},
        }
        self.assertEqual(len(body["questions"]), 21)
        self.assertEqual({name: question["type"] for name, question in
                          body["questions"].items()}, expected_types)
        self.assertEqual(result["backend"], "infra/jev-latest")
        self.assertEqual(result["intent"], "催进度")
        self.assertEqual(result["risk"], 4.2)

    def test_provider_error_is_not_retried_or_fallbacked(self):
        Server.code = 503
        with self.assertRaises(urllib.error.HTTPError) as caught:
            decision_infra.DecisionInfraJudge(self.base, "jev-latest").judge("测试")
        self.assertEqual(caught.exception.code, 503)
        self.assertEqual(len(Server.requests), 1)


if __name__ == "__main__":
    unittest.main()
