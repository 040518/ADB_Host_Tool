"""Filesystem-copy contracts and SSH-only UI/session regressions.

Run: python tests/test_endpoints.py
"""
import os
import queue
import shlex
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.file_transfer import FileEndpoint, transfer_paths
from core import terminal_backend as tb
from core import adb_backend as ab


def copy_path(source, directory):
    target = Path(directory) / Path(source).name
    if Path(source).is_dir():
        shutil.copytree(source, target)
    else:
        shutil.copy2(source, target)


class FakeRemote:
    host, user, remote_home = "test.invalid", "tester", "/home/tester"
    backend, client = "paramiko", None
    adb_remote, serial = "/tools/adb", "CHANGED_SELECTION"

    def __init__(self, root):
        self.root = Path(root)
        self.adb_calls = []
        self.fail_upload = False
        self.commands = []

    def mapped(self, path):
        return self.root / "ssh" / str(path).lstrip("/")

    def exec(self, command, timeout=60):
        self.commands.append(command)
        words = shlex.split(command)
        if words[:1] == ["if"]:
            return 0, "EXISTS\n" if self.mapped(words[3]).exists() else ""
        if words[:1] == ["mkdir"]:
            self.mapped(words[-1]).mkdir(parents="-p" in words, exist_ok="-p" in words)
        elif words[:2] == ["rm", "-rf"]:
            path = self.mapped(words[2])
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        elif words[:2] == ["cp", "-a"]:
            copy_path(self.mapped(words[2]), self.mapped(words[3]).parent)
        elif words[:1] == ["mv"]:
            self.mapped(words[1]).rename(self.mapped(words[2]))
        else:
            raise AssertionError("Unexpected remote operation: " + command)
        return 0, ""

    def upload_file(self, source, directory):
        if self.fail_upload:
            return False, "Injected upload failure"
        copy_path(source, self.mapped(directory))
        return True, ""

    def download_file(self, source, directory):
        copy_path(self.mapped(source), directory)
        return True, ""

    def adb(self, args, timeout=60):
        self.adb_calls.append(args)
        assert args[:2] == ["-s", "ORIGINAL"], args
        op, source, dest = args[2:]
        if op == "push":
            copy_path(self.mapped(source), self.root / "adb" / dest.lstrip("/"))
        elif op == "pull":
            copy_path(self.root / "adb" / source.lstrip("/"), self.mapped(dest))
        elif op == "shell":
            raise AssertionError("Unexpected shell")
        return 0, "OK"


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="adbhost-test-")
        self.root = Path(self.tmp.name)
        self.remote = FakeRemote(self.root)
        self.local = self.root / "windows"
        for directory in (self.local, self.remote.mapped("/tmp"), self.remote.mapped("/src"), self.remote.mapped("/dst"),
                          self.root / "adb" / "src", self.root / "adb" / "dst"):
            directory.mkdir(parents=True)
        self.windows = FileEndpoint("windows")
        self.ssh = FileEndpoint("ssh", self.remote)
        # Model actual ADB existence on its own filesystem, not the SSH host.
        outer = self

        class DeviceEndpoint(FileEndpoint):
            def exists(self, path):
                self.require()
                return (outer.root / "adb" / path.lstrip("/")).exists()

        self.adb = DeviceEndpoint("adb", self.remote, serial="ORIGINAL")
        self.name = "多 路's.bin"
        self.payload = bytes(range(256)) * 257 + b"\x00\r\n\xff"

    def tearDown(self):
        self.tmp.cleanup()

    def populate(self, directory):
        (directory / self.name).write_bytes(self.payload)
        sub = directory / "目录 with space"
        sub.mkdir()
        (sub / "nested.txt").write_bytes(b"nested\x00\r\n")

    def test_all_six_directions_files_directories_and_quoted_names(self):
        mapping = {"windows": (self.windows, self.local),
                   "ssh": (self.ssh, self.remote.mapped("/src")),
                   "adb": (self.adb, self.root / "adb" / "src")}
        for source_kind, destination_kind in (("windows", "ssh"), ("ssh", "windows"),
                                               ("windows", "adb"), ("adb", "windows"),
                                               ("ssh", "adb"), ("adb", "ssh")):
            with self.subTest(source=source_kind, destination=destination_kind):
                source, source_dir = mapping[source_kind]
                for child in source_dir.iterdir():
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                self.populate(source_dir)
                destination, _ = mapping[destination_kind]
                tag = source_kind + "_" + destination_kind
                if destination_kind == "windows":
                    dest_dir = self.local / tag
                    dest_dir.mkdir()
                    dest_arg = str(dest_dir)
                else:
                    dest_arg = "/dst/" + tag
                    dest_dir = (self.remote.mapped(dest_arg) if destination_kind == "ssh"
                                else self.root / "adb" / dest_arg.lstrip("/"))
                    dest_dir.mkdir()
                names = [self.name, "目录 with space"]
                source_arg = str(source_dir) if source_kind == "windows" else "/src"
                paths = [source.join(source_arg, n) for n in names]
                self.assertEqual(transfer_paths(source, destination, paths, dest_arg), names)
                self.assertEqual((dest_dir / self.name).read_bytes(), self.payload)
                self.assertEqual((dest_dir / names[1] / "nested.txt").read_bytes(), b"nested\x00\r\n")
                self.assertFalse(list(self.remote.mapped("/tmp").glob("adbhost_transfer_*")))

    def test_windows_ssh_transfer_without_any_adb(self):
        self.populate(self.local)
        self.remote.adb = lambda *_a, **_kw: self.fail("Must not use ADB")
        transfer_paths(self.windows, self.ssh, [str(self.local / self.name)], "/dst")
        target = self.local / "returned"
        target.mkdir()
        transfer_paths(self.ssh, self.windows, ["/dst/" + self.name], str(target))
        self.assertEqual((target / self.name).read_bytes(), self.payload)

    def test_existing_destination_is_not_overwritten(self):
        self.populate(self.local)
        target = self.remote.mapped("/dst") / self.name
        target.write_bytes(b"original")
        with self.assertRaises(FileExistsError):
            transfer_paths(self.windows, self.ssh, [str(self.local / self.name)], "/dst")
        self.assertEqual(target.read_bytes(), b"original")

    def test_staging_cleanup_after_failed_upload(self):
        self.populate(self.local)
        self.remote.fail_upload = True
        with self.assertRaisesRegex(RuntimeError, "Injected"):
            transfer_paths(self.windows, self.adb, [str(self.local / self.name)], "/dst")
        self.assertFalse(list(self.remote.mapped("/tmp").glob("adbhost_transfer_*")))

    def test_no_adb_fails_before_file_changes(self):
        self.populate(self.local)
        with self.assertRaisesRegex(RuntimeError, "ADB"):
            transfer_paths(self.windows, FileEndpoint("adb", self.remote),
                           [str(self.local / self.name)], "/dst")
        self.assertFalse(list((self.root / "adb" / "dst").iterdir()))

    def test_cannot_copy_directory_into_itself(self):
        source = self.local / "parent"
        child = source / "child"
        child.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "子目录"):
            transfer_paths(self.windows, self.windows, [str(source)], str(child))

    def test_ssh_rename_uses_remote_filesystem(self):
        source = self.remote.mapped("/src/old.txt")
        source.write_bytes(self.payload)
        self.ssh.rename("/src/old.txt", "/src/new.txt")
        self.assertFalse(source.exists())
        self.assertEqual(self.remote.mapped("/src/new.txt").read_bytes(), self.payload)

    def test_local_adb_push_pull_keep_selected_serial(self):
        outer = self
        class LocalAdb:
            serial = "OTHER"
            def run(self, args, serial="auto", **kwargs):
                assert serial is None
                assert args[:2] == ["-s", "ORIGINAL"]
                direction, source, dest = args[2:]
                if direction == "push":
                    copy_path(source, outer.root / "adb" / dest.lstrip("/"))
                else:
                    copy_path(outer.root / "adb" / source.lstrip("/"), dest)
                return 0, ""
        class Device(FileEndpoint):
            def exists(self, path):
                return (outer.root / "adb" / path.lstrip("/")).exists()
        device = Device("adb", adb=LocalAdb(), serial="ORIGINAL")
        self.populate(self.local)
        transfer_paths(self.windows, device, [str(self.local / self.name)], "/dst")
        target = self.local / "returned"
        target.mkdir()
        transfer_paths(device, self.windows, ["/dst/" + self.name], str(target))
        self.assertEqual((target / self.name).read_bytes(), self.payload)


class SshOnlyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_target_resolution_and_identity(self):
        from ui.context import AppContext
        ctx = AppContext({})
        self.assertIsNone(ctx.resolve_target())
        ctx.remote = object()
        self.assertEqual(ctx.resolve_target(), "ssh")
        self.assertIsNone(ctx.resolve_target("adb"))
        before = ctx.target_identity()
        ctx.current = ab.DeviceInfo("ORIGINAL", "device")
        self.assertEqual(ctx.resolve_target(), "adb")
        self.assertEqual(ctx.resolve_target("ssh"), "ssh")
        self.assertNotEqual(before, ctx.target_identity())
        ctx.current = ab.DeviceInfo("ORIGINAL", "offline")
        self.assertEqual(ctx.resolve_target(), "ssh")

    def test_direct_ssh_session_does_not_inject_adb_or_resize_commands(self):
        class Remote:
            def open_shell(self, **kwargs):
                self.kwargs = kwargs
                self.writes, self.sizes = [], []
                def write(data):
                    self.writes.append(data)
                write.resize_pty = lambda w, h: self.sizes.append((w, h))
                return write, lambda: b"", lambda: None
        remote = Remote()
        session = tb.RemoteAdbShellSession(remote, "", rows=24, cols=80)
        try:
            session.start()
            session.resize(35, 110)
            self.assertIsNone(remote.kwargs["command"])
            self.assertIsNone(session._resize_job)
            self.assertFalse(remote.writes)
            self.assertEqual(remote.sizes[-1], (110, 35))
        finally:
            session.stop()

    def test_file_panes_and_camera_available_with_ssh_only(self):
        from ui.context import AppContext
        from ui.file_page import FilePage
        from ui.screenshot_page import ScreenshotPage
        class Remote:
            host, user, remote_home = "test.invalid", "tester", "/home/tester"
            backend, client = "paramiko", None
            def realpath(self, path):
                return path
            def list_dir(self, path):
                return True, [("remote.txt", False, 3, "2026-11-08")], ""
        ctx = AppContext({})
        ctx.remote = Remote()
        fp, shot = FilePage(ctx), ScreenshotPage(ctx)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            self.app.processEvents()
            if not ctx._workers:
                break
            time.sleep(.01)
        self.assertEqual(fp.pane_local.endpoint_kind, "windows")
        self.assertEqual(fp.pane_remote.endpoint_kind, "ssh")
        self.assertEqual(fp.pane_remote.model.item(0).data(256)[0], "remote.txt")
        self.assertEqual(shot.cmb_mode.currentIndex(), 1)
        self.assertTrue(shot.btn_cap.isEnabled())
        self.assertIn("无需 ADB", shot.lbl_target.text())
        self.assertEqual(shot.cmb_rotation.currentData(), 0)
        self.assertNotIn("adb", shot._camera_stream_pipeline()[2])
        # setsid forks when the SSH command is a process-group leader; -w must wait.
        class Worker:
            def __init__(self, remote, start, command, cleanup, forward, generation, parent):
                self.command = command
        ctx.remote.client = object()
        with patch("ui.screenshot_page.CameraStreamWorker", Worker), patch.object(shot, "_attach_stream_worker"):
            self.assertTrue(shot._start_streaming_preview())
            command = shot._attach_stream_worker.call_args.args[0].command
            self.assertIn("setsid -w", command)
        generation = shot._preview_generation
        ctx.device_selected.emit(None)
        self.assertEqual(generation, shot._preview_generation)
        shot.shutdown()
        ctx.stop_all_workers()
        fp.deleteLater()
        shot.deleteLater()

    def test_camera_stream_direct_ssh_never_invokes_adb(self):
        from ui.screenshot_page import CameraStreamWorker
        frames = [b"\xff\xd8frame-A\xff\xd9", b"\xff\xd8frame-B\xff\xd9"]
        class Channel:
            def __init__(self):
                self.parts = [b"noise" + frames[0][:4], frames[0][4:] + frames[1]]
            def exec_command(self, command):
                self.command = command
            def recv_ready(self):
                return bool(self.parts)
            def recv(self, _size):
                return self.parts.pop(0)
            def exit_status_ready(self):
                return not self.parts
            def recv_stderr_ready(self):
                return False
            def close(self):
                pass
        channel = Channel()
        class Transport:
            def is_active(self):
                return True
            def open_session(self, **_):
                return channel
        class Client:
            def get_transport(self):
                return Transport()
        class Remote:
            client = Client()
            def adb(self, *_args, **_kwargs):
                raise AssertionError("SSH-only preview invoked ADB")
            def exec(self, command, **_):
                self.cleaned = command
                return 0, ""
        remote = Remote()
        worker = CameraStreamWorker(remote, "", "direct camera command", "cleanup owned camera", None, 7)
        received = []
        worker.frame_ready.connect(lambda jpeg, generation: received.append((jpeg, generation)))
        worker.run()
        self.assertEqual(received, [(frame, 7) for frame in frames])
        self.assertEqual(remote.cleaned, "cleanup owned camera")
        self.assertEqual(channel.command, "direct camera command")

    def test_terminal_page_connects_ssh_without_device(self):
        from ui.context import AppContext
        from ui.terminal_page import TerminalPage
        class Remote:
            host, user = "test.invalid", "tester"
            def open_shell(self, **kwargs):
                self.command = kwargs["command"]
                def write(_):
                    pass
                write.resize_pty = lambda *_: None
                return write, lambda: b"", lambda: None
        ctx = AppContext({})
        ctx.remote = Remote()
        page = TerminalPage(ctx)
        try:
            page.connect_terminal()
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline and page.session is None:
                self.app.processEvents()
                time.sleep(.01)
            self.assertIsNotNone(page.session)
            self.assertIsNone(ctx.remote.command)
            self.assertIn("SSH 目标终端", page.lbl_state.text())
            self.assertEqual(page.session.serial, "")
        finally:
            page.shutdown()
            ctx.stop_all_workers()
            page.deleteLater()

    def test_switching_filesystem_ignores_old_directory_result(self):
        from ui.context import AppContext
        from ui.file_page import FilePane
        class Remote:
            host, remote_home = "test.invalid", "/old-remote"
            def realpath(self, path):
                return path
            def list_dir(self, path):
                time.sleep(.15)
                return True, [("STALE_REMOTE", False, 1, "")], ""
        ctx = AppContext({})
        ctx.remote = Remote()
        pane = FilePane(ctx, "local")
        pane.set_endpoint("ssh")
        pane.set_endpoint("windows")
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            self.app.processEvents()
            if not ctx._workers:
                break
            time.sleep(.01)
        self.assertEqual(pane.cwd, os.path.expanduser("~"))
        self.assertEqual(pane.endpoint_kind, "windows")
        names = [pane.model.item(i, 0).text() for i in range(pane.model.rowCount())]
        self.assertNotIn("STALE_REMOTE", names)
        ctx.stop_all_workers()
        pane.deleteLater()

    def test_camera_keeps_stopping_worker_alive_until_finished(self):
        from PySide6.QtCore import QThread, Signal
        from ui.context import AppContext
        from ui.screenshot_page import ScreenshotPage
        class Remote:
            host, user = "test.invalid", "tester"
        class SlowWorker(QThread):
            frame_ready = Signal(bytes, int)
            stream_ended = Signal(int, str, int)
            def stop(self):
                self.requestInterruption()
            def run(self):
                self.msleep(80)
        ctx = AppContext({})
        ctx.remote = Remote()
        page = ScreenshotPage(ctx)
        worker = SlowWorker(page)
        page._preview_active = True
        page._attach_stream_worker(worker, 640, 480)
        page.stop_preview()
        self.assertIn(worker, page._owned_stream_workers)
        self.assertFalse(page.btn_cap.isEnabled())
        until = time.monotonic() + 1
        while page._owned_stream_workers and time.monotonic() < until:
            self.app.processEvents()
            time.sleep(.01)
        self.assertFalse(page._owned_stream_workers)
        self.assertTrue(page.btn_cap.isEnabled())
        page.shutdown()
        page.deleteLater()

    def test_ssh_camera_selects_multiplanar_capture_and_portable_setsid(self):
        from ui.screenshot_page import ScreenshotPage
        pipeline = ScreenshotPage._nv12_pipeline("/dev/video0", 640, 480)
        route = ScreenshotPage._ssh_camera_route("/dev/video0", pipeline, "usb-camera")
        self.assertIn("grep -q Multiplanar", route)
        self.assertIn("pixelformat=NV12", route)
        self.assertIn("-f rawvideo", route)
        self.assertIn("else usb-camera", route)
        command = ScreenshotPage._ssh_session_command("echo test")
        self.assertIn("--wait", command)
        self.assertIn("else exec setsid sh -c", command)

    def test_camera_does_not_stop_at_setsid_parent_exit_before_stdout_eof(self):
        from ui.screenshot_page import CameraStreamWorker
        frame = b"\xff\xd8frame\xff\xd9"
        class Channel:
            closed = False
            def __init__(self):
                self.polls = 0
                self.sent = False
            @property
            def eof_received(self):
                return self.sent
            def exec_command(self, _): pass
            def recv_ready(self):
                self.polls += 1
                return self.polls >= 3 and not self.sent
            def recv(self, _):
                self.sent = True
                return frame
            def exit_status_ready(self): return True
            def recv_stderr_ready(self): return False
            def close(self): self.closed = True
        channel = Channel()
        class Transport:
            def is_active(self): return True
            def open_session(self, **_): return channel
        class Client:
            def get_transport(self): return Transport()
        class Remote:
            client = Client()
            def exec(self, *_a, **_kw): return 0, ""
        worker = CameraStreamWorker(Remote(), "", "busybox-setsid", ":", None, 1)
        received = []
        worker.frame_ready.connect(lambda data, _gen: received.append(data))
        worker.run()
        self.assertEqual(received, [frame])


if __name__ == "__main__":
    unittest.main(verbosity=2)
