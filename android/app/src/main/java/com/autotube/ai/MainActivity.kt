package com.autotube.ai

import android.Manifest
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.LaunchedEffect
import com.autotube.ai.ui.AutoTubeApp as AutoTubeAppUi
import com.autotube.ai.ui.theme.AutoTubeTheme

class MainActivity : ComponentActivity() {

    private val notificationPermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            AutoTubeTheme {
                LaunchedEffect(Unit) {
                    // Approval notifications are the point of APPROVAL mode, so
                    // ask once on first launch (Android 13+ requires runtime grant).
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                        notificationPermission.launch(
                            Manifest.permission.POST_NOTIFICATIONS
                        )
                    }
                }
                AutoTubeAppUi()
            }
        }
    }
}
