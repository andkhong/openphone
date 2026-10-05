# Connecting an iPhone

iOS doesn't let one app control another, so an iPhone joins OpenMob through a **Mac** running
Apple's UI-testing framework (XCUITest), packaged as **WebDriverAgent**, the same tool Appium uses.
The Mac runs `openmob bridge --ios`, which connects the iPhone to your OpenMob server like any
other phone. The agent, chat, history and accounts work the same as for Android.

```
iPhone ──USB (or Wi-Fi pairing)──▶ Mac: WebDriverAgent + openmob bridge --ios ──▶ OpenMob server
```

## What you need
- A Mac with **Xcode** installed.
- An Apple ID added to Xcode (Xcode → Settings → Accounts). A free Apple ID works, but apps it
  signs expire after **7 days** and must be re-run from Xcode. A paid developer account ($99/year)
  lasts a year.
- The iPhone connected by USB the first time, and **trusted** ("Trust This Computer").

## One-time setup
1. **Turn on Developer Mode on the iPhone:** Settings → Privacy & Security → Developer Mode → On,
   then restart when asked. (The option appears after the phone has been connected to Xcode once.)
2. **Get WebDriverAgent:**
   ```sh
   git clone https://github.com/appium/WebDriverAgent.git
   open WebDriverAgent/WebDriverAgent.xcodeproj
   ```
3. **Sign it:** in Xcode, select the `WebDriverAgentRunner` target → Signing & Capabilities → choose
   your Team. If Xcode says the bundle id is taken, change it to something unique
   (e.g. `com.yourname.WebDriverAgentRunner`).
4. **Run it on the iPhone:** pick your iPhone as the destination, choose the `WebDriverAgentRunner`
   scheme, and run **Product → Test** (⌘U). The first time, trust the developer on the iPhone:
   Settings → General → VPN & Device Management → your Apple ID → Trust. Run the test again.
   Leave it running; it serves on port 8100 on the iPhone.
5. **Forward the port to the Mac** (USB), in a second terminal:
   ```sh
   pip install pymobiledevice3            # also lets OpenMob list your apps by name
   pymobiledevice3 usbmux forward 8100 8100
   ```
   Check it: `curl http://localhost:8100/status` should print JSON.

## Connect
```sh
# Account mode: pairs the iPhone to your account once, then reconnects with its own key.
openmob bridge https://openmob.fly.dev --ios --email you@example.com
# Several iPhones: pass --udid <id> (list them with: pymobiledevice3 usbmux list)
```
The iPhone appears in your dashboard like any other phone.

## Good to know
- **Keep the iPhone unlocked** (Settings → Display & Brightness → Auto-Lock → Never while testing).
  OpenMob can't unlock a passcode.
- **No Back key on iOS.** The agent knows this and uses on-screen Back buttons or edge swipes.
- **Opening apps by name** needs `pymobiledevice3` (installed above). Without it, the agent opens
  apps through Spotlight search.
- **Some screens can't be read or tapped** by WebDriverAgent, such as password prompts, Apple Pay and
  some system alerts.
- If the bridge says WebDriverAgent is unreachable, re-run **Product → Test** in Xcode (it stops when
  the Mac sleeps or after 7 days on a free Apple ID) and restart the port forward.
