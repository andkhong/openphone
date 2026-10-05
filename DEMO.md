# OpenMob live demo runbook

## 10 minutes before

1. **Phone:** plug in with USB, unlock it, and set the screen to stay on:
   ```sh
   adb shell svc power stayon true
   ```
   Close any game or app you'll open during the demo, so it starts from a clean state.
2. **Start OpenMob** in a terminal:
   ```sh
   cd ~/Desktop/openmob && source .venv/bin/activate
   openmob doctor             # every line should be ✓
   openmob run --public       # prints Dashboard (localhost) and Public (HTTPS) links
   ```
3. **Open the dashboard** in Chrome using the `localhost` link. Check that the live screen is moving.
4. **Optional:** open the **Public (HTTPS)** link on a second device (tablet or laptop) to show it's hosted.

## Script (about 5 minutes)

| # | Say / do | What the audience sees |
|---|---|---|
| 1 | In the phone app's **Ask your agent**, tap 🎙 and say *"Open Where Winds Meet and open the menu."* (or type it on the dashboard) | The game opens. Jev classifies each screen (loading, pop-up, menu). Claude taps through to the menu. |
| 2 | *"Close the menu."* | A follow-up that uses memory of the earlier request. |
| 3 | Click the example chip **Open YouTube and search for lofi music**. | A different app, done in 2 steps with a deep link. |
| 4 | Type *"Open the Play Store and search for Spotify, don't install anything."* | Typing, searching, and staying within instructions. |
| 5 | **Only if rehearsed:** *"Buy the first item in the shop"* (inside the game). Safer alternative: *"Open Messages and send hi to Mom"*. | The safety check: a **Yes/No** card appears before the Buy or Send tap. Click **No**. The check combines a keyword list with Jev's risk score; it isn't a guarantee, so never demo this with real money you can't afford to lose. |
| 6 | Click **Stop** during any task. | Instant cancel. |

**Talking points:**
- The phone needs nothing installed. OpenMob works with any app on any Android phone.
- Claude does the seeing and acting. Jev (TypeSafe) gives fast, calibrated screen classification and risk checks in tens to hundreds of milliseconds. OCR handles games that draw their own UI.
- App names come from the phone itself, so "Where Winds Meet" works even though the package is `com.netease.yysls`.

## If something goes wrong

| Symptom | Fix |
|---|---|
| Live screen frozen, or "screen unavailable" | Check the USB cable, then run `adb devices`. Click ↻ in the dashboard. |
| Agent says the phone is locked | Unlock the phone. OpenMob will never type your PIN. |
| Phone app's 🎙 does nothing | Check the phone's speech recognition is available (Google app), or type the request. |
| A task wanders | Press **Stop** and rephrase more specifically. |
| Public link fails | Use the localhost dashboard. The tunnel is only needed for other devices. |

**Known limit:** over this home Wi-Fi the phone only manages about 100 KB/s, so a screenshot takes up to 28 seconds. Use USB for the demo; `openmob connect` exists for wireless control on better networks.

**Roadmap answer for "is this a hosted service?"** Today the server runs on a Mac and drives phones over adb, reachable from anywhere through an HTTPS link. Next comes an OpenMob Android companion app (accessibility service plus an outbound connection to the server), so phones connect to the cloud with no computer or developer mode.
