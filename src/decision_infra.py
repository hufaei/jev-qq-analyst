"""Client for hufaei/decision-infra's canonical ``/v1/systemone`` boundary.

The desktop app never owns a TypeSafe key.  It talks only to the Decision Infra
gateway (loopback by default); the gateway owns provider credentials and exact
model routing.  One request is made for one decision: no retry and no fallback.
"""

from __future__ import annotations

from copy import deepcopy
import http.client
import io
import json
from pathlib import Path
import urllib.error
import sys
import urllib.parse

import userconfig


# PyInstaller onefile unpacks bundled data under sys._MEIPASS; the repo layout
# applies everywhere else (dev runs and macOS).
_SPEC_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
_SPEC = json.loads((_SPEC_ROOT / "shared" /
                    "decision_questions.json").read_text(encoding="utf-8"))
QUESTIONS = _SPEC["questions"]
CHAT_INTENTS = QUESTIONS["intent"]["criteria"]
RISK_LEVELS = QUESTIONS["risk"]["criteria"]
BEHAVIORS = QUESTIONS["behavior"]["criteria"]
EMOTIONS = QUESTIONS["emotion"]["criteria"]
NEEDS = QUESTIONS["need"]["criteria"]
ACTION_FIT_LEVELS = QUESTIONS["action_pause"]["criteria"]
ACTION_STRATEGIES = _SPEC["actions"]
SIGNAL_LABELS = _SPEC["signal_labels"]

DEFAULT_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_MODEL = "jev-latest"
DEFAULT_TIMEOUT = 15


def systemone_url(base_url: str) -> str:
    """Turn a gateway origin/base into its canonical decision endpoint."""
    base = (base_url or DEFAULT_BASE_URL).strip().rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Decision Infra 地址必须是有效的 http(s) 地址。")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Decision Infra 地址不能包含账号、查询参数或片段。")
    if parsed.path.endswith("/v1/systemone"):
        return base
    if parsed.path.endswith("/v1"):
        return base + "/systemone"
    return base + "/v1/systemone"


def health_url(base_url: str) -> str:
    endpoint = urllib.parse.urlsplit(systemone_url(base_url))
    path = endpoint.path.removesuffix("/v1/systemone") + "/healthz"
    return urllib.parse.urlunsplit(
        (endpoint.scheme, endpoint.netloc, path or "/healthz", "", ""))


def _connection(url: str, timeout: float):
    parsed = urllib.parse.urlsplit(url)
    cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    conn = cls(parsed.hostname, parsed.port, timeout=timeout)
    path = parsed.path + (("?" + parsed.query) if parsed.query else "")
    return conn, path


def _json_request(method: str, url: str, body: dict | None, timeout: float,
                  headers: dict | None = None) -> dict:
    """Make exactly one request; Decision Infra explicitly forbids silent retries."""
    conn, path = _connection(url, timeout)
    raw = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request_headers = {"accept": "application/json"}
    if raw is not None:
        request_headers["content-type"] = "application/json"
    if headers:
        request_headers.update(headers)
    try:
        conn.request(method, path, body=raw, headers=request_headers)
        response = conn.getresponse()
        payload = response.read()
        if response.status >= 300:
            raise urllib.error.HTTPError(
                url, response.status, response.reason, response.headers, io.BytesIO(payload))
        try:
            data = json.loads(payload)
        except (TypeError, ValueError) as exc:
            raise ValueError("Decision Infra 返回的不是有效 JSON。") from exc
        if not isinstance(data, dict):
            raise ValueError("Decision Infra 返回格式不正确。")
        return data
    finally:
        conn.close()


def gateway_health(base_url: str = DEFAULT_BASE_URL, timeout: float = 3) -> bool:
    return _json_request("GET", health_url(base_url), None, timeout).get("status") == "ok"


class DecisionInfraJudge:
    """Jev-shaped judgment backed exclusively by the Decision Infra gateway."""

    name = "decision-infra"
    load_status = None

    def __init__(self, base_url: str | None = None, model: str | None = None,
                 timeout: float = DEFAULT_TIMEOUT):
        self.base_url = base_url or userconfig.get("DECISION_INFRA_BASE_URL") or DEFAULT_BASE_URL
        # The product is deliberately Jev-only.  Selecting the exact route prevents the
        # gateway default from changing this app to Laya when no TypeSafe key is present.
        self.model = model or userconfig.get("DECISION_INFRA_MODEL") or DEFAULT_MODEL
        self.timeout = timeout
        self.endpoint = systemone_url(self.base_url)

    def judge(self, message: str, context: str | None = None,
              quoted_text: str = "") -> dict:
        state: dict[str, str] = {"message": message}
        if context:
            state["context"] = context
        if quoted_text:
            state["quote"] = quoted_text
        payload = {
            "model": self.model,
            "state": state,
            "questions": deepcopy(QUESTIONS),
        }
        data = self._post(payload)
        return self._verdict(data, message)

    def _post(self, payload: dict) -> dict:
        return _json_request("POST", self.endpoint, payload, self.timeout)

    def _verdict(self, data: dict, message: str) -> dict:
        actual_model = data.get("model")
        answers = data.get("answers")
        if not isinstance(actual_model, str) or not actual_model or not isinstance(answers, dict):
            raise ValueError("Decision Infra 响应缺少 model 或 answers。")

        intent_answer = answers.get("intent")
        risk_answer = answers.get("risk")
        if not isinstance(intent_answer, dict) or intent_answer.get("type") != "choice":
            raise ValueError("Decision Infra 未返回有效的 intent choice。")
        if not isinstance(risk_answer, dict) or risk_answer.get("type") != "score":
            raise ValueError("Decision Infra 未返回有效的 risk score。")

        intent = intent_answer.get("choice")
        if intent not in CHAT_INTENTS:
            raise ValueError("Decision Infra 返回了未知意图。")
        confidence = intent_answer.get("confidence")
        risk = risk_answer.get("score")
        if not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
            raise ValueError("Decision Infra 返回了无效置信度。")
        if not isinstance(risk, (int, float)) or not 0 <= float(risk) <= len(RISK_LEVELS) - 1:
            raise ValueError("Decision Infra 返回了无效风险分数。")

        def selected(name: str, options: dict[str, str]) -> tuple[str, float]:
            answer = answers.get(name) or {}
            choice = answer.get("choice")
            confidence = answer.get("confidence", 0)
            if (answer.get("type") != "choice" or choice not in options
                    or not isinstance(confidence, (int, float))
                    or not 0 <= float(confidence) <= 1):
                return "", 0.0
            return choice, float(confidence)

        behavior, behavior_confidence = selected("behavior", BEHAVIORS)
        emotion, emotion_confidence = selected("emotion", EMOTIONS)
        need, need_confidence = selected("need", NEEDS)
        signals = {}
        for name in SIGNAL_LABELS:
            answer = answers.get(name) or {}
            value = answer.get("noul")
            if answer.get("type") == "noul" and isinstance(value, (int, float)):
                signals[name] = max(0.0, min(1.0, float(value)))

        reply_answer = answers.get("reply_needed") or {}
        reply_probability = reply_answer.get("noul")
        if (reply_answer.get("type") != "noul"
                or not isinstance(reply_probability, (int, float))
                or not 0 <= float(reply_probability) <= 1):
            reply_probability = None
        else:
            reply_probability = float(reply_probability)

        ranked_actions = []
        for action_id, label in ACTION_STRATEGIES.items():
            answer = answers.get(f"action_{action_id}") or {}
            score = answer.get("score")
            if (answer.get("type") == "score" and isinstance(score, (int, float))
                    and 0 <= float(score) <= len(ACTION_FIT_LEVELS) - 1):
                ranked_actions.append({"id": action_id, "label": label,
                                       "score": round(float(score), 1)})
        ranked_actions.sort(key=lambda item: item["score"], reverse=True)
        if reply_probability is not None and reply_probability < .40:
            ranked_actions = [item for item in ranked_actions if item["id"] == "pause"]
        elif reply_probability is not None and reply_probability > .60:
            ranked_actions = [item for item in ranked_actions if item["id"] != "pause"]
        ranked_actions = ranked_actions[:3]

        raw_intent_probs = intent_answer.get("probabilities") or {}
        intent_ranking = sorted(
            ({"label": label, "probability": float(probability)}
             for label, probability in raw_intent_probs.items()
             if label in CHAT_INTENTS and isinstance(probability, (int, float))
             and 0 <= float(probability) <= 1),
            key=lambda item: item["probability"], reverse=True)[:3]
        if not intent_ranking:
            intent_ranking = [{"label": intent, "probability": float(confidence)}]

        return {
            "intent": intent,
            "confidence": float(confidence),
            "intent_probs": intent_answer.get("probabilities") or {},
            "intent_ranking": intent_ranking,
            "risk": round(float(risk), 1),
            "risk_probs": risk_answer.get("probabilities") or {},
            "behavior": behavior,
            "behavior_confidence": behavior_confidence,
            "emotion": emotion,
            "emotion_confidence": emotion_confidence,
            "need": need,
            "need_confidence": need_confidence,
            "signals": signals,
            "reply_probability": reply_probability,
            "action_rankings": ranked_actions,
            "signal_labels": [SIGNAL_LABELS[name] for name, probability in signals.items()
                              if probability >= 0.72],
            "actions": [item["label"] for item in ranked_actions],
            "message": message,
            "backend": f"infra/{actual_model}",
        }

    def warm(self) -> None:
        """The gateway/provider lifecycle belongs to Decision Infra, not the app."""
        return None


def test_decision(base_url: str, model: str = DEFAULT_MODEL,
                  timeout: float = DEFAULT_TIMEOUT) -> str:
    """Send a synthetic decision and return the gateway's actual route ID."""
    judge = DecisionInfraJudge(base_url=base_url, model=model or DEFAULT_MODEL, timeout=timeout)
    result = judge.judge("连接测试：请判断这是一条普通问候。")
    return result["backend"].removeprefix("infra/")


if __name__ == "__main__":
    import sys

    client = DecisionInfraJudge()
    text = sys.argv[1] if len(sys.argv) > 1 else "这个事情今天能搞定吗？"
    print(json.dumps(client.judge(text), ensure_ascii=False, indent=2))
