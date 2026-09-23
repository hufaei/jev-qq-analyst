package info.jevqq.analyst

import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import java.util.concurrent.TimeUnit

class DecisionClientTest {
    private lateinit var server: MockWebServer

    @Before fun start() {
        server = MockWebServer()
        server.start()
    }

    @After fun stop() = server.shutdown()

    private fun endpoint() = server.url("/v1/systemone").toString()

    private fun spec(): JSONObject = JSONObject(
        javaClass.classLoader!!.getResourceAsStream("decision_questions.json")!!
            .bufferedReader().use { it.readText() }
    )

    private fun answer(model: String = "jev-1.13.0") = MockResponse().setBody(
        """{"model":"$model","answers":{"intent":{"type":"choice","choice":"闲聊","confidence":0.8},"risk":{"type":"score","score":2.0}}}"""
    )

    @Test fun directJevSendsBearerAndAcceptsResolvedRelease() {
        server.enqueue(answer())
        val client = DecisionClient(
            ConnectionSettings(EndpointMode.JEV, endpoint(), "jev-latest", "secret"), spec()
        )
        val verdict = client.judge("你好", "我: 在吗", "之前的话")
        val request = server.takeRequest(1, TimeUnit.SECONDS)!!
        val body = JSONObject(request.body.readUtf8())
        assertEquals("/v1/systemone", request.path)
        assertEquals("Bearer secret", request.getHeader("Authorization"))
        assertEquals("jev-latest", body.getString("model"))
        assertEquals("你好", body.getJSONObject("state").getString("message"))
        assertEquals("我: 在吗", body.getJSONObject("state").getString("context"))
        assertEquals("之前的话", body.getJSONObject("state").getString("quote"))
        assertEquals(21, body.getJSONObject("questions").length())
        assertEquals("jev-1.13.0", verdict.backend)
    }

    @Test fun gatewayNeverSendsKeyAndRequiresExactRoute() {
        server.enqueue(answer("other-model"))
        val client = DecisionClient(
            ConnectionSettings(EndpointMode.GATEWAY, endpoint(), "jev-latest", "secret"), spec()
        )
        try {
            client.judge("你好")
            fail("mismatched gateway route must fail")
        } catch (expected: IllegalStateException) {
            assertTrue(expected.message!!.contains("路由"))
        }
        assertNull(server.takeRequest(1, TimeUnit.SECONDS)!!.getHeader("Authorization"))
    }

    @Test fun rejectsCleartextRemoteEndpointBeforeSendingKey() {
        val settings = ConnectionSettings(
            EndpointMode.JEV, "http://192.168.1.2:8080/v1/systemone", "jev-latest", "secret"
        )
        try {
            DecisionClient(settings, spec()).judge("你好")
            fail("remote HTTP must be rejected")
        } catch (expected: IllegalArgumentException) {
            assertTrue(expected.message!!.contains("HTTPS"))
        }
    }

    @Test fun unauthorizedResponseDoesNotRetry() {
        server.enqueue(MockResponse().setResponseCode(401))
        val client = DecisionClient(
            ConnectionSettings(EndpointMode.JEV, endpoint(), "jev-latest", "bad"), spec()
        )
        try {
            client.judge("你好")
            fail("401 must fail")
        } catch (expected: IllegalStateException) {
            assertTrue(expected.message!!.contains("401"))
        }
        assertEquals(1, server.requestCount)
    }
}
