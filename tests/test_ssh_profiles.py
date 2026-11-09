"""Regression coverage for saving multiple hosts, stale windows and backups."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication
from core import ssh_backend as backend
from ui.context import AppContext
from ui.ssh_dialog import SshDialog


class ProfilesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="adbhost-profiles-")
        self.path = Path(self.tmp.name) / "ssh_profiles.json"
        self.patch = patch.object(backend, "config_path", return_value=str(self.path))
        self.patch.start()
        self.a = {"name": "Host A", "host": "a.example", "port": 22, "user": "dev",
                  "password": "test-password-a", "adb": ""}
        self.assertTrue(backend.save_config({"last": 0, "profiles": [self.a]})[0])
        self.dialogs = []

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.close()
            dialog.deleteLater()
        self.app.processEvents()
        self.patch.stop()
        self.tmp.cleanup()

    def dialog(self):
        dlg = SshDialog(AppContext({}))
        self.dialogs.append(dlg)
        return dlg

    def test_saving_changed_address_adds_host_without_overwriting_selected_history(self):
        dlg = self.dialog()
        dlg.ed_host.setText("b.example")
        dlg.ed_pass.setText("test-password-b")
        dlg._save()
        profiles = backend.load_config()["profiles"]
        self.assertEqual([p["host"] for p in profiles], ["a.example", "b.example"])
        self.assertEqual(profiles[0]["password"], self.a["password"])
        self.assertEqual(dlg.cmb_saved.count(), 2)
        self.assertEqual(dlg.ed_host.text(), "b.example")

    def test_same_endpoint_updates_password_without_duplicate(self):
        dlg = self.dialog()
        dlg.ed_pass.setText("new-test-password")
        dlg._save()
        cfg = backend.load_config()
        self.assertEqual(len(cfg["profiles"]), 1)
        self.assertEqual(cfg["profiles"][0]["password"], "new-test-password")

    def test_new_connection_clears_credentials_but_keeps_saved_list(self):
        dlg = self.dialog()
        dlg._new_profile()
        self.assertEqual(dlg.cmb_saved.count(), 1)
        self.assertEqual(dlg.cmb_saved.currentIndex(), -1)
        self.assertEqual(dlg.ed_pass.text(), "")
        self.assertEqual(dlg.ed_host.text(), "")
        self.assertEqual(len(backend.load_config()["profiles"]), 1)

    def test_two_stale_windows_keep_both_new_hosts(self):
        first, second = self.dialog(), self.dialog()
        first.ed_host.setText("b.example")
        first._save()
        second.ed_host.setText("c.example")
        second._save()
        self.assertEqual({p["host"] for p in backend.load_config()["profiles"]},
                         {"a.example", "b.example", "c.example"})
        self.assertEqual(second.cmb_saved.count(), 3)

    def test_selecting_history_does_not_restore_stale_password_over_new_one(self):
        first, second = self.dialog(), self.dialog()
        first.ed_pass.setText("latest-test-password")
        first._save()
        second._selection_changed(0)
        self.assertEqual(backend.load_config()["profiles"][0]["password"], "latest-test-password")
        self.assertEqual(second.ed_pass.text(), "latest-test-password")

    def test_delete_is_explicit_and_keeps_other_hosts(self):
        dlg = self.dialog()
        dlg.ed_host.setText("b.example")
        dlg._save()
        dlg._delete()
        self.assertEqual([p["host"] for p in backend.load_config()["profiles"]], ["a.example"])

    def test_failed_atomic_replace_keeps_previous_config(self):
        before = self.path.read_bytes()
        with patch.object(backend.os, "replace", side_effect=OSError("injected")):
            ok, error = backend.save_config({"last": 0, "profiles": [dict(self.a, host="b.example")]})
        self.assertFalse(ok)
        self.assertIn("injected", error)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    def test_backup_preserves_old_records_and_is_bounded(self):
        for n in range(12):
            self.assertTrue(backend.save_config({"last": 0, "profiles": [dict(self.a, name=str(n))]})[0])
        backups = list(self.path.parent.glob("ssh_profiles.backup-*.json"))
        self.assertEqual(len(backups), 10)
        for backup in backups:
            self.assertEqual(json.loads(backup.read_text(encoding="utf-8"))["profiles"][0]["host"], "a.example")

    def test_username_case_is_not_merged_on_linux(self):
        self.assertTrue(backend.save_config({"last": 0, "profiles": [dict(self.a, user="Dev")]})[0])
        self.assertEqual({p["user"] for p in backend.load_config()["profiles"]}, {"dev", "Dev"})

    def test_ssh_automatically_uses_existing_keys_even_with_saved_password(self):
        client = unittest.mock.MagicMock()
        with patch.object(backend.paramiko, "SSHClient", return_value=client):
            remote = backend.RemoteSession("test.invalid", user="root", password="test-password")
            remote._connect_paramiko(3)
        kwargs = client.connect.call_args.kwargs
        self.assertTrue(kwargs["allow_agent"])
        self.assertTrue(kwargs["look_for_keys"])
        self.assertEqual(kwargs["password"], "test-password")

    def test_explicit_key_is_used_without_trying_unrelated_keys(self):
        client = unittest.mock.MagicMock()
        with patch.object(backend.paramiko, "SSHClient", return_value=client):
            remote = backend.RemoteSession("test.invalid", user="root", keyfile="test-key")
            remote._connect_paramiko(3)
        kwargs = client.connect.call_args.kwargs
        self.assertEqual(kwargs["key_filename"], "test-key")
        self.assertFalse(kwargs["allow_agent"])
        self.assertFalse(kwargs["look_for_keys"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
