# Set up a phone

Before any code or agent can use a phone, its owner installs the OpenMob app on it and pairs it
with their account. This page is what the owner does, and what they see afterwards. It takes about
five minutes.

You need an **Android phone with Android 11 or newer**, with a screen lock (PIN, pattern, password
or fingerprint) set up. iPhones aren't supported: see [iPhone](#iphone).

## 1. Get the app

The app is downloaded from your OpenMob server, at `https://<server>/download/openmob.apk`. You
don't need to type that address: the pairing page in step 2 links to it.

OpenMob isn't on Google Play. Google Play's policies don't allow an app that lets an AI agent see
and operate the whole phone through Android's accessibility service, which is exactly what
OpenMob does. So Android installs it as an app "from an unknown source":

1. Download `OpenMob.apk` and open it.
2. If Android asks, allow your browser to **install unknown apps** (Android shows a switch for it),
   then go back and tap **Install**.

## 2. Pair it with your account

1. On a computer, open your dashboard, click the account button (bottom left) → **Connect a phone**.
   It shows a QR code.
2. Scan the QR code with the phone's camera. It opens a page on the phone, **Set up this phone**,
   with the app download (step 1) and an **Open OpenMob** button.
3. Tap **Open OpenMob**. The app opens with your server and a one-time code already filled in.
4. Sign in with the same OpenMob account you use on the dashboard.
5. Confirm with the phone's screen lock (**Allow your OpenMob agent to control this phone?**).

The code works once, for 10 minutes, and only for your account: whoever scans it must sign in as
you and pass your screen lock. OpenMob only pairs phones that have a screen lock, so only you can
approve agents on it. If the QR code expired, click **Make a new code** on the Connect page. Can't scan?
The Connect page also shows the address to open on the phone.

## 3. Finish the checklist in the app

The app's **Set up** checklist walks through the rest:

| Step | Why |
|---|---|
| **Turn on OpenMob in Accessibility** | Lets the agent see the screen and tap, type and swipe. The app explains what this allows before opening Settings: tap OpenMob (under "Downloaded apps" or "Installed apps") and turn it on |
| **Allow notifications** | For the always-visible "agent active" notification, with its Stop and Disconnect buttons, and for approval requests |
| **Let OpenMob run in the background** | Exempts OpenMob from battery optimisation. Without it, Android may cut the connection when the screen is off, and the agent loses the phone mid-task |

**"Restricted setting" or a greyed-out switch?** Android blocks the accessibility switch for apps
installed outside Google Play until you allow it once: open the app's **App info**, tap the ⋮ menu
(top right), tap **Allow restricted settings** and confirm. Then turn OpenMob on in Accessibility.
If the menu doesn't have that option, try turning OpenMob on in Accessibility once first: Android
adds the option after that.

## 4. Connect

Tap **Connect** in the app. While it's connected, a notification says the OpenMob agent can control
the phone, and the phone shows as online (`"online": true` in
[`GET /v1/devices`](api.md#get-v1devices)).

**Smooth live view** (under Connection) shows the screen as video on your dashboard. With it on,
Android asks you to allow screen sharing each time you connect. With it off, the dashboard shows a
few screenshots a second instead, with no prompt. Either way, tasks work the same.

Keep the app connected while you want agents to be able to use the phone. If the connection drops
(Wi-Fi to mobile data, for example), the app reconnects by itself, retrying every few seconds. A
task that was running waits up to 45 seconds for the phone to come back.

## Locks and the screen

A task needs the phone **awake and unlocked**:

- A phone with a **PIN, pattern, password or fingerprint** lock must be unlocked by you. OpenMob
  never types a PIN or password. A task for a phone that is locked, or whose screen is off, is
  refused with `409 phone_locked` ("Unlock it to continue"), and a step of a multi-phone run waits
  (`waiting_for_phone`) until you unlock it.
- A **swipe-only** lock (no PIN) is dismissed automatically, and OpenMob wakes the screen if needed.

For a phone that agents use often, set the screen timeout to a few minutes so it doesn't lock in
the middle of your work.

## While an agent works

- The notification and the app say who is using the phone: "**CI bot** is using your phone", with
  the name of the API key, and later "CI bot stopped using your phone".
- The app's **Activity** list shows what the agent hears, does and says.
- **To stop it:** tap **Stop** in the notification. That stops the running task and anything
  queued, and disconnects the phone; tap **Connect** again when you want agents back. Over the API,
  the task ends as `cancelled`; for a [multi-phone run](api.md#orchestrations), the whole run ends
  (`stopped_on_phone`).
- You can also say or type "stop" in the app's **Ask your agent** box, with the same effect on the
  task, without disconnecting.
- **Questions** ("Which account should I use?") appear in the app under **Your agent is asking**.
  You can answer there, or whoever started the task answers through the API.

### Approving risky actions

Before the agent buys, sends, deletes or posts something, you get an approval request: a
notification "**Approve?** …" with **Deny** and **Review**. Tap **Review**, read what it wants to do
and who is asking, then tap **Approve** and confirm with your screen lock, or tap **Deny**.

- The request goes to **all** your online phones, so you can answer on whichever you're holding.
  It names the phone that would act. The first answer counts and the other phones drop the request.
- Approving always needs your screen lock, so the AI using your phone can't approve for itself.
- No answer within about 2 minutes counts as **Deny**.
- Denying a step of a multi-phone run ends the whole run.

See [Safety & approvals](safety.md) for what the agent may and may not do.

## Troubleshooting

| Symptom | Fix |
|---|---|
| The phone shows offline, or tasks fail with `409 phone_offline` | Open the app and tap **Connect**. Check the phone has a network connection |
| `409 phone_locked` | Unlock the phone (and wake the screen), then retry |
| `409 phone_unresponsive` | The app is connected but didn't answer in 5 seconds, usually because Android put it to sleep. Wake the phone, open the app, and make sure battery optimisation is off for OpenMob (below) |
| It disconnects whenever the screen turns off | Allow OpenMob to run in the background. **Samsung:** Settings → Apps → OpenMob → Battery → **Unrestricted**, and remove OpenMob from "Sleeping apps" under Battery → Background usage limits. **Pixel:** Settings → Apps → OpenMob → App battery usage → **Unrestricted** |
| "Accessibility is off" in the app | Turn OpenMob on again in Settings → Accessibility. Some phones turn it off after an app update |
| A task failed with "The phone didn't reconnect within 45 s" | The connection dropped mid-task and didn't come back in time. Check the network and battery settings, then run it again |
| No approval notifications | Allow notifications for OpenMob. Approval requests also show in the app |
| The QR code doesn't work | It's valid once, for 10 minutes. Click **Make a new code** on the Connect page |

To remove a phone from your account, use **Remove** on the dashboard's Connect page, or
**Sign out & unpair** in the app.

## iPhone

iPhones aren't supported for end users: iOS doesn't let one app control others, so there is no
OpenMob app for iPhone.

For local development only, an iPhone can be driven from a **Mac** that has Xcode and Apple's
WebDriverAgent (the UI-testing tool Appium uses) installed, with the phone attached by USB and in
Developer Mode. The Mac runs `openmob bridge --ios` to connect it to a server, where it appears with
`"platform": "ios"`. It needs a source checkout of OpenMob, an Apple developer signing setup, and
the Mac staying on, so it isn't a way to offer OpenMob to iPhone users.
