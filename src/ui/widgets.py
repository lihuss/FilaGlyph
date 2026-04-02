from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets
try:
    from PySide6.QtWebEngineWidgets import QWebEngineView
except Exception:
    QWebEngineView = None

try:
    from PySide6.QtMultimedia import (
        QMediaCaptureSession, QAudioInput, QMediaRecorder, QMediaFormat,
        QMediaPlayer, QAudioOutput
    )
except ImportError:
    pass

import tempfile

class ThemeToggleBar(QtWidgets.QFrame):
    # (copied later)
    pass
