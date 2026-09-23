plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "info.jevqq.analyst"
    compileSdk = 35

    defaultConfig {
        applicationId = "info.jevqq.analyst"
        minSdk = 26
        targetSdk = 35
        versionCode = 8
        versionName = "0.6.2"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    signingConfigs {
        create("release") {
            val storePath = providers.environmentVariable("ANDROID_SIGNING_STORE_FILE").orNull
            if (!storePath.isNullOrBlank()) {
                storeFile = file(storePath)
                storePassword = providers.environmentVariable("ANDROID_SIGNING_STORE_PASSWORD").orNull
                keyAlias = providers.environmentVariable("ANDROID_SIGNING_KEY_ALIAS").orNull
                keyPassword = providers.environmentVariable("ANDROID_SIGNING_KEY_PASSWORD").orNull
            }
        }
    }
    buildTypes {
        getByName("release") {
            signingConfig = signingConfigs.getByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }

    sourceSets.getByName("main").assets.srcDir("../../shared")
    sourceSets.getByName("test").resources.srcDir("../../shared")
}

dependencies {
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
    testImplementation("com.squareup.okhttp3:mockwebserver:4.12.0")
}
