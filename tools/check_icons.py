import os, sys
sys.path.insert(0, os.getcwd())
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPixmap, QPainter
from PySide6.QtCore import Qt, QSize
from PySide6.QtSvg import QSvgRenderer
app = QApplication([])
d = os.path.join("resources", "icons")
bad = []
for f in sorted(os.listdir(d)):
    if not f.endswith(".svg"): continue
    path = os.path.join(d, f)
    r = QSvgRenderer(path)
    if not r.isValid():
        bad.append((f, "invalid"))
        continue
    pm = QPixmap(QSize(24, 24)); pm.fill(Qt.transparent)
    p = QPainter(pm); r.render(p); p.end()
    img = pm.toImage()
    n = 0
    for y in range(img.height()):
        for x in range(img.width()):
            if img.pixelColor(x, y).alpha() > 8: n += 1
    if n < 12:
        bad.append((f, "空白(%d px)" % n))
print("图标总数:", len([f for f in os.listdir(d) if f.endswith('.svg')]))
print("异常:", bad if bad else "无")
