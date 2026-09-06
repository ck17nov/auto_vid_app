plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.kotlin.serialization)
    alias(libs.plugins.ksp)
}

android {
    namespace = "com.autotube.ai"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.autotube.ai"
        // Samsung Galaxy M34 5G ships Android 13/14; 26 keeps older devices usable.
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"

        // Default backend address. Overridable at runtime in Settings.
        // 10.0.2.2 is the host machine as seen from the Android emulator.
        // Placeholder only. Retrofit needs a parseable base URL to build a
        // service, but this address (the emulator's view of the host) is never
        // dialled: the repository refuses to call anything while the
        // configured URL is blank.
        buildConfigField("String", "DEFAULT_BACKEND_URL", "\"http://10.0.2.2:8099/\"")

        // OAuth redirect scheme. A Google *Android* OAuth client is validated
        // by package name plus signing certificate, and AppAuth's redirect is
        // "<package>:/oauth2redirect" - so the scheme MUST equal the real
        // applicationId of the build being installed.
        //
        // Set once, here, for every build type. There is deliberately NO
        // applicationIdSuffix on debug: a suffix means the OAuth client in
        // Google Cloud has to be registered against "com.autotube.ai.debug",
        // and registering the obvious "com.autotube.ai" instead makes Google
        // reject the authorization request with a bare "invalid request".
        // Side-by-side debug and release installs are not worth an OAuth
        // setup that silently breaks when you switch build type.
        manifestPlaceholders["appAuthRedirectScheme"] = "com.autotube.ai"
    }

    buildTypes {
        debug {
            isMinifyEnabled = false
        }
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        compose = true
        buildConfig = true
    }
    packaging {
        resources {
            excludes += "/META-INF/{AL2.0,LGPL2.1}"
        }
    }
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    // Provides collectAsStateWithLifecycle(), used by every screen.
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(libs.androidx.activity.compose)

    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.ui)
    implementation(libs.androidx.ui.graphics)
    implementation(libs.androidx.ui.tooling.preview)
    implementation(libs.androidx.material3)
    implementation(libs.androidx.material.icons.extended)
    implementation(libs.androidx.navigation.compose)
    debugImplementation(libs.androidx.ui.tooling)

    // Local persistence (spec section 27)
    implementation(libs.androidx.room.runtime)
    implementation(libs.androidx.room.ktx)
    ksp(libs.androidx.room.compiler)

    // Background scheduling (spec section 2)
    implementation(libs.androidx.work.runtime.ktx)

    // Networking
    implementation(libs.retrofit)
    implementation(libs.retrofit.kotlinx.serialization)
    implementation(libs.okhttp)
    implementation(libs.okhttp.logging)
    implementation(libs.kotlinx.serialization.json)

    // Secrets at rest (spec section 30) - Android Keystore backed
    implementation(libs.androidx.security.crypto)
    implementation(libs.androidx.datastore.preferences)

    // YouTube OAuth 2.0 (spec section 19)
    implementation(libs.appauth)

    // Video preview
    implementation(libs.androidx.media3.exoplayer)
    implementation(libs.androidx.media3.ui)

    testImplementation(libs.junit)
    testImplementation(libs.kotlinx.coroutines.test)
    // Drives the transport-retry test against a real socket. Test-only: not
    // packaged into the APK.
    testImplementation(libs.okhttp.mockwebserver)
    androidTestImplementation(libs.androidx.test.junit)
    androidTestImplementation(libs.androidx.espresso.core)
    androidTestImplementation(platform(libs.androidx.compose.bom))
}
