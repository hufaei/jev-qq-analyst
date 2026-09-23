"""Client for hufaei/decision-infra's canonical ``/v1/systemone`` boundary.

The desktop app never owns a TypeSafe key.  It talks only to the Decision Infra
gateway (loopback by default); the gateway owns provider credentials and exact
model routing.  One request is made for one decision: no retry and no fallback.
"""

from __future__ import annotations

import http.client
import io
import json
import urllib.error
import urllib.parse

import userconfig
from judge import INTENTS, RISK_LEVELS


DEFAULT_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_MODEL = "jev-latest"
DEFAULT_TIMEOUT = 15

BEHAVIORS = {
    "告知": "提供事实、状态或消息，没有直接要求",
    "提问": "直接向我索取信息或解释",
    "请求": "希望我做一件事或提供帮助",
    "催促": "对已在进行的事要求更快推进或立即反馈",
    "表态": "表达赞同、反对、拒绝或立场",
    "社交": "问候、寒暄、玩笑或维系交流",
}

EMOTIONS = {
    "平静": "措辞中没有明显情绪起伏",
    "开心": "可从文字中看出高兴、轻松或感激",
    "着急": "可从文字中看出紧迫、急切或焦躁",
    "担忧": "可从文字中看出不安、疑虑或顾虑",
    "不满": "可从文字中看出失望、批评或愤怒",
    "难判断": "仅凭这些文字不足以推断情绪",
}

NEEDS = {
    "信息": "想知道事实、原因或背景",
    "行动": "期待我执行或处理具体事情",
    "时间承诺": "期待明确进度、期限或下一次反馈时间",
    "确认": "想得到同意、答复或确定性",
    "情绪回应": "期待被理解、安慰或认可",
    "暂无明确需求": "没有可辨认的回应要求",
}

SIGNAL_LABELS = {
    "indirect_request": "可能有隐含请求",
    "pressure": "有催促感",
    "boundary": "对方在设边界",
}

CHAT_INTENTS = {
    **INTENTS,
    "求确认": "希望我明确同意、否定、选择或给出确定答复",
    "倾诉": "主要在分享烦恼或脆弱感受，希望被听见而非解决任务",
    "设边界": "明确拒绝、停止某话题，或限定可接受的行为",
    "邀约": "邀请我进行非工作会议性质的活动或见面",
}

ACTION_STRATEGIES = {
    "pause": "暂不回复，留出空间",
    "empathize": "先回应对方的感受",
    "answer": "直接回答对方的问题",
    "clarify": "先澄清对方具体指什么",
    "update": "说明事实和当前进展",
    "schedule": "给出可信的时间安排",
    "help": "提出具体可做的帮助",
    "repair": "道歉并说明补救办法",
    "boundary": "尊重边界，停止追问",
    "decline": "礼貌而明确地拒绝",
    "arrange": "确认邀约或安排细节",
    "appreciate": "表达感谢或认可",
}

ACTION_FIT_LEVELS = [
    "0：不适合，可能让交流变差",
    "1：此刻不太合适",
    "2：可以考虑，但不是优先选择",
    "3：适合当前消息",
    "4：当前最值得优先采用",
]


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


def _json_request(method: str, url: str, body: dict | None, timeout: float) -> dict:
    """Make exactly one request; Decision Infra explicitly forbids silent retries."""
    conn, path = _connection(url, timeout)
    raw = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"accept": "application/json"}
    if raw is not None:
        headers["content-type"] = "application/json"
    try:
        conn.request(method, path, body=raw, headers=headers)
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
            "questions": {
                "intent": {
                    "type": "choice",
                    "instructions": "只判断 message 中新写的话的主要意图；quote 如有值，仅作被引用背景。",
                    "criteria": CHAT_INTENTS,
                },
                "risk": {
                    "type": "score",
                    "instructions": "如果直接回复 message 中的新发言，风险有多大？quote 只作背景。",
                    "criteria": RISK_LEVELS,
                },
                "behavior": {
                    "type": "choice",
                    "instructions": "message 中新写的话在对话里做了什么？quote 不是新发言。",
                    "criteria": BEHAVIORS,
                },
                "emotion": {
                    "type": "choice",
                    "instructions": "message 中新写的文字支持哪种情绪？证据不足选难判断；不要把 quote 当成对方现在的情绪。",
                    "criteria": EMOTIONS,
                },
                "need": {
                    "type": "choice",
                    "instructions": "根据 message 中的新发言，对方此刻最可能需要什么？quote 只作背景，不推断长期人格或关系。",
                    "criteria": NEEDS,
                },
                "reply_needed": {
                    "type": "noul",
                    "instructions": "对此刻的 message，马上给对方一条可见回复，是否比暂不回复、留出空间更合适？quote 不是新发言。",
                },
                "indirect_request": {
                    "type": "noul",
                    "instructions": "message 中的新发言是否含未直接说出的具体请求或期待？",
                },
                "pressure": {
                    "type": "noul",
                    "instructions": "message 中的新发言是否明显催促、施压或要求快速答复？",
                },
                "boundary": {
                    "type": "noul",
                    "instructions": "message 中的新发言是否明确设置拒绝、界限或不愿继续的边界？",
                },
            },
        }
        for action_id, label in ACTION_STRATEGIES.items():
            payload["questions"][f"action_{action_id}"] = {
                "type": "score",
                "instructions": (f"只评价行动策略「{label}」对 message 中新发言的适配程度；"
                                 "结合 context，quote 仅作背景；不要生成回复文本。"),
                "criteria": ACTION_FIT_LEVELS,
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
