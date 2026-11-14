#!/usr/bin/env python3
"""Render assets/fyrn-48.png, assets/fyrn-256.png and
assets/fyrn.png from assets/fyrn.svg.

Build-time helper, shipped in the zip: build_zip.py calls it when
the PNGs are missing, as in a fresh clone (the repo holds only the
SVG). Uses QSvgRenderer when available, else draws the same shapes
with QPainter. Verifies the PNGs by reading them back.
"""

import os
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def render_with_svg(app):
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    renderer = QSvgRenderer(
        os.path.join(ROOT, "assets", "fyrn.svg"))
    if not renderer.isValid():
        return False
    for size in (48, 256):
        image = QImage(size, size, QImage.Format_ARGB32)
        image.fill(0)
        painter = QPainter(image)
        renderer.render(painter)
        painter.end()
        out = os.path.join(ROOT, "assets",
                           "fyrn-%d.png" % size)
        image.save(out, "PNG")
    return True


def _flame_path(klass):
    """The flame teardrop, drawn with cubics (the SVG uses an arc
    for the round bottom; two cubics draw the same shape here)."""
    path = klass()
    path.moveTo(128, 64)
    path.cubicTo(146, 94, 166, 112, 166, 142)
    path.cubicTo(166, 164, 149, 180, 128, 180)
    path.cubicTo(107, 180, 90, 164, 90, 142)
    path.cubicTo(90, 112, 110, 94, 128, 64)
    path.closeSubpath()
    return path


def _inner_flame_path(klass):
    path = klass()
    path.moveTo(128, 108)
    path.cubicTo(138, 126, 148, 136, 148, 152)
    path.cubicTo(148, 164, 139, 172, 128, 172)
    path.cubicTo(117, 172, 108, 164, 108, 152)
    path.cubicTo(108, 136, 118, 126, 128, 108)
    path.closeSubpath()
    return path


def render_with_painter(app):
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import (QColor, QImage, QPainter, QPainterPath,
                               QPolygonF)
    for size in (48, 256):
        scale = size / 256.0
        image = QImage(size, size, QImage.Format_ARGB32)
        image.fill(0)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.scale(scale, scale)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#14202e"))
        painter.drawRoundedRect(QRectF(8, 8, 240, 240), 48, 48)
        painter.setBrush(QColor(232, 137, 12, 90))  # ember glow
        painter.drawEllipse(QRectF(50, 58, 156, 156))
        painter.setBrush(QColor("#f7b731"))
        painter.drawPath(_flame_path(QPainterPath))
        painter.setBrush(QColor("#ffd166"))
        painter.drawPath(_inner_flame_path(QPainterPath))
        painter.drawPolygon(QPolygonF([
            QPointF(192, 52), QPointF(196, 62),
            QPointF(206, 66), QPointF(196, 70),
            QPointF(192, 80), QPointF(188, 70),
            QPointF(178, 66), QPointF(188, 62)]))
        painter.end()
        out = os.path.join(ROOT, "assets",
                           "fyrn-%d.png" % size)
        image.save(out, "PNG")
    return True


def main():
    from PySide6.QtGui import QGuiApplication, QImage
    app = QGuiApplication(sys.argv)
    try:
        used = "svg" if render_with_svg(app) else "painter"
    except ImportError:
        render_with_painter(app)
        used = "painter"
    # The window icon is the 256 px render under its plain name.
    shutil.copyfile(
        os.path.join(ROOT, "assets", "fyrn-256.png"),
        os.path.join(ROOT, "assets", "fyrn.png"))
    ok = True
    for name, size in (("fyrn-48.png", 48),
                       ("fyrn-256.png", 256),
                       ("fyrn.png", 256)):
        path = os.path.join(ROOT, "assets", name)
        image = QImage(path)
        good = (not image.isNull() and image.width() == size
                and image.height() == size
                and os.path.getsize(path) > 100)
        print("%s: %dx%d, %d bytes, ok=%s"
              % (path, image.width(), image.height(),
                 os.path.getsize(path), good))
        ok = ok and good
    print("renderer: %s" % used)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
