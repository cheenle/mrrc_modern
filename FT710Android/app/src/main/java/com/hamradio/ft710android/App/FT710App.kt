package com.hamradio.ft710android.App

import android.app.Application

class FT710App : Application() {
    override fun onCreate() {
        super.onCreate()
        instance = this
        ServiceLocator.assemble()
    }

    companion object { lateinit var instance: FT710App }
}
