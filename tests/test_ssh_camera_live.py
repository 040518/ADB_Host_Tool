"""Opt-in real SSH camera preview and process-cleanup acceptance."""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QCoreApplication, QEvent
from core import ssh_backend
from ui.context import AppContext
from ui.screenshot_page import ScreenshotPage
from ui import theme


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--node", default="/dev/video0")
    parser.add_argument("--seconds", type=float, default=6)
    parser.add_argument("--size", default="640×480")
    args = parser.parse_args()
    profiles = ssh_backend.load_config().get("profiles", [])
    p = next(p for p in profiles if p.get("host") == args.host)
    remote = ssh_backend.RemoteSession(p["host"], p.get("port", 22), p["user"], p.get("password", ""))
    remote.connect(timeout=8)
    app = QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(theme.load_stylesheet())
    ctx = AppContext(dict(theme.DEFAULT_SETTINGS, auto_refresh_sec=0))
    ctx.remote = remote
    ctx.log_message.connect(lambda level, text: print("LOG:", level, text))
    remote.adb = lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("SSH camera invoked ADB"))
    page = ScreenshotPage(ctx)
    page.resize(1100, 760)
    page.cmb_camera.setCurrentText(args.node)
    page.cmb_size.setCurrentText(args.size)
    page.show()
    total = [0]
    try:
        page.start_preview()
        worker = page._stream_worker
        if worker:
            worker.stream_ended.connect(lambda count, message, _gen: print("STREAM_END:", count, message))
            worker.frame_ready.connect(lambda *_: total.__setitem__(0, total[0] + 1))
        until = time.monotonic() + args.seconds
        while time.monotonic() < until:
            app.processEvents()
            time.sleep(.01)
        assert page._pixmap is not None and not page._pixmap.isNull(), page.lbl_info.text()
        assert total[0] >= 10, (total[0], page.lbl_info.text())
        out = Path("docs/screenshots")
        out.mkdir(exist_ok=True)
        path = out / ("ssh-usb-camera-%s.png" % args.size.replace("×", "x"))
        assert page._pixmap.save(str(path))
        print("PASS: real SSH camera, ADB calls forbidden, frames=%d" % total[0])
        print("STATUS:", page.lbl_info.text())
        print("SAVED:", path)
        page.stop_preview()
        page.shutdown()
        for _ in range(5):
            app.processEvents()
            time.sleep(.03)
        rc, out = remote.exec("find /tmp -maxdepth 1 -user $(id -u) -name '.adbhost_camera_*.pid' -print", timeout=10)
        assert not out.strip(), "Camera pidfile left after stop: " + out
        print("PASS: stop returns; owned camera pidfile removed")
    finally:
        page.shutdown()
        page.close()
        ctx.stop_all_workers()
        remote.close()
        page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
