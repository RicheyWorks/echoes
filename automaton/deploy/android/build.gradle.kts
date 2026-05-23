// Top-level build file. Plugin versions are declared here so all
// sub-modules use the same toolchain.
plugins {
    alias(libs.plugins.android.application) apply false
    alias(libs.plugins.kotlin.android) apply false
    alias(libs.plugins.kotlin.serialization) apply false
}
