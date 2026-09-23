package info.jevqq.analyst

import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URI
import java.net.URL

internal enum class EndpointMode { JEV, GATEWAY }

internal data class ConnectionSettings(
    val mode: EndpointMode,
    val url: String,
    val model: String = "jev-latest",
    val apiKey: String = "",
) {
    fun endpoint(): URL {
        val uri = try { URI(url.trim()) }
        catch (_: Exception) { throw IllegalArgumentException("请输入有效的接口 URL") }
        val host = uri.host?.lowercase() ?: throw IllegalArgumentException("接口 URL 缺少主机")
        if (uri.scheme !in setOf("http", "https") || uri.userInfo != null ||
            uri.query != null || uri.fragment != null || model.isBlank()
        ) throw IllegalArgumentException("接口 URL 或模型路由无效")
        if (uri.scheme == "http" && host !in setOf("127.0.0.1", "localhost", "::1", "10.0.2.2")) {
            throw IllegalArgumentException("非本机地址必须使用 HTTPS")
        }
        val path = uri.path.orEmpty().trimEnd('/')
        val endpointPath = when {
            path.endsWith("/v1/systemone") -> path
            path.endsWith("/v1") -> "$path/systemone"
            else -> "$path/v1/systemone"
        }
        return URI(uri.scheme, null, host, uri.port, endpointPath, null, null).toURL()
    }
}

internal data class RankedIntent(val label: String, val probability: Double)
internal data class RankedAction(val label: String, val score: Double)
internal data class Verdict(
    val intent: String,
    val confidence: Double,
    val risk: Double,
    val behavior: String,
    val behaviorConfidence: Double,
    val emotion: String,
    val emotionConfidence: Double,
    val need: String,
    val needConfidence: Double,
    val signals: List<String>,
    val replyProbability: Double?,
    val rankedIntents: List<RankedIntent>,
    val rankedActions: List<RankedAction>,
    val backend: String,
)

/** Jev /v1/systemone request and response, with one HTTP attempt per judgment. */
internal class DecisionClient(
    private val settings: ConnectionSettings,
    private val spec: JSONObject,
) {
    fun judge(message: String, context: String? = null, quote: String = ""): Verdict {
        val state = JSONObject().put("message", message)
        if (!context.isNullOrEmpty()) state.put("context", context)
        if (quote.isNotEmpty()) state.put("quote", quote)
        val payload = JSONObject()
            .put("model", settings.model.trim())
            .put("state", state)
            .put("questions", spec.getJSONObject("questions"))
        return parse(post(payload))
    }

    private fun post(payload: JSONObject): JSONObject {
        val connection = settings.endpoint().openConnection() as HttpURLConnection
        connection.requestMethod = "POST"
        connection.instanceFollowRedirects = false
        connection.connectTimeout = 5000
        connection.readTimeout = 15000
        connection.doOutput = true
        connection.setRequestProperty("Content-Type", "application/json; charset=utf-8")
        connection.setRequestProperty("Accept", "application/json")
        if (settings.mode == EndpointMode.JEV && settings.apiKey.isNotBlank()) {
            connection.setRequestProperty("Authorization", "Bearer ${settings.apiKey.trim()}")
        }
        try {
            connection.outputStream.use { it.write(payload.toString().toByteArray(Charsets.UTF_8)) }
            val status = connection.responseCode
            if (status !in 200..299) throw IllegalStateException(when (status) {
                401, 403 -> "接口鉴权失败（HTTP $status）"
                404 -> "接口或模型路由不存在（HTTP 404）"
                503 -> "模型暂不可用（HTTP 503）"
                in 300..399 -> "接口重定向已拒绝（HTTP $status）"
                else -> "接口返回 HTTP $status"
            })
            return JSONObject(connection.inputStream.bufferedReader(Charsets.UTF_8).use { it.readText() })
        } finally {
            connection.disconnect()
        }
    }

    private fun parse(response: JSONObject): Verdict {
        val model = response.optString("model")
        if (model.isBlank() || (settings.mode == EndpointMode.GATEWAY && model != settings.model.trim())) {
            throw IllegalStateException("接口返回的模型路由无效")
        }
        val answers = response.optJSONObject("answers") ?: throw IllegalStateException("判断响应缺少 answers")
        val intentAnswer = answers.optJSONObject("intent") ?: throw IllegalStateException("判断响应缺少 intent")
        val riskAnswer = answers.optJSONObject("risk") ?: throw IllegalStateException("判断响应缺少 risk")
        val intent = intentAnswer.optString("choice")
        val confidence = intentAnswer.optDouble("confidence", Double.NaN)
        val risk = riskAnswer.optDouble("score", Double.NaN)
        val criteria = spec.getJSONObject("questions").getJSONObject("intent").getJSONObject("criteria")
        if (intentAnswer.optString("type") != "choice" || !criteria.has(intent) ||
            !confidence.isFinite() || confidence !in 0.0..1.0 ||
            riskAnswer.optString("type") != "score" || !risk.isFinite() || risk !in 0.0..9.0
        ) throw IllegalStateException("判断响应字段无效")

        fun selected(id: String): Pair<String, Double> {
            val answer = answers.optJSONObject(id) ?: return "" to 0.0
            val choice = answer.optString("choice")
            val probability = answer.optDouble("confidence", Double.NaN)
            val options = spec.getJSONObject("questions").getJSONObject(id).getJSONObject("criteria")
            return if (answer.optString("type") == "choice" && options.has(choice) &&
                probability.isFinite() && probability in 0.0..1.0
            ) choice to probability else "" to 0.0
        }

        fun probability(id: String): Double? {
            val answer = answers.optJSONObject(id) ?: return null
            val value = answer.optDouble("noul", Double.NaN)
            return value.takeIf { answer.optString("type") == "noul" && it.isFinite() && it in 0.0..1.0 }
        }

        val (behavior, behaviorConfidence) = selected("behavior")
        val (emotion, emotionConfidence) = selected("emotion")
        val (need, needConfidence) = selected("need")
        val signals = spec.getJSONObject("signal_labels").let { labels ->
            labels.keys().asSequence().filter { (probability(it) ?: 0.0) >= .72 }
                .map { labels.getString(it) }.toList()
        }
        val reply = probability("reply_needed")
        val rawIntents = intentAnswer.optJSONObject("probabilities")
        val rankedIntents = criteria.keys().asSequence().mapNotNull { label ->
            val value = rawIntents?.optDouble(label, Double.NaN) ?: Double.NaN
            value.takeIf { it.isFinite() && it in 0.0..1.0 }?.let { RankedIntent(label, it) }
        }.sortedByDescending { it.probability }.take(3).toList()
            .ifEmpty { listOf(RankedIntent(intent, confidence)) }
        val rankedActions = spec.getJSONObject("actions").let { labels ->
            labels.keys().asSequence().mapNotNull { id ->
                val answer = answers.optJSONObject("action_$id") ?: return@mapNotNull null
                val score = answer.optDouble("score", Double.NaN)
                if (answer.optString("type") == "score" && score.isFinite() && score in 0.0..4.0)
                    RankedAction(labels.getString(id), score) else null
            }.filter { action ->
                when {
                    reply != null && reply < .40 -> action.label == labels.getString("pause")
                    reply != null && reply > .60 -> action.label != labels.getString("pause")
                    else -> true
                }
            }.sortedByDescending { it.score }.take(3).toList()
        }
        return Verdict(intent, confidence, risk, behavior, behaviorConfidence,
            emotion, emotionConfidence, need, needConfidence, signals, reply,
            rankedIntents, rankedActions, model)
    }
}
