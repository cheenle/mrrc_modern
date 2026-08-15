package com.hamradio.ft710android.App

import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import com.hamradio.ft710android.Data.SettingsStore
import com.hamradio.ft710android.UI.LoginScreen
import com.hamradio.ft710android.UI.MainScreen
import com.hamradio.ft710android.UI.SettingsScreen
import com.hamradio.ft710android.ViewModel.MainViewModel

@Composable
fun RootScreen(vm: MainViewModel, settings: SettingsStore) {
    var loggedIn by rememberSaveable { mutableStateOf(false) }
    var showSettings by rememberSaveable { mutableStateOf(false) }
    AppSetup(rxRunning = true, onTxRelease = { vm.onPttRelease() })
    if (!loggedIn) {
        LoginScreen(vm, settings) { loggedIn = true }
    } else if (showSettings) {
        SettingsScreen(vm, settings) { loggedIn = false; showSettings = false }
    } else {
        MainScreen(vm)
    }
}
