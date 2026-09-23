"""Visible Decision Infra settings smoke test using a temporary config and fake gateway.

Run: uv run python -B probe/settings_smoke.py
It opens the real settings page, presses the real connection-test button and saves only
to a temporary file. It never reads QQ messages, user credentials or user config.
"""

import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

import AppKit as A
from Foundation import NSDate

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_settings import Gateway, SettingsNetwork
import userconfig
from settings import SettingsController


def wait_for_request(controller):
    deadline = time.monotonic() + 5
    while controller.busy and time.monotonic() < deadline:
        A.NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.02))
    assert not controller.busy, "UI never received request completion"
    assert controller.save_button.isEnabled()


app = A.NSApplication.sharedApplication()
app.setActivationPolicy_(A.NSApplicationActivationPolicyRegular)
SettingsNetwork.setUpClass()
try:
    with tempfile.TemporaryDirectory() as directory, \
            patch.dict(os.environ, {}, clear=True), \
            patch.object(userconfig, "_startup_sources", None), \
            patch.object(userconfig, "env_files", return_value=[Path(directory) / "env"]), \
            patch.object(userconfig, "PROJECT_ENV", Path(directory) / ".env"):
        path = Path(directory) / "env"
        path.write_text('# keep\nCUSTOM="untouched"\n')
        userconfig.load()

        controller = SettingsController.alloc().init().build()
        controller.show()
        fields = controller.fields
        assert set(fields) == {"BASE_URL", "MODEL"}, "settings must expose only gateway route"
        fields["BASE_URL"].setStringValue_(SettingsNetwork.base)
        fields["MODEL"].setStringValue_("jev-latest")

        Gateway.requests = []
        controller.test_button.performClick_(None)
        wait_for_request(controller)
        assert "连接成功" in controller.status.stringValue(), controller.status.stringValue()
        assert Gateway.requests[-1][2]["model"] == "jev-latest"

        controller.save_button.performClick_(None)
        assert "已保存" in controller.status.stringValue(), controller.status.stringValue()
        saved = userconfig.parse_env_file(path)
        assert saved["DECISION_INFRA_BASE_URL"] == SettingsNetwork.base
        assert saved["DECISION_INFRA_MODEL"] == "jev-latest"
        assert "DECISION_INFRA_API_KEY" not in saved
        assert saved["CUSTOM"] == "untouched"
        assert path.stat().st_mode & 0o777 == 0o600
        assert not userconfig.get("DECISION_INFRA_BASE_URL"), "must not hot reload"

        controller.window.close()
        reopened = SettingsController.alloc().init().build()
        assert reopened.fields["MODEL"].stringValue() == "jev-latest"
        reopened.window.close()
        print("PASS: visible Decision Infra page, no app key, real async test button, secure save, restart isolation")
finally:
    SettingsNetwork.tearDownClass()
