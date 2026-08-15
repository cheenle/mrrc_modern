package com.hamradio.ft710android.App

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import com.hamradio.ft710android.Data.SettingsStore

class MainActivity : ComponentActivity() {
    private val holder: MainViewModelHolder by viewModels()
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val settings = SettingsStore(applicationContext)
        setContent { AppTheme { RootScreen(vm = holder.vm, settings = settings) } }
    }
    override fun onStop() { holder.vm.onPttRelease(); super.onStop() }
}

@Composable
fun AppTheme(content: @Composable () -> Unit) {
    MaterialTheme(content = content)
}
