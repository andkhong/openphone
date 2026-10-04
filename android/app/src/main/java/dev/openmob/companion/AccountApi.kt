package dev.openmob.companion

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Blocking HTTP calls to the OpenMob server's account and pairing endpoints. Call only from a
 * background thread. Failures throw [ApiError] with a message fit to show the user.
 */
class AccountApi(server: String) {
    class ApiError(message: String, val status: Int = 0) : Exception(message)

    data class Me(val accounts: Boolean, val email: String?)
    data class SignedIn(val email: String, val session: String)

    private val base = server.trim().trimEnd('/')

    fun login(email: String, password: String): SignedIn = signIn("/api/auth/login", email, password)

    fun signup(email: String, password: String): SignedIn = signIn("/api/auth/signup", email, password)

    fun me(session: String): Me {
        val body = call(Request.Builder().url("$base/api/auth/me").header(SESSION_HEADER, session).get().build())
        val email = if (body.isNull("email")) null else body.optString("email").ifBlank { null }
        return Me(body.optBoolean("accounts", false), email)
    }

    /** Returns this phone's device key. */
    fun redeem(session: String, code: String, deviceId: String, model: String): String {
        val json = JSONObject().put("code", code).put("device_id", deviceId).put("model", model)
        val body = call(
            Request.Builder().url("$base/api/pair/redeem").header(SESSION_HEADER, session).post(json.toBody()).build()
        )
        return body.optString("key").ifBlank { throw ApiError("The server did not return a device key.") }
    }

    /** Best effort: revoke this phone's key and end the session. Errors are ignored. */
    fun unpair(session: String, deviceId: String) {
        val empty = JSONObject().toBody()
        listOf("$base/api/devices/$deviceId/revoke", "$base/api/auth/logout").forEach { url ->
            try {
                client.newCall(Request.Builder().url(url).header(SESSION_HEADER, session).post(empty).build())
                    .execute().close()
            } catch (_: Exception) {
            }
        }
    }

    private fun signIn(path: String, email: String, password: String): SignedIn {
        val json = JSONObject().put("email", email).put("password", password)
        val body = call(Request.Builder().url(base + path).post(json.toBody()).build())
        val session = body.optString("session").ifBlank { throw ApiError("The server did not return a session.") }
        return SignedIn(body.optString("email").ifBlank { email }, session)
    }

    private fun call(request: Request): JSONObject {
        val response = try {
            client.newCall(request).execute()
        } catch (e: IOException) {
            throw ApiError("Couldn't reach the server: ${e.message ?: e.javaClass.simpleName}")
        }
        response.use {
            val text = it.body?.string().orEmpty()
            val json = try { JSONObject(text) } catch (_: Exception) { null }
            if (!it.isSuccessful) {
                // FastAPI validation errors (422) carry a list here, not a sentence; fall back.
                val detail = (json?.opt("detail") as? String).orEmpty().ifBlank { "Server error (HTTP ${it.code})." }
                throw ApiError(detail, it.code)
            }
            return json ?: throw ApiError("Unexpected reply from the server.")
        }
    }

    private fun JSONObject.toBody() = toString().toRequestBody(JSON)

    companion object {
        const val SESSION_HEADER = "X-OpenMob-Session"
        private val JSON = "application/json; charset=utf-8".toMediaType()
        private val client = OkHttpClient.Builder()
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(20, TimeUnit.SECONDS)
            .build()
    }
}
