import sys, os
sys.path.insert(0, os.getcwd())
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPixmap, QPainter, QColor
from PySide6.QtCore import Qt, QSize, QRectF
from PySide6.QtSvg import QSvgRenderer
from PIL import Image

app = QApplication([])
# 深色圆角底 + 蓝色芯片图标
sizes = [16, 24, 32, 48, 64, 128, 256]
imgs = []
svg = open(os.path.join("resources", "icons", "chip.svg"), encoding="utf-8").read()
svg = svg.replace('stroke="#CCCCCC"', 'stroke="#4FC1FF"')
renderer = QSvgRenderer(svg.encode())

for s in sizes:
    pm = QPixmap(s, s)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    # 背景圆角方块
    p.setBrush(QColor("#1E1E1E"))
    p.setPen(Qt.NoPen)
    r = s * 0.14
    p.drawRoundedRect(QRectF(0, 0, s, s), r, r)
    # 内描边
    p.setBrush(Qt.NoBrush)
    from PySide6.QtGui import QPen
    p.setPen(QPen(QColor("#0E639C"), max(1.0, s * 0.03)))
    p.drawRoundedRect(QRectF(s*0.06, s*0.06, s*0.88, s*0.88), r*0.8, r*0.8)
    # 图标本体（留出内边距）
    pad = s * 0.18
    renderer.render(p, QRectF(pad, pad, s - 2*pad, s - 2*pad))
    p.end()
    tmp = "_icon_%d.png" % s
    pm.save(tmp, "PNG")
    imgs.append(Image.open(tmp).convert("RGBA"))

imgs[-1].save("resources/app.ico", format="ICO",
              sizes=[(i.width, i.height) for i in imgs])
# 同时存一张 png 供窗口图标使用
imgs[-1].save("resources/app_icon.png", format="PNG")
for s in sizes:
    try: os.remove("_icon_%d.png" % s)
    except OSError: pass
print("已生成 resources/app.ico", os.path.getsize("resources/app.ico"), "字节")
