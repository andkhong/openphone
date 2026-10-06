package dev.openmob.companion

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.PrivateKey
import java.security.Signature
import java.security.spec.ECGenParameterSpec

/**
 * The tap key: signs the owner's tap on Allow for tap-level gate prompts (three confirmation levels, owner
 * principle). Unlike [ApprovalKeys] it doesn't need the screen lock: the security basis is that only OpenMob's own
 * Allow button uses it, in OpenMob's own window, which the gatekeeper never lets an agent tap
 * (builtin:own_window), with filterTouchesWhenObscured against overlays. It never signs a fingerprint-level
 * prompt: those messages are domain-separated (`gk1|tap|…` vs `gk1|…`) and verified against the other key.
 */
object TapKeys {
    private const val ALIAS = "openmob_tap"

    private fun store(): KeyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }

    private fun key(): PrivateKey? = try {
        val ks = store()
        if (!ks.containsAlias(ALIAS)) {
            KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, "AndroidKeyStore").apply {
                initialize(KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_SIGN)
                    .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
                    .setDigests(KeyProperties.DIGEST_SHA256)
                    .build())
            }.generateKeyPair()
        }
        ks.getKey(ALIAS, null) as? PrivateKey
    } catch (e: Exception) {
        null
    }

    /** Sign a tap-level gate approval (only ever called from ApprovalActivity's Allow button). */
    fun sign(message: String): String? = try {
        val k = key() ?: throw IllegalStateException("no tap key")
        Signature.getInstance("SHA256withECDSA").run {
            initSign(k)
            update(message.toByteArray(Charsets.UTF_8))
            Base64.encodeToString(sign(), Base64.NO_WRAP)
        }
    } catch (e: Exception) {
        null
    }

    fun verify(message: String, signature: String): Boolean = try {
        val cert = store().getCertificate(ALIAS) ?: throw IllegalStateException("no key")
        Signature.getInstance("SHA256withECDSA").run {
            initVerify(cert.publicKey)
            update(message.toByteArray(Charsets.UTF_8))
            verify(Base64.decode(signature, Base64.NO_WRAP))
        }
    } catch (e: Exception) {
        false
    }
}
