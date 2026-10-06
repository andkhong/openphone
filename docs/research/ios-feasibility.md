# iPhone support: feasibility study

Status: research, 2026-10-05. Not a decision. Written against `feature/vision-speed`
(`openmob/phone/ios.py`, `docs/ios.md`).

**How to read this document.** Each claim carries one of three tags:
- **[V]** verified fact, with a link to the source;
- **[I]** inference: our reasoning from verified facts, not confirmed anywhere;
- **[T]** unknown that must be tested on a real device before we commit to anything.

Primary sources (Apple documentation, WWDC sessions, the App Review Guidelines) are used where they
exist. Several of the most important facts, especially about iOS 27 and how competitors work, come
only from open-source code, vendor documentation or developer reports, and are flagged as such.

---

## 0. Summary

1. **No App Store app can operate other apps on an iPhone. Nothing announced through iOS 27 changes
   that.** iOS has no equivalent of Android's AccessibilityService for third-party apps. App
   Intents, Shortcuts and Siri only call actions that each app chooses to expose. Every working
   "AI drives an iPhone" product today, Mobilerun and mobile-mcp included, relies on **a computer
   paired with the phone** and Apple's **developer tooling**: XCUITest/WebDriverAgent, or new in
   iOS 27, CoreDevice remote input. **[V]**, see §2.
2. **The surprise: iOS 27 adds remote touch, keyboard and screen video for a paired host, with no
   signed test runner.** Xcode 27's Device Hub can view and control physical iPhones. Underneath,
   the device exposes CoreDevice services for an HID digitizer, keyboard, buttons and a screen
   stream. These are reachable from macOS, Linux or Windows through `pymobiledevice3`, and
   Mobilerun already ships a "native control" mode that uses them on iOS 27 with **no WebDriverAgent
   signing**. This removes the 7-day free-provisioning expiry and the Xcode setup. It still requires
   Developer Mode, a pairing over USB, and a host nearby. It gives pixels only, with no element
   tree. **[V]** from community and vendor sources, not from Apple's documentation (§3.2).
3. **So the realistic iOS product is a "bridge" on the user's own computer**: a Mac app first, a
   Linux box for enterprises later. It combines iOS 27 native input and video (fast, no expiry) with
   WebDriverAgent where an element tree is needed. A separate App Store companion app handles
   approvals, which keeps the screen-lock approval model.
4. **Consumers without a computer can't be served on iOS today.** That is the honest answer.

---

## 1. Why iOS is different

| Area | Android (what OpenMob uses) | iOS |
|---|---|---|
| Reading other apps' UI | `AccessibilityService` gives any app with user consent the element tree of every window | No public API. Each app is sandboxed and can't read outside its own container **[V]** ([Guideline 2.5.2](https://developer.apple.com/app-store/review/guidelines/#software-requirements); [Apple Platform Security: runtime process security](https://support.apple.com/guide/security/security-of-runtime-process-sec15bfe098e/web)) |
| Injecting input | `dispatchGesture`, `performAction`, `setText` | No public API. Only XCTest/XCUITest, which runs only under development signing on Developer Mode devices **[V]** ([Enabling Developer Mode](https://developer.apple.com/documentation/xcode/enabling-developer-mode-on-a-device)) |
| Screen capture | `takeScreenshot`, MediaProjection | ReplayKit broadcast extension: system-wide capture that the user starts, capped at **50 MB** of memory **[V]** ([Apple forums](https://developer.apple.com/forums/thread/651367); [Twilio](https://www.twilio.com/docs/video/ios-v4-screen-share.md)) |
| Staying connected | Foreground service holding a WebSocket | Background modes are allowed only for their stated purposes **[V]** ([Guideline 2.5.4](https://developer.apple.com/app-store/review/guidelines/#software-requirements)). An app can't keep a control socket open indefinitely **[I]** |
| Store policy | Play allows AccessibilityService tools, with a declaration | Apps must use **public APIs only, for their intended purpose** (2.5.1), may not run code that changes other apps (2.5.2), and must show consent and a visible indicator when recording (2.5.14) **[V]** ([guidelines](https://developer.apple.com/app-store/review/guidelines/)) |
| Remote desktop | Allowed | 4.2.7 covers iOS apps that **view** a Mac, PC or console on a local network. It doesn't let an iOS app expose itself to remote control **[V]** ([4.2.7](https://developer.apple.com/app-store/review/guidelines/#minimum-functionality)) |

The remote-support market confirms the gap. TeamViewer's iOS product offers **screen sharing only**
("Due to limitations of iOS, only screen-sharing is possible") **[V]**
([TeamViewer community](https://community.teamviewer.com/English/discussion/58790/need-clarification-on-ios-vs-android-support-for-screen-sharing)).
Developer-forum questions asking for an "AnyDesk for iOS" have gone unanswered
([thread 795216](https://developer.apple.com/forums/thread/795216),
[thread 702263](https://developer.apple.com/forums/thread/702263)). **[V]**

Apple does ship remote control, but only in its own apps. Since iOS 18, a contact can take
**remote control during a one-to-one FaceTime screen share**
([Apple Support](https://support.apple.com/en-mn/guide/iphone/iph5d70f34a3/ios)). macOS 15+ includes
**iPhone Mirroring** ([Apple Support 120421](https://support.apple.com/120421)). iOS 27 adds Device
Hub control for developers (§3.2). **[V]** None of these has a third-party API. **[I]**

---

## 2. What competitors actually do

| Product | iOS mechanism | Requirements | Source |
|---|---|---|---|
| **Mobilerun** (formerly DroidRun), "iOS Portal" | The original `ios-portal` was **an XCTest UI-test bundle running an HTTP server on the phone** (port 6643). It is now **archived**, replaced by `mobilerun-ios` | Mac, Xcode, signing team, `iproxy` | [github.com/droidrun/ios-portal](https://github.com/droidrun/ios-portal) (README, archived) **[V]** |
| **Mobilerun** `mobilerun-ios` (current) | Below iOS 27, a signed **"Mobilerun WebDriverAgent"**. **On iOS 27+, "native RemoteXPC control" that "does not need WDA signing"** but is "not feature-equivalent to WDA" | "A Mac and an iPhone connected with a data-capable USB cable", Developer Mode, a developer disk image. "Keep the process running." No hosted iPhones: iOS is only offered as "Personal Phone" | [Connect an iPhone](https://docs.mobilerun.ai/guides/connect-iphone.md), [mobilerun-ios overview](https://docs.mobilerun.ai/mobilerun-ios/overview), [device types](https://docs.mobilerun.ai/device-types) **[V]** |
| **mobile-mcp** (mobile-next) | Appium's **WebDriverAgent**, reached through **go-ios** (`ios tunnel start --userspace`, `ios forward 8100 8100`). Its newer `mobilecli` runs a "DeviceKit" XCTest agent on the phone | Mac, Xcode signing, tunnel and forward kept running | [mobile-mcp wiki: iOS real device](https://github.com/mobile-next/mobile-mcp/wiki/Getting-Started-with-iOS-Real-Device), [mobilecli #487](https://github.com/mobile-next/mobilecli/issues/487) **[V]** |
| **mirroir-mcp** and **iphone-mirror-mcp** | Drives macOS **iPhone Mirroring**: **CGEvent** clicks and keys, screen capture, Apple Vision OCR | macOS 15+, Screen Recording and Accessibility permissions on the Mac. Can't act on a locked phone. Single pointer | [jfarcand/mirroir-mcp](https://github.com/jfarcand/mirroir-mcp) **[V]** |
| **BrowserStack, AWS Device Farm** | Racks of real iPhones driven by XCUITest/Appium from Macs. AWS offers 60-minute remote-access sessions and private devices from about $200 per device per month. BrowserStack "Device Tunnel" exposes remote devices locally over usbmuxd (Enterprise plan) | Their hardware, not the user's phone | [AWS remote access](https://docs.aws.amazon.com/devicefarm/latest/developerguide/remote-access.md), [BrowserStack Device Tunnel](https://www.browserstack.com/docs/custom-device-lab/device-tunnel) **[V]** |

**Takeaway [I]:** "iOS support" from every competitor means *a computer paired with the iPhone,
running Apple's developer tooling*. None of them has an App Store-only path. Mobilerun's marketing
says "supports iOS", but its own documentation requires a Mac and a USB cable.

---

## 3. Implementation routes

### 3.1 XCUITest / WebDriverAgent from a host (what `ios.py` does today)

**What `ios.py` does [V]** (read from the code). It is an HTTP client for WebDriverAgent on
`localhost:8100`, usually forwarded over USB with `pymobiledevice3 usbmux forward`:
- **Screen:** `GET /screenshot` returns a PNG, scaled down to a 1280 px JPEG.
- **Element tree:** `GET /source?format=xml` returns the XCUITest snapshot, parsed into
  `UIElement`s. Points are converted to pixels; at most 150 elements are kept, and invisible or
  unlabeled "Other" nodes are dropped.
- **Actions:** taps, long presses and swipes are W3C `/actions` pointer sequences. Typing uses
  `/wda/keys`, and **password fields are refused** after checking `/element/active`. Home, volume
  and unlock use `/wda/*`. `BACK` raises an error that tells the agent to use the on-screen button.
- **App launch:** by bundle id. App listing needs `pymobiledevice3`'s InstallationProxy over USB.
- **Not handled:** no `wait_idle`, no streaming, and no recovery when WDA dies.

**How it works [V].** WebDriverAgent is an XCTest UI-test bundle. XCUITest can drive any app,
including SpringBoard, and read its accessibility snapshot, which is why every competitor uses it.
It must be **development-signed** and runs only on a device with **Developer Mode** enabled
([Apple](https://developer.apple.com/documentation/xcode/enabling-developer-mode-on-a-device)).

| Aspect | Assessment |
|---|---|
| Read screen | Screenshot: yes. Element tree: yes, but `/source` can be very slow on deep hierarchies, since XCTest snapshots time out and React Native and WebView screens are the worst cases **[V]** ([WDA PR #273](https://github.com/appium/WebDriverAgent/pull/273), [Appium discuss](https://discuss.appium.io/t/appium-ios-testing-getpagesource-and-requesting-snapshot-of-accessibility-hierarchy-get-stuck-and-fail-to-retrieve-the-app-source/32487)) |
| Act | Tap, swipe, type and launch in any app. Some system sheets, Apple Pay and password prompts are opaque **[V]** (`docs/ios.md`) |
| Signing | **Free Apple ID: profiles expire after 7 days**, with a limit of 3 apps per device **[V]** ([Apple forums](https://developer.apple.com/forums/thread/724896)). **Paid team ($99 a year): 1 year.** Mobilerun accepts a paid team's App Store Connect API key for automated re-signing **[V]** |
| Without a Mac? | Since iOS 17, services sit behind **CoreDevice/RemoteXPC tunnels**. `pymobiledevice3` and `appium-ios-remotexpc` implement them on macOS, Linux and Windows, with **one-time USB pairing, then Wi-Fi** **[V]** ([pymobiledevice3 tunnels](https://github.com/doronz88/pymobiledevice3/blob/master/docs/guides/ios17-tunnels.md), [TestMu/Appium RemoteXPC](https://www.testmuai.com/blog/remotexpc-wireless-ios-automation-appium/)). *Building and signing* WDA still needs Xcode or a signing service, but **running** it does not **[I]** |
| Phone runs WDA alone? | One community report: on iOS 17, a WDA re-packaged without the XC frameworks and launched with `devicectl … process launch` **"ran even after I disconnected iPhone from MacBook"** ([Appium discuss, May 2024](https://discuss.appium.io/t/unable-to-launch-ios-webdriver-agent-manually-on-ios-17-x-devices/41492)). The normal behavior since iOS 17 is that it stops without the host **[V, single report]**. If it holds up, an on-device runner could open an **outbound WebSocket** to the OpenMob server like the Android app does, with no Mac in the loop at runtime **[T]**: how long it survives, backgrounding, reboots, and whether XCUITest calls still work without testmanagerd's session |
| Distribution | Development or ad hoc signing only. A UI-test runner can't ship through the App Store or TestFlight **[I]** (it is a test bundle using XCTest). The Enterprise Program is for internal use only ([ADEP](https://developer.apple.com/programs/enterprise/)) **[V]** |
| Who uses it | Appium, mobile-mcp, Mobilerun (iOS below 27), BrowserStack, Sauce Labs, AWS Device Farm **[V]** |
| Reliability | "Particularly flaky" on real devices. The usual fix is restarting WDA or the device **[V]** ([Appium XCUITest driver README](https://github.com/appium/appium-xcuitest-driver/blob/b87df4e2ce188c75d15985b42b19362fe1f8f79a/README.md)) |

### 3.2 iOS 27 CoreDevice remote input and screen ("native control"): the new option

- **Device Hub [V].** Xcode 27 ships **Device Hub**, where "you can see and control physical devices"
  and "click, drag, scroll or use natural trackpad gestures… the same whether it's a device or
  simulator". "Capture keyboard routes your Mac's keystrokes straight to the device."
  ([WWDC26 session 260](https://developer.apple.com/videos/play/wwdc2026/260/);
  [docs](https://developer.apple.com/documentation/xcode/interacting-with-your-app-in-device-hub)).
- **iOS 27 only [V, developer reports].** "Xcode 27's Device Hub doesn't support remote screen sharing
  with iOSes earlier than 27" (Steve Troughton-Smith, via
  [Michael Tsai](https://mjtsai.com/blog/2026/06/25/xcode-27s-device-hub/)).
- **Services underneath [V, open source].** An iOS 27 device advertises these CoreDevice features:
  `remote.hid.digitizer`, `hid.keyboard`, `hid.button`, `hid.scroll`, `universalhid` and
  `viewdevicescreen`. They sit behind **Settings › Developer › UI Automation**, a toggle described as
  "Paired Mac computers can remotely view and control this iPhone"
  ([getsentry/MobileBuildMCP #519](https://github.com/getsentry/MobileBuildMCP/issues/519)).
- **Usable without Xcode [V].** `pymobiledevice3` implements `universal-hid-service tap/drag/swipe`
  with normalized 0–65535 coordinates, named hardware buttons (home, lock, volume, Siri), and a
  **live HEVC screen stream** over the RSD tunnel (`display serve-web`, `serve-vnc`). The tunnel
  runs in userspace with **no root** on macOS, Linux and Windows for iOS 17.4+
  ([cli-recipes](https://github.com/doronz88/pymobiledevice3/blob/master/docs/guides/cli-recipes.md)).
- **No CLI from Apple [V, report].** Apple's `devicectl` has no input subcommand
  ([#519](https://github.com/getsentry/MobileBuildMCP/issues/519)).
- **Shipping in a product [V].** Mobilerun defaults iOS 27 phones to "native control, which does not
  need WDA signing" ([docs](https://docs.mobilerun.ai/guides/connect-iphone.md)).

| Aspect | Assessment |
|---|---|
| Read screen | **Video and screenshots only. No element tree** ("no device equivalent of `snapshot_ui`") **[V]**. iOS refuses to mirror while a foreground app uses the camera or microphone **[V]** (cli-recipes) |
| Act | Real HID touch, keyboard and buttons, so anything a finger can do in any app, including Home and Lock **[V]**. Multi-touch is unclear **[T]** |
| Friction | Developer Mode (a restart), a one-time USB pairing, and the UI Automation toggle. **No Apple ID signing and no 7-day expiry** **[V]**. After pairing, Wi-Fi may be enough **[T]** |
| Host | Any computer on the same network, Mac or Linux **[V]**. Over the internet, through a VPN to the host's network, is untested **[T]** |
| Risk | Uses **private, undocumented** CoreDevice protocols reverse-engineered by `pymobiledevice3`. Apple can change them in any update **[I]**. The only first-party client is a GUI **[V]** |
| Who uses it | Xcode Device Hub (Apple), Mobilerun native mode, the `pymobiledevice3` CLI **[V]** |

**Inference [I]:** this is the route most like our Android app in speed and coverage: continuous
video, real touch, any app. Its weakness is that there's no element tree, so we'd fall back to the
vision step. A **hybrid** solves that: native HID and video for acting and watching, and WDA
`/source` only when the fast path needs elements, on phones where WDA is installed.

### 3.3 App Intents, Shortcuts, Siri and Apple Intelligence

- **App Intents expose an app's own actions [V].** The WWDC26 App Intents session covers dialogs,
  entity indexing, onscreen awareness annotations and confirmations, all for *an app's own* actions
  and entities ([WWDC26 session 343](https://developer.apple.com/videos/play/wwdc2026/343/)).
  "Siri AI lets users get things done across apps" means Siri calling intents that apps expose
  ([Apple Newsroom](https://www.apple.com/newsroom/2026/06/apple-introduces-siri-ai-a-profoundly-more-capable-and-personal-assistant/)).
  Siri AI isn't available initially in the EU or China.
- **Shortcuts can't tap inside other apps [V].** "Receive What's On Screen" passes content only from
  supported apps ([Shortcuts User Guide](https://support.apple.com/guide/shortcuts/apd163eb9f95)).
- **"Use Model" (iOS 26) [V].** It runs a prompt on the on-device model, Private Cloud Compute or
  ChatGPT inside a shortcut ([MacStories](https://www.macstories.net/notes/i-have-many-questions-about-apples-updated-foundation-models-and-the-great-use-model-action-in-shortcuts/)).
  It reasons over data. It doesn't operate UI.
- **MCP [V].** Code for MCP in App Intents was found in **iOS 26.1 beta 1**, described as "very
  incipient", with apps *exposing* actions ([9to5Mac](https://9to5mac.com/2025/09/22/macos-tahoe-26-1-beta-1-mcp-integration/)).
  At WWDC26, the only MCP Apple announced was in **Xcode 27** ("bring in the tools they use every day
  through the Model Context Protocol") ([Apple Newsroom](https://www.apple.com/newsroom/2026/06/apple-aids-app-development-with-new-intelligence-frameworks-and-advanced-tools/)).
  I found **no primary source for a system-level MCP for iOS apps** as of October 2026.
- **"Extensions" for third-party assistants (Claude, Gemini) in Siri.** Secondary outlets report this
  ([Business Standard](https://www.business-standard.com/technology/tech-news/apple-ios-27-open-siri-gemini-claude-more-extension-how-will-it-work-126032700337_1.html)),
  but **Apple's two WWDC26 press releases I read don't mention it** **[unverified]**. Even if it
  ships, it routes *conversations* to a model. It doesn't give that model control of the UI. **[I]**

**Verdict:** App Intents can't drive other apps. They are worth a small integration *later*:
expose OpenMob's own intents ("Run my OpenMob skill", "Approve pending action") so Siri and
Shortcuts can start tasks on *another* phone or approve them. **[I]**

### 3.4 Accessibility hardware routes

| Mechanism | What it does | Usable by a program? |
|---|---|---|
| **AssistiveTouch + Bluetooth mouse** | An on-screen pointer that clicks anything **[V]** ([Apple 111775](https://support.apple.com/en-ug/111775)) | An ESP32 BLE HID mouse works on iOS 26.3 with AssistiveTouch on **[V]** ([HijelHID_BLEMouse](https://github.com/HijelHub/HijelHID_BLEMouse)). Movement is **relative with acceleration**, so taps need "slam to a corner, then move" calibration **[I]**. Drift and accuracy are untested **[T]** |
| **Full Keyboard Access** | Tab and Space activate the focused item; Fn-H goes Home; Fn-C opens Control Center **[V]** ([Apple](https://support.apple.com/en-bn/guide/iphone/ipha4375873f/ios)) | Text entry and element-by-element navigation, but it's blind without a view of the screen **[I]** |
| **Switch Control "Use Other Devices"** | One Apple device controls another with the same Apple Account on the same Wi-Fi **[V]** ([Apple 118667](https://support.apple.com/118667)) | Scanning interface only. No coordinates, no API **[I]** |
| **Voice Control** | "Show grid" and "tap [label]" **[V]** ([Apple](https://support.apple.com/en-ge/HT210417)) | It could be driven by synthesized speech, but that is slow and fragile. Not viable **[I]** |

**The capture side** for a dongle setup:
- an **App Store app with a ReplayKit broadcast extension**, which captures system-wide with a red
  indicator, a 50 MB cap and a user-initiated start, and streams frames to the server **[V]**;
- or an HDMI adapter and capture card **[I]**.

**Verdict [I]:** a pocket BLE dongle plus a ReplayKit app is *technically* a no-Mac route. It is
the only one that works without Developer Mode and passes App Review, since the app only shares the
screen. But:
- it needs hardware we'd ship;
- relative pointing is imprecise;
- typing goes through a separate keyboard HID profile;
- the user restarts the broadcast after every reboot or extension kill;
- it can't read an element tree.

It is a research spike, not a product. Apple could also treat an app that markets "AI controls
your phone via a dongle" as violating the spirit of 2.5.1. **[I]**

### 3.5 ReplayKit broadcast upload extension (on its own)

- **Capabilities [V]:** system-wide capture that keeps running while the host app is in the
  background, a 50 MB memory cap, and a required user tap (system picker or Control Center).
  Guideline 2.5.14 requires consent and a visible indicator.
- **Input:** none. It pairs only with an external input device (§3.4) **[I]**.
- **Use for OpenMob [I]:** a *watch-only* mode (agent advice, "screen reader" tasks), or the capture
  half of the dongle route.

### 3.6 iPhone Mirroring (macOS 15+) driven by a Mac app

- **Requirements [V]:** Apple silicon or a T2 Mac, iOS 18+, the same Apple Account with two-factor
  authentication, phone **locked and within about 10 m**, mirroring "automatically pauses… after
  periods of inactivity", **not available in the EU**, no camera or microphone, so no Face ID
  ([Apple 120421](https://support.apple.com/120421)).
- **Automation [V]:** mirroir-mcp drives it with **CGEvent** clicks and keys, ScreenCaptureKit and
  Vision OCR.
- **For consumers [I]:**
  - no Developer Mode, no signing, nothing installed on the phone;
  - but the phone can't be used while the agent works (mirroring needs it locked; unlocking ends
    the session);
  - pixels only, single pointer, idle timeouts;
  - it fights the user for the Mac's mouse and focus;
  - nothing guarantees Apple won't block synthetic events into the mirroring window.

  Managed fleets can disable mirroring, reportedly with an MDM restriction **[T: verify the key]**.

### 3.7 MDM and supervised devices

- **What MDM can do [V]:** supervision through Apple Business Manager enables restrictions,
  kiosk-style **Single App Mode** and **Autonomous Single App Mode**, where an MDM-allowlisted app
  locks itself in with `UIAccessibility.requestGuidedAccessSession`
  ([Apple](https://developer.apple.com/documentation/devicemanagement/autonomoussingleappmode)).
- **What it can't do [V]:** MDM manages configuration. It has no screen-view or input command, and
  no Apple Remote Desktop exists for iOS (TeamViewer above, [Esper](https://www.esper.io/blog/ios-supervised-mode-vs-unsupervised-mode-whats-the-difference)).
- **What enterprises gain [I]:**
  - deploying the companion app;
  - locking down which apps exist on agent phones;
  - Autonomous Single App Mode for a dedicated "agent" app;
  - restricting iPhone Mirroring;
  - controlled iOS versions, which matters if we rely on iOS 27 private protocols.
- **Unknown [T]:** whether Developer Mode and the UI Automation toggle can be enabled or kept across
  a supervised fleet without someone touching each phone.

### 3.8 Hosted real iPhones (BrowserStack, Sauce Labs, AWS Device Farm)

- **How they work [V]:** Mac-hosted racks running XCUITest/Appium. AWS charges $0.17 per device
  minute, has a 60-minute remote session cap, and private devices from about $200 a month.
  BrowserStack's Device Tunnel needs the Enterprise plan.
- **For OpenMob [I]:** it's an "Appium endpoint" backend for **developers testing their own apps**,
  not "your own phone". A user's banking, messaging and game accounts don't live there. A
  `CloudAppiumDriver` that talks WebDriver to a remote URL is cheap, since `ios.py` already speaks
  WDA. Partnership is possible, but nobody offers an agent-native hosted iPhone. Mobilerun offers
  hosted *Android* only.

### 3.9 Other routes: not viable

| Route | Why not |
|---|---|
| **EU alternative marketplaces and web distribution** | Apps still go through **Notarization**, which bans reading outside the container, downloading code and "direct[ing] users to lower the security" **[V]** ([Apple: apps in the EU](https://developer.apple.com/support/apps-in-the-eu/)). Private APIs still don't work in the sandbox. The DMA **interoperability request** process exists **[V]**, and a request for "accessibility-service-equivalent" access is a long-shot, multi-year option **[I]** |
| **Private APIs in an App Store app** | Rejected under 2.5.1, and sandboxed anyway **[V]** |
| **Jailbreak** | No public jailbreaks for current iOS, broken by every update, voids security. The opposite of an enterprise offering **[I]** |
| **Enterprise-signed (ADEP) "control" app** | Signing doesn't grant entitlements to control other apps, and ADEP is internal-only **[V/I]** |
| **FaceTime SharePlay remote control** | Human-to-human, a contact in a FaceTime call, no API **[V]** |

---

## 4. Screen reading and latency per route

Android baseline (`docs/benchmarks.md`): a fast step takes 0.15–0.6 s, a vision step about 1.6 s,
and task medians are 2.5–11 s. All iOS figures below are **[I] estimates to measure [T]**, not
measurements.

| Route | How the screen is read | Per action, expected | Per-step overhead vs Android |
|---|---|---|---|
| WDA over USB | `/screenshot` PNG (~0.3–1 s) + `/source` (0.3 s on simple screens, **several seconds or timeouts** on deep RN/WebView trees **[V]**) | Tap ~0.1–0.3 s | Fast steps get slower, because the tree is the bottleneck. Cap depth, use `snapshotMaxDepth`, cache |
| WDA over Wi-Fi tunnel | Same, plus a network round trip | +50–200 ms | Worse tail latency |
| iOS 27 native (HID + HEVC) | Continuous video; a "screenshot" is the latest decoded frame (about 0 ms to obtain, ~50–150 ms stale) | Tap tens of ms | **Faster than our Android screenshots** (Android throttles to about 3 per second); vision-only, so every step is a vision step unless WDA is also present |
| Hybrid (native + WDA tree on demand) | Frames from video, tree only for the fast path | Tap tens of ms | Closest to Android: fast path where the tree is cheap |
| iPhone Mirroring | ScreenCaptureKit on the Mac window + OCR | CGEvent ~10 ms + mirroring lag | Vision and OCR only; tasks ~1.2–1.5× Android **[I]** |
| BLE dongle + ReplayKit | Broadcast frames uploaded by the phone | Pointer travel 100–400 ms | Slowest; calibration errors |
| Hosted farm | Appium over WAN | +100–300 ms per call | Fine for testing, not for personal use |

---

## 5. Security mapping

Two pillars of OpenMob's model are owner approvals signed by the screen lock, and the gatekeeper:
on-device enforcement *below* the server (`docs/design/phone-gatekeeper.md`, D1).

**Approvals map cleanly on every route, through a separate App Store companion app. [I]**
- The app creates a **Secure Enclave key** with an access control flag that requires biometry or
  the passcode (`.userPresence`).
- It receives push notifications ("On iPhone 16: tap 'Buy now'?") and signs
  `<approval id>|<task id>|…`. This is the same protocol as Android's Keystore key.
- It's ordinary, reviewable App Store functionality, with no control claims.
- This keeps "only the screen lock can approve" on iOS. **[I]**

**Enforcement can't live on the iPhone itself. It moves to the bridge.**

| Route | Where enforcement can live | Gap |
|---|---|---|
| WDA runner (our own fork) | **Inside the runner on the phone**: hit-test the element under the tap, refuse SecureTextField typing (already done in `ios.py`, but host-side), block bundle ids. This is the closest analog to D1 | The runner is development-signed by the *user's* team (or ours, ad hoc). Integrity relies on the signer. A compromised host can re-sign a WDA without the checks **[I]** |
| iOS 27 native HID | **Host bridge only.** The phone obeys raw HID from any paired host with UI Automation on | The host is the trust boundary: **a compromised Mac or Linux box equals full control of the phone**, including typing into password fields (with no tree, we can't even detect them) **[I]**. Pairing records on the host are high-value secrets |
| iPhone Mirroring | Mac app | Same as above. Plus, mirroring auth can be set to "authenticate automatically" **[V]**, removing a human check |
| BLE dongle | Dongle firmware plus the app | Firmware sees no screen. Policy has to sit in the bridge or app that sees ReplayKit frames |
| Hosted farm | Provider plus OpenMob server | Not the owner's data, so a lower stake |

**What the gatekeeper's goals become on iOS. [I]**
- **G1, refuse even if the server is compromised:** holds only if the *bridge* is on hardware the
  owner controls and the bridge enforces policy locally. Ship the gatekeeper logic in the bridge,
  with the policy signed by the release key, not served by the server.
- **G2, phone-worded prompts:** the companion app words prompts from the bridge's view, signed by
  the bridge. That is weaker than Android, where the prompt comes from the phone that hit-tests.
- **Also needed:**
  - TLS pinning between the bridge and the server;
  - the bridge holds an outbound WebSocket, the same as `openmob bridge` today;
  - "one live connection per phone" is unchanged.

---

## 6. Recommendation

### 6.1 Ranked routes per audience

**Developers (want it today, tolerate setup)**
1. **Hybrid bridge on their Mac.** iOS 27 native HID and video, plus WDA for element trees where
   installed. This is today's `openmob bridge --ios`, grown up.
2. WDA-only bridge, for iOS 17–26 phones (what exists now).
3. A hosted farm Appium endpoint, for testing their own apps.

**Enterprises (fleets, compliance)**
1. **Supervised iPhones (ABM + MDM) on a Linux or Mac host per bench or desk.** iOS 27 native plus a
   WDA signed with *their own* team. Gatekeeper policy in the bridge, approvals through the companion
   app, MDM locks down apps and blocks mirroring.
2. A hosted private-device farm (AWS or BrowserStack), for workflows on company accounts that don't
   need "my phone".
3. App Intents in *their* apps, where they own the app: the structured path, not UI control.

**Consumers (least friction)**
1. **Nothing good today. Say so.** iPhone users can control their phone via OpenMob *only with a
   computer*.
2. Mac owners outside the EU: the **iPhone Mirroring** backend in the OpenMob Mac app. No Developer
   Mode, but the phone must stay locked and nearby. Ship it as an experiment.
3. Mac owners willing to flip Developer Mode: the same hybrid bridge, with guided setup.
4. Watch until 2027: a system MCP / App Intents agent API, and a DMA interoperability request.

### 6.2 Phased plan

| Phase | Build | Effort (engineer-weeks) | Exit criterion |
|---|---|---|---|
| 0. Device spike | On an iOS 27 iPhone, measure: native tap and video latency through `pymobiledevice3`, Wi-Fi after USB pairing, locked-phone behavior, multi-touch, typing of non-ASCII text, WDA coexistence, survival across sleep and reboot. Also test the standalone `devicectl`-launched WDA on iOS 26 and 27 | 1–2 | A latency table like §4, with measurements |
| 1. `NativeIOSDriver` | New `Device` implementation: HID touch, keys and buttons; frames from HEVC (decode on the host); `wait_idle` from frame diffing; optional WDA for `ui_elements`; passcode-field heuristics. Keep `ios.py` as the WDA backend | 3–4 | Bench tasks within 1.5× of the Android medians |
| 2. OpenMob Bridge for Mac | Menu-bar app, signed with Developer ID and notarized, **outside the Mac App Store** (sandbox and 2.4.5 conflict **[I]**). Guided setup: Developer Mode, pairing, the UI Automation toggle, optional WDA signing through the user's team or an App Store Connect key. Auto-reconnect, wake handling, bridge-side gatekeeper, outbound WSS | 5–7 | A new user goes from zero to a first task in under 15 minutes |
| 3. iOS companion app (App Store) | Pairing, push approvals with a Secure Enclave signature, "who's using my phone", kill switch. Optional ReplayKit "share screen" for watch-only | 3–4 | Approval latency like Android; passes App Review |
| 4. Enterprise host | Headless Linux bridge for N phones (pymobiledevice3 runs there **[V]**), fleet docs for ABM/MDM, per-device policy | 3–4 | 10 phones on one box, 24 h soak |
| 5. Experiments | iPhone Mirroring backend in the Mac app (2 weeks); BLE dongle + ReplayKit spike (2 weeks, research only); `CloudAppiumDriver` for hosted farms (1 week) | 5 | Go/no-go memo each |

**Total to a credible developer and enterprise iOS offering: about 15–21 engineer-weeks** (phases
0–4), with phase 0 as a hard gate.

### 6.3 What would make each route fail

| Route | Failure triggers |
|---|---|
| iOS 27 native HID | Apple changes or authenticates the private CoreDevice protocol so only Xcode can use it; the UI Automation toggle becomes per-session or time-limited; Developer Mode becomes harder to keep on (for example, MDM blocks it) **[I]** |
| WDA | XCTest changes on each iOS release (as with iOS 17); free-signing limits tighten; testmanagerd ties the runner more strictly to the host; `/source` timeouts on modern apps |
| Mac bridge | Users won't enable Developer Mode (a warning prompt plus restart); Macs sleep; laptops leave the phone's network; security teams ban Developer Mode on corporate phones |
| iPhone Mirroring | Apple filters synthetic events into the mirroring window; EU unavailability; the phone is unusable during tasks; inactivity pauses |
| Dongle + ReplayKit | App Review rejects it under 2.5.1/2.5.14; pointer drift; broadcasts killed at 50 MB; hardware logistics |
| Hosted farms | Not the user's accounts; cost per minute; providers' terms on AI agents |
| App Intents / MCP | Only reaches apps that adopt it; Apple keeps it Siri-only |

### 6.4 Impossible today

All of these are **[V]** for the API facts above, and **[I]** for "impossible", meaning no
sanctioned path exists:
- An App Store app that reads or taps other apps.
- A phone-only setup with no computer, except the unproven standalone-WDA hack and the dongle route.
- MDM remote control.
- Approving or operating Face ID and Apple Pay sheets through any route.
- Background, always-on control without a host process.

---

## 7. Unknowns to test on a device (phase 0 checklist)

1. Does iOS 27 native HID and video work **over Wi-Fi only** after one USB pairing? How long does
   pairing last, and does it survive a reboot?
2. Is the "UI Automation" toggle present on **iOS 26**, or only on 27? Does HID work while the
   phone is **locked**? Could HID typing **enter the passcode** (a security concern for us and an
   unlock path)?
3. Multi-touch (pinch) and long-press fidelity through the universal HID service.
4. Real latency: tap to first changed frame, HEVC decode cost on the host, and how frame staleness
   affects the agent.
5. Can WDA and native control run at the same time? Is the cost of `/source` acceptable when the
   phone is driven natively?
6. The standalone-WDA trick (`devicectl` launch without XC frameworks): does it survive host
   disconnect, app switches and sleep on iOS 26 and 27? Could the runner hold an outbound WebSocket
   to our server?
7. The MDM restriction key for iPhone Mirroring, and whether supervised devices can pre-enable
   Developer Mode.
8. iPhone Mirroring: are synthetic CGEvents honored reliably, and how long until the inactivity
   pause?
9. BLE HID with AssistiveTouch: positioning error after 100 taps, and recovery.

---

## Sources (primary first)

- Apple: [App Review Guidelines](https://developer.apple.com/app-store/review/guidelines/) (2.4.5, 2.5.1, 2.5.2, 2.5.4, 2.5.14, 4.2.7, 5.1.1)
- Apple: [Enabling Developer Mode](https://developer.apple.com/documentation/xcode/enabling-developer-mode-on-a-device)
- Apple: [Platform Security: runtime process security](https://support.apple.com/guide/security/security-of-runtime-process-sec15bfe098e/web)
- Apple: [WWDC26 "Get the most out of Device Hub"](https://developer.apple.com/videos/play/wwdc2026/260/), [Interacting with your app in Device Hub](https://developer.apple.com/documentation/xcode/interacting-with-your-app-in-device-hub)
- Apple: [WWDC26 "Explore advanced App Intents features"](https://developer.apple.com/videos/play/wwdc2026/343/)
- Apple Newsroom (June 2026): [Siri AI](https://www.apple.com/newsroom/2026/06/apple-introduces-siri-ai-a-profoundly-more-capable-and-personal-assistant/), [developer tools](https://www.apple.com/newsroom/2026/06/apple-aids-app-development-with-new-intelligence-frameworks-and-advanced-tools/)
- Apple Support: [iPhone Mirroring](https://support.apple.com/120421), [FaceTime remote control](https://support.apple.com/en-mn/guide/iphone/iph5d70f34a3/ios), [AssistiveTouch pointer](https://support.apple.com/en-ug/111775), [Full Keyboard Access](https://support.apple.com/en-bn/guide/iphone/ipha4375873f/ios), [Switch Control other devices](https://support.apple.com/118667), [Voice Control](https://support.apple.com/en-ge/HT210417), [Shortcuts on-screen content](https://support.apple.com/guide/shortcuts/apd163eb9f95)
- Apple: [Autonomous Single App Mode](https://developer.apple.com/documentation/devicemanagement/autonomoussingleappmode), [Apps in the EU / Notarization / interoperability](https://developer.apple.com/support/apps-in-the-eu/), [Enterprise Program](https://developer.apple.com/programs/enterprise/)
- Apple Developer Forums: [free provisioning limits](https://developer.apple.com/forums/thread/724896), [broadcast extension memory](https://developer.apple.com/forums/thread/651367), [remote control question 795216](https://developer.apple.com/forums/thread/795216)
- Open source and vendors: [pymobiledevice3 CLI recipes](https://github.com/doronz88/pymobiledevice3/blob/master/docs/guides/cli-recipes.md), [pymobiledevice3 iOS 17 tunnels](https://github.com/doronz88/pymobiledevice3/blob/master/docs/guides/ios17-tunnels.md), [MobileBuildMCP #519](https://github.com/getsentry/MobileBuildMCP/issues/519), [droidrun/ios-portal](https://github.com/droidrun/ios-portal), [Mobilerun: connect an iPhone](https://docs.mobilerun.ai/guides/connect-iphone.md), [mobilerun-ios overview](https://docs.mobilerun.ai/mobilerun-ios/overview), [mobile-mcp iOS wiki](https://github.com/mobile-next/mobile-mcp/wiki/Getting-Started-with-iOS-Real-Device), [mobilecli #487](https://github.com/mobile-next/mobilecli/issues/487), [mirroir-mcp](https://github.com/jfarcand/mirroir-mcp), [HijelHID BLE mouse](https://github.com/HijelHub/HijelHID_BLEMouse), [Appium discuss: WDA on iOS 17](https://discuss.appium.io/t/unable-to-launch-ios-webdriver-agent-manually-on-ios-17-x-devices/41492), [WDA PR #273](https://github.com/appium/WebDriverAgent/pull/273), [AWS Device Farm remote access](https://docs.aws.amazon.com/devicefarm/latest/developerguide/remote-access.md), [BrowserStack Device Tunnel](https://www.browserstack.com/docs/custom-device-lab/device-tunnel), [TeamViewer iOS](https://community.teamviewer.com/English/discussion/58790/need-clarification-on-ios-vs-android-support-for-screen-sharing)
- Press (secondary): [9to5Mac on MCP in iOS 26.1 beta](https://9to5mac.com/2025/09/22/macos-tahoe-26-1-beta-1-mcp-integration/), [Michael Tsai on Device Hub](https://mjtsai.com/blog/2026/06/25/xcode-27s-device-hub/), [MacStories on Use Model](https://www.macstories.net/notes/i-have-many-questions-about-apples-updated-foundation-models-and-the-great-use-model-action-in-shortcuts/), [Business Standard on Siri Extensions](https://www.business-standard.com/technology/tech-news/apple-ios-27-open-siri-gemini-claude-more-extension-how-will-it-work-126032700337_1.html)
