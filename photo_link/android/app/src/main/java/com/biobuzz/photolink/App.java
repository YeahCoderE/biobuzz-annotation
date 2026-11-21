package com.biobuzz.photolink;

import android.app.Application;

public class App extends Application {
    @Override
    public void onCreate() {
        super.onCreate();
        Hub.get(this);  // network callback + connection engine live as long as the process
    }
}
