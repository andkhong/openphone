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
   with the app download (step 1, first time only) and an **Open OpenMob** button.
3. Tap **Open OpenMob**. The phone asks once, with its own screen-lock prompt:
   **Connect this phone to *\<server\>*? as *you@example.com***. Touch the fingerprint sensor (or
   enter your PIN, pattern or password). That's it: no sign-in on the phone.

So pairing is three steps and two taps plus one touch: scan, **Open OpenMob**, confirm.

What makes this safe:

- **The QR code is the credential.** The dashboard makes it only when you open **Connect a phone**.
  It works once, for 2 minutes, only for your account, and it has 128 bits of secret (the server
  keeps only a hash). A few wrong guesses void it, and the server limits attempts per network
  address and per account.
- **The phone confirms with its own screen.** The prompt names the server and the account, and
  needs the phone's screen lock, so a link from anywhere else can't pair your phone silently.
  OpenMob only pairs phones that have a screen lock.
- **The phone never holds your sign-in.** It gets its own device key, stored encrypted with a key in
  the phone's hardware keystore. That key can only drive this phone, and you can revoke it.
- **You see every new phone.** The dashboard shows **New phone added: SM-S948U1** with a one-click
  **Remove**, and each pairing is written to your account's audit log (Developer › Logs).

If the QR code expired, click **Make a new code** on the Connect page. Can't scan? The Connect page
also shows the address to open on the phone.

**No laptop at hand?** In the app, tap **No laptop? Sign in on this phone** and sign in with your
OpenMob account. The app uses that sign-in only to make a pairing code, signs out again at once, and
then shows the same one-touch confirmation. It never keeps the session.

### Developers: one command over USB

With the phone plugged into your computer (USB debugging on), one command installs the app, sets
it up and pairs it:

```sh
export OPENMOB_SERVER=https://openmob.example.com
export OPENMOB_API_KEY=om_live_…   # a key with the devices:add scope
openmob phone add                  # --serial SERIAL with several phones, --apk PATH for a local build
```

It needs an API key with the **`devices:add`** scope (make one on **Developer API & AI agents**, or
`openmob key create you@example.com --scopes devices:add` on the server). It's never a default
scope: it can start pairings for your account. The command:

| Step | How | On a second run |
|---|---|---|
| Install or update the app | `adb install -r` with the server's `/download/openmob.apk` (or `--apk`). adb installs aren't subject to Android's "restricted settings" | Skipped when the installed app is the same build |
| Notifications | `pm grant … POST_NOTIFICATIONS` (Android 13+) | Skipped when granted |
| Run in the background | `dumpsys deviceidle whitelist +dev.openmob.companion` | Skipped when exempt |
| Accessibility | Adds OpenMob to `enabled_accessibility_services`, **keeping every other service** you have on, and sets `accessibility_enabled 1` | Skipped when on |
| Screen sharing | Tries `appops set … PROJECT_MEDIA allow` and reports whether Android kept it. Android 14+ still asks once per connection, the first time you open the live view | Tried again |
| Pair | Makes a single-use, 2-minute enrollment code and opens the app on it. **The phone still shows its confirmation** naming the server and your account, passed with its screen lock: the server doesn't pair the phone until then | Skipped when the app is already paired to your account (read from a debug build) |
| Verify | Waits for the phone to come online and prints a summary with the time taken | |

So `phone add` needs one touch on the phone (the confirmation), plus **Allow** on "Allow USB
debugging?" the first time you plug in. Re-running it fixes whatever is missing. It explains what to
do when there's no phone, the phone hasn't allowed this computer, several phones are plugged in, the
phone runs Android older than 11, or a Samsung phone is plugged in but invisible to adb because of
**Auto Blocker** (Settings › Security and privacy › Auto Blocker › off; checked on macOS).

For a server on your own computer, run the command with the address you use locally: the server
gives the phone its network name (`http://<computer>.local:<port>`), so keep the phone on the same
Wi-Fi.

## 3. Finish the checklist in the app

After pairing, the app's **Set up** checklist shows the rest, in order, with the next step
highlighted and a progress count:

| Step | Why |
|---|---|
| **Turn on OpenMob in Accessibility** | Lets the agent see the screen and tap, type and swipe. The app explains what this allows before opening Settings: tap OpenMob (under "Downloaded apps" or "Installed apps") and turn it on. Android brings you back to the app once it's on |
| **Allow notifications** | For the always-visible "agent active" notification, with its Stop and Disconnect buttons, and for approval requests. One tap on Android's prompt |
| **Let OpenMob run in the background** | One tap: Android's own "Let app always run in background?" prompt (**Why?** explains it). Without it, Android may cut the connection when the screen is off. On **Samsung** phones, the step then offers **Never sleeping apps** (Battery › Background usage limits) as well; on **Pixel** the standard prompt is all it takes |
| **Share the screen for the live view** | Only appears once someone opens this phone's live view on the dashboard (the phone also gets a notification). It's never asked for at setup or when connecting |

**"Restricted setting" or a greyed-out switch?** Android blocks the accessibility switch for apps
installed outside Google Play until you allow it once: open the app's **App info**, tap the ⋮ menu
(top right), tap **Allow restricted settings** and confirm. Then turn OpenMob on in Accessibility.
If the menu doesn't have that option, try turning OpenMob on in Accessibility once first: Android
adds the option after that. (`openmob phone add` avoids this: apps installed over adb aren't
restricted.)

## 4. Connect

Once Accessibility is on, the app connects by itself after pairing; later, tap **Connect**. While it's connected, a notification says the OpenMob agent can control
the phone, and the phone shows as online (`"online": true` in
[`GET /v1/devices`](api.md#get-v1devices)).

**Smooth live view** (under Connection) shows the screen as video on your dashboard. Connecting
never asks for screen sharing: the first time someone opens the phone's live view, the phone offers
it (a notification and a **Share screen** step), and Android asks once for that connection. Until
then, and with the switch off, the dashboard shows a few screenshots a second. Either way, tasks
work the same.

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
| The QR code doesn't work | It's valid once, for 2 minutes. Click **Make a new code** on the Connect page |
| "Too many pairing attempts" | Several wrong codes came from your network or for your account. Wait 10 minutes and make a new code |

Every phone paired to your account, online or not, is listed under the account button →
**Your phones** (and on the Connect page). **Remove** revokes its key and closes its connections at
once. In the app, **Unpair this phone** does the same from the phone, proven with its own key.

## iPhone

iPhones aren't supported for end users: iOS doesn't let one app control others, so there is no
OpenMob app for iPhone.

For local development only, an iPhone can be driven from a **Mac** that has Xcode and Apple's
WebDriverAgent (the UI-testing tool Appium uses) installed, with the phone attached by USB and in
Developer Mode. The Mac runs `openmob bridge --ios` to connect it to a server, where it appears with
`"platform": "ios"`. It needs a source checkout of OpenMob, an Apple developer signing setup, and
the Mac staying on, so it isn't a way to offer OpenMob to iPhone users.
