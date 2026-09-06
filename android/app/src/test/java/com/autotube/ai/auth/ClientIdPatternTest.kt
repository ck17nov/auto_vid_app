package com.autotube.ai.auth

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Guards the client-ID check.
 *
 * It exists to catch a pasted client secret or a truncated id before the
 * browser opens, because Google answers those with the same opaque "invalid
 * request" page it gives every other misconfiguration. It must not be
 * stricter than that: a real id has to pass, whatever its length.
 */
class ClientIdPatternTest {

    private fun accepts(id: String) =
        YouTubeAuthManager.CLIENT_ID_PATTERN.matches(id)

    @Test
    fun `accepts a real android client id`() {
        assertTrue(accepts("123456789012-abc123def456.apps.googleusercontent.com"))
    }

    @Test
    fun `accepts the reported id`() {
        // Verbatim from the device. Long hyphenated segments are normal and a
        // check that rejected this would block a correctly configured user.
        assertTrue(
            accepts(
                "530289804527-j91ljoef5abd50c353ic3g25pf8u0v41" +
                    ".apps.googleusercontent.com"
            )
        )
    }

    @Test
    fun `accepts a legacy id with no hyphenated segment`() {
        assertTrue(accepts("123456789012.apps.googleusercontent.com"))
    }

    @Test
    fun `rejects a client secret`() {
        assertFalse(accepts("GOCSPX-abcdefghijklmnopqrstuvwx"))
    }

    @Test
    fun `rejects a truncated id`() {
        assertFalse(accepts("123456789012-abc123def456"))
    }

    @Test
    fun `rejects trailing text after the domain`() {
        assertFalse(accepts("123456789012-abc.apps.googleusercontent.com.evil.test"))
    }
}
