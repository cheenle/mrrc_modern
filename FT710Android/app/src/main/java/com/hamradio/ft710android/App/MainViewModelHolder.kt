package com.hamradio.ft710android.App

import androidx.lifecycle.ViewModel
import com.hamradio.ft710android.ViewModel.MainViewModel

class MainViewModelHolder : ViewModel() {
    val vm: MainViewModel by lazy { ServiceLocator.vmFactory() }
}
