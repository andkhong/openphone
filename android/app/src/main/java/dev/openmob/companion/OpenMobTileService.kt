package dev.openmob.companion

import android.app.PendingIntent
import android.content.Intent
import android.os.Build
import android.service.quicksettings.Tile
import android.service.quicksettings.TileService

/**
 * Quick Settings tile ("OpenMob" in the pull-down panel): a big, easy target. With Accessibility off
 * it opens the setup (explanation, then Android's Accessibility settings); otherwise it connects or
 * opens the app.
 */
class OpenMobTileService : TileService() {
    override fun onStartListening() {
        val tile = qsTile ?: return
        val a11yOn = OpenMobAccessibilityService.instance != null
        tile.label = getString(R.string.app_name)
        tile.state = when {
            AppState.connected -> Tile.STATE_ACTIVE
            else -> Tile.STATE_INACTIVE
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            tile.subtitle = getString(
                when {
                    !a11yOn -> R.string.tile_turn_on_accessibility
                    AppState.connected -> R.string.tile_connected
                    else -> R.string.tile_tap_to_connect
                }
            )
        }
        tile.updateTile()
    }

    override fun onClick() {
        val action = when {
            OpenMobAccessibilityService.instance == null -> Shortcuts.OPEN_ACCESSIBILITY
            !AppState.connected -> Shortcuts.CONNECT
            else -> Intent.ACTION_MAIN  // already connected: just open the app
        }
        val intent = Intent(this, MainActivity::class.java).setAction(action).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        val open = {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
                startActivityAndCollapse(PendingIntent.getActivity(this, 0, intent, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT))
            } else {
                @Suppress("DEPRECATION", "StartActivityAndCollapseDeprecated")
                startActivityAndCollapse(intent)
            }
        }
        if (isLocked) unlockAndRun { open() } else open()
    }
}
