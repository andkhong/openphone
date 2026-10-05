package dev.openmob.companion

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Handler
import android.os.Looper
import android.widget.Toast
import org.json.JSONObject

/**
 * Phone approvals for risky actions (docs/phone-protocol.md, section 3.3). The server asks with
 * `approval_request`, and only the person holding the phone answers, never the calling model:
 * Deny needs nothing, Approve needs the screen lock (ApprovalActivity). Each request is answered
 * at most once. With no answer it expires after [EXPIRY_MS]: the notification goes away and
 * nothing is sent, so the server's own timeout counts it as a no.
 *
 * Everything here runs on the main thread.
 */
object Approvals {
    /** `deviceName`: the phone that would act, when the server says (it may be another of the owner's phones). */
    data class Request(
        val id: String, val taskId: String, val question: String, val requester: String, val deviceName: String = "",
    ) {
        /** "On Pixel 9: Send message to Mom?", or just the question when the phone isn't named. */
        val shownQuestion: String get() = if (deviceName.isEmpty()) question else "On $deviceName: $question"
    }

    const val EXTRA_ID = "dev.openmob.APPROVAL_ID"
    const val ACTION_DENY = "dev.openmob.APPROVAL_DENY"
    private const val CHANNEL = "approvals"
    private const val NOTIFICATION_ID = 2 // one per request, told apart by tag (the request id)
    private const val EXPIRY_MS = 2 * 60 * 1000L
    private const val REMEMBERED = 200

    private val pending = LinkedHashMap<String, Request>()
    private val finished = LinkedHashSet<String>() // answered or expired: never shown or sent again
    private val main = Handler(Looper.getMainLooper())

    fun pending(): List<Request> = pending.values.toList()

    fun get(id: String): Request? = pending[id]

    /** A new `approval_request` from the server. Repeats of a known id are ignored. */
    fun request(context: Context, message: JSONObject) {
        val id = message.optString("id")
        if (id.isEmpty() || id in pending || id in finished) return
        val app = context.applicationContext
        val request = Request(
            id = id,
            taskId = message.optString("task_id"),
            question = message.optString("question").trim().ifEmpty { app.getString(R.string.approval_no_question) },
            requester = message.optString("requester").trim().ifEmpty { app.getString(R.string.approval_unknown_requester) },
            deviceName = message.optString("device_name").trim(),
        )
        pending[id] = request
        main.postDelayed({ expire(app, id) }, id, EXPIRY_MS)
        notify(app, request)
        AppState.addEvent("approval", app.getString(R.string.approval_event_asked, request.requester, request.shownQuestion))
    }

    /**
     * Sends the owner's answer, once. False if the request is gone (answered or expired) or the
     * phone is offline; in the offline case it stays pending so the owner can try again.
     */
    fun answer(context: Context, id: String, approved: Boolean): Boolean {
        val request = pending[id] ?: return false
        if (!ConnectionService.sendApproval(id, approved)) return false
        finish(context, id)
        val app = context.applicationContext
        AppState.addEvent(
            "approval",
            app.getString(if (approved) R.string.approval_event_approved else R.string.approval_event_denied, request.question),
        )
        return true
    }

    /** The server withdrew a request (answered on another phone, or the task moved on): drop it unanswered. */
    fun cancel(context: Context, id: String) {
        if (pending[id] == null) return
        finish(context, id)
        AppState.notifyListeners()
    }

    /** The connection was closed by the owner: drop every pending request without answering. */
    fun clear(context: Context) {
        pending.keys.toList().forEach { dismiss(context, it) }
        pending.clear()
        AppState.notifyListeners()
    }

    private fun expire(context: Context, id: String) {
        val request = pending[id] ?: return
        finish(context, id)
        AppState.addEvent("approval", context.getString(R.string.approval_event_expired, request.question))
    }

    private fun finish(context: Context, id: String) {
        pending.remove(id)
        finished.add(id)
        while (finished.size > REMEMBERED) finished.remove(finished.first())
        dismiss(context, id)
    }

    private fun dismiss(context: Context, id: String) {
        main.removeCallbacksAndMessages(id)
        context.getSystemService(NotificationManager::class.java).cancel(id, NOTIFICATION_ID)
    }

    /** High priority so it shows heads-up; the question stays hidden on the lock screen. */
    private fun notify(context: Context, request: Request) {
        val manager = context.getSystemService(NotificationManager::class.java)
        if (manager.getNotificationChannel(CHANNEL) == null) {
            manager.createNotificationChannel(
                NotificationChannel(CHANNEL, context.getString(R.string.approval_channel), NotificationManager.IMPORTANCE_HIGH)
                    .apply { description = context.getString(R.string.approval_channel_description) }
            )
        }
        val code = request.id.hashCode()
        val review = PendingIntent.getActivity(
            context, code,
            Intent(context, ApprovalActivity::class.java)
                .setIdentifier(request.id) // a distinct PendingIntent per request
                .putExtra(EXTRA_ID, request.id)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val deny = PendingIntent.getBroadcast(
            context, code,
            Intent(context, ApprovalReceiver::class.java).setAction(ACTION_DENY)
                .setIdentifier(request.id)
                .putExtra(EXTRA_ID, request.id),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val text = context.getString(R.string.approval_notification_text, request.requester)
        val hidden = Notification.Builder(context, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_dialog_alert)
            .setContentTitle(context.getString(R.string.approval_notification_public))
            .build()
        val notification = Notification.Builder(context, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_dialog_alert)
            .setContentTitle(context.getString(R.string.approval_notification_title, request.shownQuestion))
            .setContentText(text)
            .setStyle(Notification.BigTextStyle().bigText("${request.shownQuestion}\n\n$text"))
            .setCategory(Notification.CATEGORY_REMINDER)
            .setVisibility(Notification.VISIBILITY_PRIVATE)
            .setPublicVersion(hidden)
            .setTimeoutAfter(EXPIRY_MS) // also dismisses it if this process is gone by then
            .setContentIntent(review)
            .addAction(Notification.Action.Builder(null, context.getString(R.string.approval_deny), deny).build())
            .addAction(Notification.Action.Builder(null, context.getString(R.string.approval_review), review).build())
            .build()
        manager.notify(request.id, NOTIFICATION_ID, notification)
    }
}

/** The notification's Deny button. Denying needs no screen lock. */
class ApprovalReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Approvals.ACTION_DENY) return
        val id = intent.getStringExtra(Approvals.EXTRA_ID) ?: return
        if (Approvals.get(id) != null && !Approvals.answer(context, id, false)) {
            Toast.makeText(context, R.string.approval_offline, Toast.LENGTH_SHORT).show()
        }
    }
}
