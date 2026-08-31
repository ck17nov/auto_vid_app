package com.autotube.ai.ui

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.AddCircle
import androidx.compose.material.icons.filled.Dashboard
import androidx.compose.material.icons.filled.Insights
import androidx.compose.material.icons.filled.Schedule
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Troubleshoot
import androidx.compose.material3.Icon
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.navigation.NavHostController
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import com.autotube.ai.ui.screens.AnalyticsScreen
import com.autotube.ai.ui.screens.ContentScreen
import com.autotube.ai.ui.screens.CreateAutomationScreen
import com.autotube.ai.ui.screens.DashboardScreen
import com.autotube.ai.ui.screens.PreviewScreen
import com.autotube.ai.ui.screens.ResearchScreen
import com.autotube.ai.ui.screens.SchedulerScreen
import com.autotube.ai.ui.screens.SettingsScreen

sealed class Dest(val route: String, val label: String, val icon: ImageVector?) {
    data object Dashboard : Dest("dashboard", "Dashboard", Icons.Filled.Dashboard)
    data object Create : Dest("create", "Create", Icons.Filled.AddCircle)
    data object Research : Dest("research", "Research", Icons.Filled.Troubleshoot)
    data object Scheduler : Dest("scheduler", "Schedule", Icons.Filled.Schedule)
    data object Analytics : Dest("analytics", "Analytics", Icons.Filled.Insights)
    data object Settings : Dest("settings", "Settings", Icons.Filled.Settings)

    // Detail destinations are not in the bottom bar.
    data object Content : Dest("content", "Content", null)
    data object Preview : Dest("preview/{jobId}", "Preview", null) {
        fun path(jobId: String) = "preview/$jobId"
    }
}

private val bottomBar = listOf(
    Dest.Dashboard, Dest.Create, Dest.Research, Dest.Scheduler, Dest.Analytics,
)

@Composable
fun AutoTubeApp(navController: NavHostController = rememberNavController()) {
    val backStack by navController.currentBackStackEntryAsState()
    val currentRoute = backStack?.destination?.route

    Scaffold(
        bottomBar = {
            // Hide the bar on full-screen detail views.
            if (currentRoute?.startsWith("preview/") != true) {
                NavigationBar {
                    bottomBar.forEach { dest ->
                        NavigationBarItem(
                            selected = currentRoute == dest.route,
                            onClick = {
                                if (currentRoute != dest.route) {
                                    navController.navigate(dest.route) {
                                        popUpTo(Dest.Dashboard.route) {
                                            saveState = true
                                        }
                                        launchSingleTop = true
                                        restoreState = true
                                    }
                                }
                            },
                            icon = {
                                dest.icon?.let {
                                    Icon(it, contentDescription = dest.label)
                                }
                            },
                            label = { Text(dest.label) },
                        )
                    }
                }
            }
        }
    ) { inner ->
        Box(Modifier.fillMaxSize().padding(inner)) {
            NavHost(
                navController = navController,
                startDestination = Dest.Dashboard.route,
            ) {
                composable(Dest.Dashboard.route) {
                    DashboardScreen(
                        onOpenJob = { navController.navigate(Dest.Preview.path(it)) },
                        onCreate = { navController.navigate(Dest.Create.route) },
                        onOpenSettings = { navController.navigate(Dest.Settings.route) },
                        onOpenContent = { navController.navigate(Dest.Content.route) },
                    )
                }
                composable(Dest.Create.route) {
                    CreateAutomationScreen(
                        onStarted = { navController.navigate(Dest.Dashboard.route) },
                    )
                }
                composable(Dest.Research.route) { ResearchScreen() }
                composable(Dest.Content.route) {
                    ContentScreen(
                        onOpenJob = { navController.navigate(Dest.Preview.path(it)) },
                    )
                }
                composable(Dest.Scheduler.route) {
                    SchedulerScreen(
                        onOpenJob = { navController.navigate(Dest.Preview.path(it)) },
                    )
                }
                composable(Dest.Analytics.route) { AnalyticsScreen() }
                composable(Dest.Settings.route) { SettingsScreen() }
                composable(Dest.Preview.route) { entry ->
                    val jobId = entry.arguments?.getString("jobId").orEmpty()
                    PreviewScreen(
                        jobId = jobId,
                        onBack = { navController.popBackStack() },
                    )
                }
            }
        }
    }
}
