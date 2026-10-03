package com.hamradio.ft710android.App

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import com.hamradio.ft710android.Data.SettingsStore
import com.hamradio.ft710android.UI.AppTheme

class MainActivity : ComponentActivity() {
    private val holder: MainViewModelHolder by viewModels()
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val settings = SettingsStore(applicationContext)
        setContent { AppTheme { RootScreen(vm = holder.vm, settings = settings) } }
    }
    override fun onStop() { holder.vm.onPttRelease(); super.onStop() }
}
