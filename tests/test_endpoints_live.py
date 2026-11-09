"""Opt-in live endpoint acceptance; saved credentials never printed.

python tests/test_endpoints_live.py --saved-profile
Creates/removes only unique test directories. Does not open real cameras.
"""
import hashlib
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import ssh_backend as ssh
from core import terminal_backend as tb
from core.file_transfer import FileEndpoint, transfer_paths
from core.adb_backend import quote


def main():
    if "--saved-profile" not in sys.argv:
        print("SKIP: pass --saved-profile for live acceptance")
        return 0
    profiles = ssh.load_config().get("profiles", [])
    if not profiles:
        print("SKIP: no saved SSH profile")
        return 0
    p = profiles[0]
    remote = ssh.RemoteSession(p["host"], p.get("port", 22), p.get("user", ""),
                               p.get("password", ""), p.get("adb", ""))
    remote.connect(timeout=8)
    root = "/tmp/adbhost_e2e_" + uuid.uuid4().hex
    device_root = "/data/local/tmp/adbhost_e2e_" + uuid.uuid4().hex
    device = None
    terminal = None
    try:
        with tempfile.TemporaryDirectory(prefix="adbhost-live-") as local:
            local = Path(local)
            source = local / "source"
            source.mkdir()
            name = "多 路's.bin"
            payload = bytes(range(256)) * 257 + b"\x00\r\n\xff"
            (source / name).write_bytes(payload)
            folder = source / "目录 with space"
            folder.mkdir()
            (folder / "nested.txt").write_bytes(b"nested\x00\r\n")
            expected = hashlib.sha256(payload).hexdigest()
            windows, host = FileEndpoint("windows"), FileEndpoint("ssh", remote)
            host.mkdir(root + "/src")
            # Force any accidental dependency on adb to fail during SSH-only tests.
            adb_original = remote.adb
            def no_adb(*_args, **_kwargs):
                raise AssertionError("SSH-only feature attempted ADB")
            remote.adb = no_adb
            paths = [str(source / name), str(folder)]
            transfer_paths(windows, host, paths, root + "/src")
            returned = local / "returned"
            returned.mkdir()
            transfer_paths(host, windows, [root + "/src/" + name, root + "/src/" + folder.name], str(returned))
            assert hashlib.sha256((returned / name).read_bytes()).hexdigest() == expected
            assert (returned / folder.name / "nested.txt").read_bytes() == b"nested\x00\r\n"
            print("PASS: real Windows -> SSH -> Windows, binary SHA256 and nested directory, ADB forbidden")
            host.rename(root + "/src/" + name, root + "/src/renamed.bin")
            host.rename(root + "/src/renamed.bin", root + "/src/" + name)
            print("PASS: SSH rename uses SSH filesystem")
            terminal = tb.RemoteAdbShellSession(remote, "", rows=24, cols=100)
            terminal.start()
            marker = "ENDPOINT_SSH_" + uuid.uuid4().hex
            terminal.send_line("printf '%s\\n' " + quote(marker))
            received = bytearray()
            until = time.monotonic() + 8
            while time.monotonic() < until:
                while not terminal.out_q.empty():
                    received.extend(terminal.out_q.get())
                if received.count(marker.encode()) >= 2:
                    break
                time.sleep(.03)
            assert received.count(marker.encode()) >= 2, "SSH terminal missing actual command output"
            terminal.stop()
            terminal = None
            print("PASS: real SSH PTY terminal runs command without ADB")
            rc, tools = remote.exec("command -v ffmpeg || true", timeout=10)
            if tools.strip():
                from PySide6.QtCore import QCoreApplication
                from PySide6.QtGui import QImage
                from ui.screenshot_page import CameraStreamWorker
                app = QCoreApplication.instance() or QCoreApplication([])
                decoded = []
                worker = CameraStreamWorker(
                    remote, "", "ffmpeg -nostdin -hide_banner -loglevel error -f lavfi "
                    "-i testsrc=size=160x120:rate=5 -frames:v 6 -c:v mjpeg -f mjpeg pipe:1",
                    ":", None, 1)
                def accept(data, _generation):
                    image = QImage.fromData(data, "JPG")
                    decoded.append((image.width(), image.height()))
                worker.frame_ready.connect(accept)
                worker.run()
                assert decoded == [(160, 120)] * 6, decoded
                print("PASS: real SSH binary MJPEG stream, six decoded synthetic frames, ADB forbidden")
            remote.adb = adb_original
            rc, inventory = remote.exec("command -v ffmpeg || true; ls /dev/video* 2>/dev/null || true", timeout=10)
            print("CAMERA_TOOLS: " + inventory.strip().replace("\n", ", "))
            devices = [d for d in remote.adb_devices() if d[1] == "device"]
            if devices:
                device = FileEndpoint("adb", remote, serial=devices[0][0])
                device.mkdir(device_root)
                transfer_paths(host, device, [root + "/src/" + name, root + "/src/" + folder.name], device_root)
                back = root + "/adb-to-ssh"
                host.mkdir(back)
                transfer_paths(device, host, [device_root + "/" + name, device_root + "/" + folder.name], back)
                check = local / "adb-ssh-check"
                check.mkdir()
                transfer_paths(host, windows, [back + "/" + name], str(check))
                assert hashlib.sha256((check / name).read_bytes()).hexdigest() == expected
                print("PASS: real SSH <-> ADB binary file + directory")
                device.mkdir(device_root + "/windows")
                transfer_paths(windows, device, paths, device_root + "/windows")
                check = local / "adb-windows-check"
                check.mkdir()
                transfer_paths(device, windows, [device_root + "/windows/" + name,
                                                device_root + "/windows/" + folder.name], str(check))
                assert hashlib.sha256((check / name).read_bytes()).hexdigest() == expected
                assert (check / folder.name / "nested.txt").read_bytes() == b"nested\x00\r\n"
                print("PASS: real Windows <-> ADB through SSH staging, binary + nested directory")
            else:
                print("SKIP: physical ADB directions; no online ADB device")
            print("PASS: temporary test data cleaned in finally; no real camera opened")
    finally:
        if terminal:
            terminal.stop()
        if device:
            device.delete([device_root])
        remote.exec("rm -rf " + quote(root), timeout=20)
        remote.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
