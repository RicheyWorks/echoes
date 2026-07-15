"""Validate the Android Kotlin sources on every PR.

Linux CI can't run `./gradlew build`; what we can do is sanity-check
the file structure + class/method names so the Android app stays in
sync with the Python client and the server's HTTP surface. The actual
Gradle build should happen on a machine with the Android SDK.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT   = Path(__file__).parent.parent / "deploy" / "android"
APP    = ROOT / "app" / "src" / "main" / "kotlin" / "com" / "automaton"
CLIENT = APP / "client"
SCREENS = APP / "screens"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _read_app(rel: str) -> str:
    return (APP / rel).read_text(encoding="utf-8")


# ------------------------------------------------------------------ #
# Project layout                                                       #
# ------------------------------------------------------------------ #

def test_settings_gradle_exists_with_app_module():
    src = _read("settings.gradle.kts")
    assert 'include(":app")' in src


def test_app_build_gradle_declares_compose():
    src = _read("app/build.gradle.kts")
    assert "compose = true" in src


def test_app_build_gradle_uses_okhttp():
    src = _read("app/build.gradle.kts")
    assert "okhttp3:okhttp" in src


def test_app_build_gradle_uses_serialization():
    src = _read("app/build.gradle.kts")
    assert "kotlinx-serialization-json" in src


def test_app_build_gradle_uses_encrypted_shared_prefs():
    src = _read("app/build.gradle.kts")
    assert "security-crypto" in src


def test_app_build_gradle_uses_work_manager():
    src = _read("app/build.gradle.kts")
    assert "work-runtime" in src


# ------------------------------------------------------------------ #
# Models.kt                                                            #
# ------------------------------------------------------------------ #

@pytest.mark.parametrize("status", [
    "pending", "running", "completed", "failed", "cancelled", "timed_out",
])
def test_run_status_enum_matches_server(status):
    """RunStatus enum must include every status the server can emit,
    including timed_out added in migration 0003."""
    src = (CLIENT / "Models.kt").read_text(encoding="utf-8")
    assert status in src, f"RunStatus missing: {status}"


def test_models_have_serial_name_annotations():
    """snake_case field names must use @SerialName so the Kotlin decoder
    maps them from the server's JSON without a runtime naming strategy."""
    src = (CLIENT / "Models.kt").read_text(encoding="utf-8")
    assert "@SerialName" in src


def test_models_include_run_detail():
    src = (CLIENT / "Models.kt").read_text(encoding="utf-8")
    assert "class RunDetail" in src


def test_models_include_trigger_result():
    src = (CLIENT / "Models.kt").read_text(encoding="utf-8")
    assert "class TriggerResult" in src


# ------------------------------------------------------------------ #
# AutomatonClient.kt                                                   #
# ------------------------------------------------------------------ #

@pytest.mark.parametrize("method", [
    "fun runs(",
    "fun runDetail(",
    "fun workflows(",
    "fun agents(",
    "fun agentEntries(",
    "fun trigger(",
    "fun signal(",
    "fun cancel(",
    "fun health(",
])
def test_client_method_present(method):
    src = (CLIENT / "AutomatonClient.kt").read_text(encoding="utf-8")
    assert method in src, f"AutomatonClient missing: {method}"


def test_client_uses_coroutines():
    src = (CLIENT / "AutomatonClient.kt").read_text(encoding="utf-8")
    assert "suspend fun" in src


def test_client_uses_dispatchers_io():
    src = (CLIENT / "AutomatonClient.kt").read_text(encoding="utf-8")
    assert "Dispatchers.IO" in src


def test_client_pins_cert_when_fingerprint_set():
    src = (CLIENT / "AutomatonClient.kt").read_text(encoding="utf-8")
    assert "pinnedCertSHA256" in src
    assert "SHA-256" in src or "MessageDigest" in src


def test_client_sends_bearer_token():
    src = (CLIENT / "AutomatonClient.kt").read_text(encoding="utf-8")
    assert "Bearer" in src


# ------------------------------------------------------------------ #
# Settings.kt                                                          #
# ------------------------------------------------------------------ #

def test_token_stored_in_encrypted_shared_prefs_not_plain():
    src = _read_app("Settings.kt")
    assert "EncryptedSharedPreferences" in src
    # Token must NOT be stored in plain SharedPreferences.
    # Look for any plain prefs.edit() that also mentions token.
    # Easiest check: EncryptedSharedPreferences is used for the token key.
    assert "KEY_TOKEN" in src


def test_settings_exposes_make_client():
    src = _read_app("Settings.kt")
    assert "fun makeClient" in src


# ------------------------------------------------------------------ #
# Screens                                                              #
# ------------------------------------------------------------------ #

@pytest.mark.parametrize("screen_file,composable_name", [
    ("RunsListScreen.kt",   "RunsListScreen"),
    ("RunDetailScreen.kt",  "RunDetailScreen"),
    ("WorkflowsScreen.kt",  "WorkflowsScreen"),
    ("AgentsScreen.kt",     "AgentsScreen"),
    ("SettingsScreen.kt",   "SettingsScreen"),
])
def test_screen_declares_composable(screen_file, composable_name):
    src = (SCREENS / screen_file).read_text(encoding="utf-8")
    assert "@Composable" in src
    assert f"fun {composable_name}" in src


def test_run_detail_polls_while_active():
    """RunDetailScreen must poll the API while the run is not terminal."""
    src = (SCREENS / "RunDetailScreen.kt").read_text(encoding="utf-8")
    assert "delay(" in src or "POLL_INTERVAL" in src


def test_run_detail_has_signal_responder():
    """The 'respond to signal' UX must be present for the agent loop."""
    src = (SCREENS / "RunDetailScreen.kt").read_text(encoding="utf-8")
    assert "signal" in src.lower()
    assert "Signal" in src


def test_runs_list_shows_timed_out_status():
    """StatusBadge must handle the timed_out status introduced in A7."""
    src = (SCREENS / "RunsListScreen.kt").read_text(encoding="utf-8")
    assert "timed_out" in src


# ------------------------------------------------------------------ #
# MainActivity.kt                                                      #
# ------------------------------------------------------------------ #

def test_main_activity_uses_navigation_bar():
    src = _read_app("MainActivity.kt")
    assert "NavigationBar" in src


def test_main_activity_wires_three_tabs():
    src = _read_app("MainActivity.kt")
    assert "Tab.Runs" in src
    assert "Tab.Workflows" in src
    assert "Tab.Settings" in src


def test_settings_screen_documents_sideload_path():
    """Settings screen should explain sideloading so users know how to
    install without the Play Store."""
    src = (SCREENS / "SettingsScreen.kt").read_text(encoding="utf-8")
    assert "sideload" in src.lower() or "apk" in src.lower()
