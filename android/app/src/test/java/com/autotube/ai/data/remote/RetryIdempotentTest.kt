package com.autotube.ai.data.remote

import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.SocketPolicy
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Covers the bug where one screen showed "connected to backend" and the next
 * reported "cannot reach backend" against the same healthy endpoint.
 *
 * The endpoint was fine - it answers 200 in under a second and survives a
 * ten-call burst. What failed was the transport: a pooled connection the
 * server had already closed. These tests reproduce that with a server that
 * drops the connection, and assert the retry hides it for reads while leaving
 * writes alone.
 */
class RetryIdempotentTest {

    private lateinit var server: MockWebServer

    private val client = OkHttpClient.Builder()
        // Off, so the only retry under test is ours.
        .retryOnConnectionFailure(false)
        .addInterceptor(RetryIdempotent())
        .callTimeout(10, TimeUnit.SECONDS)
        .build()

    @Before fun start() { server = MockWebServer().also { it.start() } }
    @After fun stop() { server.shutdown() }

    @Test
    fun `a GET survives a dropped connection`() {
        server.enqueue(MockResponse().apply { socketPolicy = SocketPolicy.DISCONNECT_AT_START })
        server.enqueue(MockResponse().setResponseCode(200).setBody("""{"ok":true}"""))

        val response = client.newCall(
            Request.Builder().url(server.url("/niche/preview")).build()
        ).execute()

        assertEquals(200, response.code)
        assertEquals("""{"ok":true}""", response.body?.string())
        // Proof it actually retried rather than getting lucky.
        assertEquals(2, server.requestCount)
        response.close()
    }

    @Test
    fun `a GET gives up after the last attempt`() {
        repeat(3) {
            server.enqueue(MockResponse().apply {
                socketPolicy = SocketPolicy.DISCONNECT_AT_START
            })
        }
        var thrown: Exception? = null
        try {
            client.newCall(Request.Builder().url(server.url("/health")).build()).execute()
        } catch (e: IOException) {
            thrown = e
        }
        assertTrue("expected an IOException once retries ran out", thrown is IOException)
        assertEquals(3, server.requestCount)
    }

    @Test
    fun `a POST is never replayed`() {
        server.enqueue(MockResponse().apply { socketPolicy = SocketPolicy.DISCONNECT_AT_START })
        server.enqueue(MockResponse().setResponseCode(202).setBody("""{"accepted":true}"""))

        var thrown: Exception? = null
        try {
            client.newCall(
                Request.Builder()
                    .url(server.url("/automations"))
                    .post("""{"niche":"science"}""".toRequestBody())
                    .build()
            ).execute()
        } catch (e: IOException) {
            thrown = e
        }
        // Replaying this would queue a second automation and render a
        // duplicate video. An error the user can retry deliberately is the
        // cheaper outcome.
        assertTrue("a POST must not be retried", thrown is IOException)
        assertEquals(1, server.requestCount)
    }

    /**
     * The control. Without the interceptor the same dropped connection is
     * fatal - which is what the app was doing, and what made a healthy
     * endpoint look unreachable. If this ever starts passing, the first test
     * is no longer proving anything.
     */
    @Test
    fun `without the interceptor the same drop is fatal`() {
        val bare = OkHttpClient.Builder()
            .retryOnConnectionFailure(false)
            .callTimeout(10, TimeUnit.SECONDS)
            .build()
        server.enqueue(MockResponse().apply { socketPolicy = SocketPolicy.DISCONNECT_AT_START })
        server.enqueue(MockResponse().setResponseCode(200).setBody("{}"))

        var thrown: Exception? = null
        try {
            bare.newCall(Request.Builder().url(server.url("/niche/preview")).build()).execute()
        } catch (e: IOException) {
            thrown = e
        }
        assertTrue("the drop must be fatal without the retry", thrown is IOException)
        assertEquals(1, server.requestCount)
    }

    @Test
    fun `a successful GET is not repeated`() {
        server.enqueue(MockResponse().setResponseCode(200).setBody("{}"))
        val response = client.newCall(
            Request.Builder().url(server.url("/health")).build()
        ).execute()
        assertEquals(200, response.code)
        assertEquals(1, server.requestCount)
        response.close()
    }
}
