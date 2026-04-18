from pathlib import Path
import tempfile
from PySide6 import QtCore, QtGui, QtWidgets
from agents.workflow import AgentWorkflow
try:
    from PySide6.QtWebEngineWidgets import QWebEngineView
except Exception:
    QWebEngineView = None
try:
    from PySide6.QtMultimedia import (QMediaCaptureSession, QAudioInput, QMediaRecorder, QMediaFormat, QMediaPlayer, QAudioOutput)
except ImportError:
    pass

from typing import Dict

ROOT = Path(__file__).resolve().parent.parent.parent
USE_WEBENGINE_MARKDOWN = False

class ThemeToggleBar(QtWidgets.QFrame):
    mode_changed = QtCore.Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("themeToggle")
        self.setFixedHeight(46)
        self._buttons: Dict[str, QtWidgets.QToolButton] = {}
        self._mode = "follow"

        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)

        self._pill = QtWidgets.QFrame(self)
        self._pill.setObjectName("themePill")
        self._pill.lower()
        self._anim = QtCore.QPropertyAnimation(self._pill, b"geometry", self)
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QtCore.QEasingCurve.OutCubic)

        for mode, icon, tip in (
            ("light", "☀", "浅色"),
            ("dark", "☾", "深色"),
            ("follow", "◐", "跟随系统"),
        ):
            btn = QtWidgets.QToolButton(self)
            btn.setObjectName("themeBtn")
            btn.setText(icon)
            btn.setToolTip(tip)
            btn.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
            btn.setFixedSize(48, 32)
            btn.clicked.connect(lambda _=False, m=mode: self.set_mode(m, emit=True))
            lay.addWidget(btn, 0, QtCore.Qt.AlignCenter)
            self._buttons[mode] = btn

        QtCore.QTimer.singleShot(0, lambda: self.set_mode("follow", emit=False, animate=False))

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self.set_mode(self._mode, emit=False, animate=False)

    def set_mode(self, mode: str, *, emit: bool, animate: bool = True) -> None:
        if mode not in self._buttons:
            return
        self._mode = mode
        for k, b in self._buttons.items():
            b.setProperty("active", k == mode)
            b.style().unpolish(b)
            b.style().polish(b)
        target = self._buttons[mode].geometry().adjusted(-2, -1, 2, 1)
        if animate:
            self._anim.stop()
            self._anim.setStartValue(self._pill.geometry())
            self._anim.setEndValue(target)
            self._anim.start()
        else:
            self._pill.setGeometry(target)
        if emit:
            self.mode_changed.emit(mode)

    @property
    def mode(self) -> str:
        return self._mode


class DropZone(QtWidgets.QFrame):
    file_dropped = QtCore.Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setObjectName("dropZone")
        self.setCursor(QtCore.Qt.ArrowCursor)

        self._prompt_input = QtWidgets.QTextEdit(self)
        self._prompt_input.setObjectName("workbenchPromptInput")
        self._prompt_input.setPlaceholderText("发给Solver的补充说明（可选）")
        self._prompt_input.setAcceptRichText(False)
        self._prompt_input.setMinimumHeight(140)

        self._hint_label = QtWidgets.QLabel("支持粘贴 / 拖拽 / 上传图片")
        self._hint_label.setObjectName("dropZoneHint")

        self._label = QtWidgets.QLabel("未选择题目图片")
        self._label.setObjectName("dropZoneFileName")
        self._label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)

        self._upload_btn = QtWidgets.QPushButton("上传图片")
        self._upload_btn.setObjectName("secondaryButton")
        self._upload_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self._upload_btn.clicked.connect(self._open_file_dialog)

        footer = QtWidgets.QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(8)
        footer.addWidget(self._hint_label)
        footer.addStretch(1)
        footer.addWidget(self._upload_btn)

        info = QtWidgets.QHBoxLayout()
        info.setContentsMargins(0, 0, 0, 0)
        info.setSpacing(8)
        info.addWidget(self._label, 1)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        layout.addWidget(self._prompt_input)
        layout.addLayout(info)
        layout.addLayout(footer)

    def _open_file_dialog(self) -> None:
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "选择题目图片",
            str(ROOT),
            "Images (*.png *.jpg *.jpeg *.webp *.bmp)",
        )
        if file_path:
            self.file_dropped.emit(file_path)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        super().mousePressEvent(event)

    def dragEnterEvent(self, event: QtGui.QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QtGui.QDropEvent) -> None:
        urls = event.mimeData().urls()
        if not urls:
            return
        path = urls[0].toLocalFile()
        if path:
            self.file_dropped.emit(path)

    def set_filename(self, path: str | None) -> None:
        self._label.setText(Path(path).name if path else "未选择题目图片")

    def prompt_text(self) -> str:
        return self._prompt_input.toPlainText()

    def set_prompt_text(self, text: str) -> None:
        self._prompt_input.setPlainText(text or "")


class MessageCard(QtWidgets.QFrame):
    retry_clicked = QtCore.Signal()
    def __init__(self, stage: str, title: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("messageCard")
        self._expanded = False
        self._stage = stage
        self._title = title
        self._header_buttons: Dict[str, QtWidgets.QToolButton] = {}

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        header = QtWidgets.QHBoxLayout()
        header.setSpacing(8)

        icon_col = QtWidgets.QVBoxLayout()
        icon_col.setContentsMargins(0, 0, 0, 0)
        icon_col.setSpacing(4)

        self.icon_label = QtWidgets.QLabel(self._stage_short(stage))
        self.icon_label.setObjectName("agentIcon")
        self.icon_label.setAlignment(QtCore.Qt.AlignCenter)
        self.icon_label.setFixedSize(24, 24)
        icon_col.addWidget(self.icon_label, 0, QtCore.Qt.AlignHCenter)

        header.addLayout(icon_col)

        title_label = QtWidgets.QLabel(title)
        title_label.setObjectName("messageTitle")
        header.addWidget(title_label)

        header.addStretch(1)

        self.status_label = QtWidgets.QLabel("运行中")
        self.status_label.setObjectName("messageStatusRunning")
        header.addWidget(self.status_label)

        self.spinner = QtWidgets.QProgressBar()
        self.spinner.setObjectName("messageSpinner")
        self.spinner.setTextVisible(False)
        self.spinner.setRange(0, 0)
        self.spinner.setFixedWidth(56)
        self.spinner.setFixedHeight(8)
        header.addWidget(self.spinner)

        self.header_actions = QtWidgets.QHBoxLayout()
        self.header_actions.setContentsMargins(0, 0, 0, 0)
        self.header_actions.setSpacing(4)
        header.addLayout(self.header_actions)

        layout.addLayout(header)

        self.preview_label = QtWidgets.QLabel("等待执行")
        self.preview_label.setObjectName("messagePreview")
        self.preview_label.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
        self.preview_label.setWordWrap(True)
        self.preview_label.setFixedHeight(42)
        layout.addWidget(self.preview_label)

        self.fade_mask = QtWidgets.QFrame()
        self.fade_mask.setObjectName("fadeMask")
        self.fade_mask.setFixedHeight(18)
        layout.addWidget(self.fade_mask)

        self._raw_markdown = ""
        self._web_content_height = 200
        self._web_dirty = False
        is_dark = self._detect_dark()
        if QWebEngineView is not None and USE_WEBENGINE_MARKDOWN:
            self.full_web = QWebEngineView()
            self.full_web.setObjectName("messageWeb")
            sp = self.full_web.sizePolicy()
            sp.setVerticalPolicy(QtWidgets.QSizePolicy.Fixed)
            self.full_web.setSizePolicy(sp)
            self.full_web.setFixedHeight(0)
            # Opaque background to prevent ghost/residual rendering
            bg_color = QtGui.QColor("#1f2937") if is_dark else QtGui.QColor("#ffffff")
            self.full_web.page().setBackgroundColor(bg_color)
            self.full_web.loadFinished.connect(self._on_web_load_finished)
            layout.addWidget(self.full_web)
            self.full_text = None
        else:
            self.full_text = QtWidgets.QTextBrowser()
            self.full_text.setObjectName("messageFull")
            self.full_text.setOpenExternalLinks(False)
            self.full_text.setMarkdown("")
            self.full_text.setVisible(False)
            self.full_text.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
            self.full_text.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
            self.full_text.setFrameShape(QtWidgets.QFrame.NoFrame)
            layout.addWidget(self.full_text)
            self.full_web = None

        divider = QtWidgets.QHBoxLayout()
        line_l = QtWidgets.QFrame()
        line_l.setFrameShape(QtWidgets.QFrame.HLine)
        line_l.setObjectName("dividerLine")
        line_r = QtWidgets.QFrame()
        line_r.setFrameShape(QtWidgets.QFrame.HLine)
        line_r.setObjectName("dividerLine")
        self.toggle_btn = QtWidgets.QPushButton("展开")
        self.toggle_btn.setObjectName("expandButton")
        self.toggle_btn.clicked.connect(self._toggle)
        divider.addWidget(line_l, 1)
        divider.addWidget(self.toggle_btn)
        divider.addWidget(line_r, 1)
        layout.addLayout(divider)

    @staticmethod
    def _to_preview(content: str) -> str:
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        return "\n".join(lines[:2]) if lines else ""

    @staticmethod
    def _stage_short(stage: str) -> str:
        mapping = {"solver": "S", "deepseek": "Q", "architect": "A", "director": "D", "animator": "M", "coder": "C", "system": "I"}
        return mapping.get(stage, "?")

    def _detect_dark(self) -> bool:
        """Detect dark mode using MainWindow's theme setting."""
        w = self.window()
        if hasattr(w, '_is_dark_mode'):
            return w._is_dark_mode()
        return self.palette().color(QtGui.QPalette.Window).lightness() < 128

    def set_content(self, content: str) -> None:
        self._raw_markdown = content or ""
        # Create a fast plain text preview skipping markdown tags and huge blocks
        plain_lines = []
        for line in self._raw_markdown.splitlines():
            s = line.strip()
            if s and not s.startswith("```") and not s.startswith("#") and not s.startswith("="):
                plain_lines.append(s)
                if len(plain_lines) >= 2:
                    break
        
        self.preview_label.setText("\n".join(plain_lines) if plain_lines else "（空内容）")
        
        if self.full_text is not None:
            self.full_text.setMarkdown(content or "")
        # Pre-render web content immediately so expand does not suffer lazy-load lag.
        if self.full_web is not None:
            self._web_dirty = True
            self._load_web_content()

    def _load_web_content(self) -> None:
        """Load HTML into the web view."""
        if self.full_web is None or not self._web_dirty:
            return
        is_dark = self._detect_dark()
        bg_color = QtGui.QColor("#1f2937") if is_dark else QtGui.QColor("#ffffff")
        self.full_web.page().setBackgroundColor(bg_color)
        self._web_dirty = False
        self.full_web.setHtml(self._build_math_html(self._raw_markdown, is_dark=is_dark))
        self.full_web.update()

    def set_status(self, status: str) -> None:
        if status == "running":
            self.status_label.setText("运行中")
            self.status_label.setObjectName("messageStatusRunning")
            self.spinner.setVisible(True)
            if self.preview_label.text().strip() in {"", "等待执行", "处理中..."}:
                self.preview_label.setText("处理中...")
        elif status == "done":
            self.status_label.setText("完成")
            self.status_label.setObjectName("messageStatusDone")
            self.spinner.setVisible(False)
        else:
            self.status_label.setText("失败")
            self.status_label.setObjectName("messageStatusError")
            self.spinner.setVisible(False)
            if not self.preview_label.text().strip():
                self.preview_label.setText("执行失败")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def set_activity(self, text: str) -> None:
        message = (text or "").strip()
        if not message:
            message = "处理中..."
        self.preview_label.setText(message)

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        if self.full_text is not None:
            self.full_text.setVisible(self._expanded)
            if self._expanded:
                self._resize_text_browser()
        if self.full_web is not None:
            if self._expanded:
                # Give the web view a real viewport first; content is pre-rendered in set_content.
                self.full_web.setFixedHeight(self._web_content_height)
                self._load_web_content()
                self._schedule_web_measurements()
            else:
                self.full_web.setFixedHeight(0)
        self.preview_label.setVisible(not self._expanded)
        self.fade_mask.setVisible(not self._expanded)
        self.toggle_btn.setText("收起" if self._expanded else "展开")
        if self.parent() and self.parent().layout():
            self.parent().layout().activate()
        QtCore.QTimer.singleShot(50, self._force_scroll_update)

    def _on_web_load_finished(self, ok: bool) -> None:
        if not ok or self.full_web is None:
            return
        if not self._expanded:
            return
        self._schedule_web_measurements()

    def _schedule_web_measurements(self) -> None:
        """Measure height after load, and again later for MathJax."""
        QtCore.QTimer.singleShot(100, self._measure_web_height)
        QtCore.QTimer.singleShot(1000, self._measure_web_height)
        QtCore.QTimer.singleShot(2500, self._measure_web_height)

    def _measure_web_height(self) -> None:
        if self.full_web is None or not self._expanded:
            return
        js = """
        (function() {
            var body = document.body;
            var html = document.documentElement;
            return Math.max(
                body.scrollHeight, body.offsetHeight,
                html.offsetHeight, html.scrollHeight
            );
        })()
        """
        self.full_web.page().runJavaScript(js, self._apply_web_height)

    def _apply_web_height(self, height) -> None:
        if not height or self.full_web is None or not self._expanded:
            return
        new_h = int(height) + 20
        self._web_content_height = new_h
        if abs(self.full_web.height() - new_h) > 5:
            self.full_web.setFixedHeight(new_h)
            self.full_web.updateGeometry()
            self.updateGeometry()
            self._force_scroll_update()

    def _force_scroll_update(self) -> None:
        # Find the QScrollArea that likely contains us
        p = self.parent()
        while p:
            if isinstance(p, QtWidgets.QScrollArea):
                v = p.verticalScrollBar().value()
                p.verticalScrollBar().setValue(v + 1)
                p.verticalScrollBar().setValue(v)
                break
            p = p.parent()

    def _resize_text_browser(self) -> None:
        if self.full_text is None:
            return
        doc = self.full_text.document()
        doc.setTextWidth(self.full_text.viewport().width())
        h = int(doc.size().height()) + 32
        self.full_text.setFixedHeight(max(60, h))

    def add_retry_button(self) -> None:
        """Append a retry button below the expand/collapse divider."""
        if hasattr(self, "_retry_btn"):
            return
        self._retry_btn = QtWidgets.QPushButton("重新执行 Coder")
        self._retry_btn.setObjectName("runButton")
        self._retry_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self._retry_btn.setFixedHeight(36)
        self._retry_btn.clicked.connect(self.retry_clicked.emit)
        self.layout().addWidget(self._retry_btn)

    def add_header_icon_button(self, key: str, tooltip: str, callback, icon_text: str = "↻") -> None:
        btn = self._header_buttons.get(key)
        if btn is None:
            btn = QtWidgets.QToolButton(self)
            btn.setObjectName("headerIconButton")
            btn.setText(icon_text)
            btn.setCursor(QtCore.Qt.PointingHandCursor)
            btn.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
            btn.setFixedSize(24, 24)
            self.header_actions.addWidget(btn)
            self._header_buttons[key] = btn
        else:
            try:
                btn.clicked.disconnect()
            except Exception:
                pass
            btn.setText(icon_text)
        btn.setToolTip(tooltip)
        btn.clicked.connect(callback)

    def add_action_button(self, key: str, text: str, callback) -> None:
        if not hasattr(self, "_action_buttons"):
            self._action_buttons = {}
        btn = self._action_buttons.get(key)
        if btn is None:
            btn = QtWidgets.QPushButton(text)
            btn.setObjectName("runButton")
            btn.setCursor(QtCore.Qt.PointingHandCursor)
            btn.setFixedHeight(36)
            self.layout().addWidget(btn)
            self._action_buttons[key] = btn
        else:
            try:
                btn.clicked.disconnect()
            except Exception:
                pass
            btn.setText(text)
        btn.clicked.connect(callback)

    def remove_action_button(self, key: str) -> None:
        if not hasattr(self, "_action_buttons"):
            return
        btn = self._action_buttons.pop(key, None)
        if btn is None:
            return
        try:
            btn.clicked.disconnect()
        except Exception:
            pass
        btn.setParent(None)
        btn.deleteLater()

    @staticmethod
    def _markdown_to_html(content: str) -> str:
        try:
            import markdown
            return markdown.markdown(content or "", extensions=['fenced_code', 'codehilite', 'tables', 'nl2br', 'sane_lists'])
        except ImportError:
            doc = QtGui.QTextDocument()
            doc.setMarkdown(content or "")
            return doc.toHtml()

    def _build_math_html(self, markdown_text: str, *, is_dark: bool = False) -> str:
        body = self._markdown_to_html(markdown_text)
        if is_dark:
            bg_color = "#1f2937"
            text_color = "#e2e8f0"
            pre_bg = "#111827"
            pre_border = "#334155"
            code_bg = "#111827"
        else:
            bg_color = "#ffffff"
            text_color = "#111827"
            pre_bg = "#f8fafc"
            pre_border = "#e5e7eb"
            code_bg = "#f8fafc"
        return f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <style>
    html {{
      overflow: hidden;
    }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, 'Open Sans', 'Helvetica Neue', sans-serif;
      font-size: 14px;
      line-height: 1.6;
      color: {text_color};
      background-color: {bg_color};
      margin: 8px;
    }}
    pre {{
      background: {pre_bg};
      border: 1px solid {pre_border};
      border-radius: 8px;
      padding: 10px;
      overflow-x: auto;
      line-height: 125%;
      contain: paint;
      transform: translateZ(0);
    }}
    code {{
      font-family: Consolas, 'Courier New', monospace;
    }}
    /* Pygments syntax highlighting */
    td.linenos .normal {{ color: inherit; background-color: transparent; padding-left: 5px; padding-right: 5px; }}
    span.linenos {{ color: inherit; background-color: transparent; padding-left: 5px; padding-right: 5px; }}
    td.linenos .special {{ color: #000000; background-color: #ffffc0; padding-left: 5px; padding-right: 5px; }}
    span.linenos.special {{ color: #000000; background-color: #ffffc0; padding-left: 5px; padding-right: 5px; }}
    .codehilite .hll {{ background-color: #ffffcc }}
    .codehilite {{
      background: {code_bg};
      overflow-x: auto;
      contain: paint;
      transform: translateZ(0);
    }}
    .codehilite .c {{ color: #3D7B7B; font-style: italic }}
    .codehilite .err {{ border: 1px solid #F00 }}
    .codehilite .k {{ color: #008000; font-weight: bold }}
    .codehilite .o {{ color: #666 }}
    .codehilite .ch {{ color: #3D7B7B; font-style: italic }}
    .codehilite .cm {{ color: #3D7B7B; font-style: italic }}
    .codehilite .cp {{ color: #9C6500 }}
    .codehilite .cpf {{ color: #3D7B7B; font-style: italic }}
    .codehilite .c1 {{ color: #3D7B7B; font-style: italic }}
    .codehilite .cs {{ color: #3D7B7B; font-style: italic }}
    .codehilite .gd {{ color: #A00000 }}
    .codehilite .ge {{ font-style: italic }}
    .codehilite .ges {{ font-weight: bold; font-style: italic }}
    .codehilite .gr {{ color: #E40000 }}
    .codehilite .gh {{ color: #000080; font-weight: bold }}
    .codehilite .gi {{ color: #008400 }}
    .codehilite .go {{ color: #717171 }}
    .codehilite .gp {{ color: #000080; font-weight: bold }}
    .codehilite .gs {{ font-weight: bold }}
    .codehilite .gu {{ color: #800080; font-weight: bold }}
    .codehilite .gt {{ color: #04D }}
    .codehilite .kc {{ color: #008000; font-weight: bold }}
    .codehilite .kd {{ color: #008000; font-weight: bold }}
    .codehilite .kn {{ color: #008000; font-weight: bold }}
    .codehilite .kp {{ color: #008000 }}
    .codehilite .kr {{ color: #008000; font-weight: bold }}
    .codehilite .kt {{ color: #B00040 }}
    .codehilite .m {{ color: #666 }}
    .codehilite .s {{ color: #BA2121 }}
    .codehilite .na {{ color: #687822 }}
    .codehilite .nb {{ color: #008000 }}
    .codehilite .nc {{ color: #00F; font-weight: bold }}
    .codehilite .no {{ color: #800 }}
    .codehilite .nd {{ color: #A2F }}
    .codehilite .ni {{ color: #717171; font-weight: bold }}
    .codehilite .ne {{ color: #CB3F38; font-weight: bold }}
    .codehilite .nf {{ color: #00F }}
    .codehilite .nl {{ color: #767600 }}
    .codehilite .nn {{ color: #00F; font-weight: bold }}
    .codehilite .nt {{ color: #008000; font-weight: bold }}
    .codehilite .nv {{ color: #19177C }}
    .codehilite .ow {{ color: #A2F; font-weight: bold }}
    .codehilite .w {{ color: #BBB }}
    .codehilite .mb {{ color: #666 }}
    .codehilite .mf {{ color: #666 }}
    .codehilite .mh {{ color: #666 }}
    .codehilite .mi {{ color: #666 }}
    .codehilite .mo {{ color: #666 }}
    .codehilite .sa {{ color: #BA2121 }}
    .codehilite .sb {{ color: #BA2121 }}
    .codehilite .sc {{ color: #BA2121 }}
    .codehilite .dl {{ color: #BA2121 }}
    .codehilite .sd {{ color: #BA2121; font-style: italic }}
    .codehilite .s2 {{ color: #BA2121 }}
    .codehilite .se {{ color: #AA5D1F; font-weight: bold }}
    .codehilite .sh {{ color: #BA2121 }}
    .codehilite .si {{ color: #A45A77; font-weight: bold }}
    .codehilite .sx {{ color: #008000 }}
    .codehilite .sr {{ color: #A45A77 }}
    .codehilite .s1 {{ color: #BA2121 }}
    .codehilite .ss {{ color: #19177C }}
    .codehilite .bp {{ color: #008000 }}
    .codehilite .fm {{ color: #00F }}
    .codehilite .vc {{ color: #19177C }}
    .codehilite .vg {{ color: #19177C }}
    .codehilite .vi {{ color: #19177C }}
    .codehilite .vm {{ color: #19177C }}
    .codehilite .il {{ color: #666 }}
  </style>
  <script>
    window.MathJax = {{
      tex: {{ inlineMath: [['$','$'], ['\\\\(','\\\\)']], displayMath: [['$$','$$'], ['\\\\[','\\\\]']] }},
      svg: {{ fontCache: 'global' }}
    }};
  </script>
  <script>
    (function() {{
      function forceRepaint() {{
        document.body.style.transform = 'translateZ(0)';
        requestAnimationFrame(function() {{
          document.body.style.transform = '';
        }});
      }}

      function bindScrollableRepaint() {{
        var nodes = document.querySelectorAll('pre, .codehilite');
        nodes.forEach(function(node) {{
          if (node.dataset.repaintBound === '1') {{
            return;
          }}
          node.dataset.repaintBound = '1';
          node.addEventListener('scroll', forceRepaint, {{ passive: true }});
          node.addEventListener('pointerup', forceRepaint);
          node.addEventListener('mouseup', forceRepaint);
        }});
      }}

      document.addEventListener('DOMContentLoaded', function() {{
        bindScrollableRepaint();
        setTimeout(bindScrollableRepaint, 300);
        setTimeout(bindScrollableRepaint, 1200);
      }});
    }})();
  </script>
  <script id="MathJax-script" async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js"></script>
</head>
<body>
{body}
</body>
</html>
"""


class AgentWorker(QtCore.QThread):
    finished = QtCore.Signal(dict)
    failed = QtCore.Signal(str)
    cancelled = QtCore.Signal(dict)
    coder_failed = QtCore.Signal(dict)  # emitted when coder fails but earlier stages ok
    progress = QtCore.Signal(str)
    stage_result = QtCore.Signal(str, str)

    def __init__(
        self,
        image_path: str | None,
        render_options: dict | None = None,
        resume_run_dir: str | None = None,
        resume_from_stage: str | None = None,
        stop_after_stage: str | None = None,
    ) -> None:
        super().__init__()
        self._image_path = image_path
        self._render_options = render_options or {}
        self._resume_run_dir = Path(resume_run_dir) if resume_run_dir else None
        self._resume_from_stage = resume_from_stage
        self._stop_after_stage = stop_after_stage
        self._workflow: AgentWorkflow | None = None

    def current_run_dir(self) -> Path | None:
        if self._resume_run_dir is not None:
            return self._resume_run_dir
        if self._workflow is not None and getattr(self._workflow, "current_run_dir", None) is not None:
            return self._workflow.current_run_dir
        return None

    def continue_payload(self) -> dict:
        run_dir = self.current_run_dir()
        return {
            "run_dir": str(run_dir) if run_dir is not None else "",
            "render_options": self._render_options,
            "resume_from_stage": self._resume_from_stage,
            "stop_after_stage": self._stop_after_stage,
        }

    def request_cancel(self) -> None:
        if self._workflow is not None:
            self._workflow.cancel()

    def force_terminate(self) -> None:
        if self.isRunning():
            self.terminate()
            self.wait(1500)

    def run(self) -> None:
        try:
            workflow = AgentWorkflow()
            self._workflow = workflow
            if self._resume_run_dir is not None:
                if self._resume_from_stage:
                    self.progress.emit(f"继续运行：从 {self._resume_from_stage} 开始")
                outputs = workflow.continue_run(
                    run_dir=self._resume_run_dir,
                    on_progress=lambda msg: self.progress.emit(msg),
                    on_stage_result=lambda stage, content: self.stage_result.emit(stage, content),
                    render_options=self._render_options,
                    resume_from_stage=self._resume_from_stage,
                    stop_after_stage=self._stop_after_stage,
                )
            else:
                outputs = workflow.run(
                    Path(self._image_path) if self._image_path else None,
                    on_progress=lambda msg: self.progress.emit(msg),
                    on_stage_result=lambda stage, content: self.stage_result.emit(stage, content),
                    render_options=self._render_options,
                )
            if outputs.coder_failed:
                self.coder_failed.emit({
                    "director_plan": outputs.director_plan,
                    "animator_plan": outputs.animator_plan,
                    "run_dir": str(outputs.run_dir),
                    "render_options": self._render_options,
                    "coder_output": outputs.coder_output,
                })
            else:
                self.finished.emit(
                    {
                        "solver": outputs.solver_answer,
                        "architect": outputs.architect_code,
                        "director": outputs.director_plan,
                        "animator": outputs.animator_plan,
                        "coder": outputs.coder_output,
                        "run_dir": str(outputs.run_dir),
                        "render_options": self._render_options,
                    }
                )
        except Exception as exc:
            if "运行已取消" in str(exc):
                self.cancelled.emit(self.continue_payload())
            else:
                self.failed.emit(str(exc))
        finally:
            self._workflow = None


class CoderRetryWorker(QtCore.QThread):
    """Re-runs only the coder step using preserved earlier outputs."""
    finished = QtCore.Signal(dict)
    failed = QtCore.Signal(str)
    cancelled = QtCore.Signal(dict)
    coder_failed = QtCore.Signal(dict)
    progress = QtCore.Signal(str)
    stage_result = QtCore.Signal(str, str)

    def __init__(self, director_plan: str, animator_plan: str, render_options: dict, run_dir: str) -> None:
        super().__init__()
        self._director_plan = director_plan
        self._animator_plan = animator_plan
        self._render_options = render_options
        self._run_dir = Path(run_dir)
        self._workflow: AgentWorkflow | None = None

    def current_run_dir(self) -> Path:
        return self._run_dir

    def continue_payload(self) -> dict:
        return {
            "run_dir": str(self._run_dir),
            "render_options": self._render_options,
        }

    def request_cancel(self) -> None:
        if self._workflow is not None:
            self._workflow.cancel()

    def force_terminate(self) -> None:
        if self.isRunning():
            self.terminate()
            self.wait(1500)

    def run(self) -> None:
        try:
            workflow = AgentWorkflow()
            self._workflow = workflow
            outputs = workflow.rerun_coder(
                animator_plan=self._animator_plan,
                director_plan=self._director_plan,
                render_options=self._render_options,
                run_dir=self._run_dir,
                on_progress=lambda msg: self.progress.emit(msg),
                on_stage_result=lambda stage, content: self.stage_result.emit(stage, content),
            )
            if outputs.coder_failed:
                self.coder_failed.emit({
                    "director_plan": self._director_plan,
                    "animator_plan": self._animator_plan,
                    "run_dir": str(self._run_dir),
                    "render_options": self._render_options,
                    "coder_output": outputs.coder_output,
                })
            else:
                self.finished.emit({
                    "solver": outputs.solver_answer,
                    "architect": outputs.architect_code,
                    "director": outputs.director_plan,
                    "animator": outputs.animator_plan,
                    "coder": outputs.coder_output,
                    "run_dir": str(outputs.run_dir),
                    "render_options": self._render_options,
                })
        except Exception as exc:
            if "运行已取消" in str(exc):
                self.cancelled.emit(self.continue_payload())
            else:
                self.failed.emit(str(exc))
        finally:
            self._workflow = None


class InlineRenameWidget(QtWidgets.QStackedWidget):
    rename_requested = QtCore.Signal(str)

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.lbl = QtWidgets.QLabel(text)
        self.lbl.setStyleSheet("background: transparent; font-size: 13px; font-weight: 500;")

        self.edit = QtWidgets.QLineEdit(text)
        self.edit.setStyleSheet("font-size: 13px; font-weight: 500; border: none; background: transparent; padding: 0px;")

        self.addWidget(self.lbl)
        self.addWidget(self.edit)
        self.setCurrentWidget(self.lbl)

        self.lbl.mouseDoubleClickEvent = self._on_double_click
        self.edit.editingFinished.connect(self._on_edit_finished)

    def _on_double_click(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self.start_edit()

    def start_edit(self):
        self.edit.setText(self.lbl.text())
        self.setCurrentWidget(self.edit)
        self.edit.setFocus()
        self.edit.selectAll()

    def _on_edit_finished(self):
        if self.currentWidget() != self.edit:
            return
        new_text = self.edit.text().strip()
        self.setCurrentWidget(self.lbl)
        if new_text and new_text != self.lbl.text():
            self.rename_requested.emit(new_text)

class InlinePromptTextWidget(QtWidgets.QStackedWidget):
    text_changed = QtCore.Signal(str)

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.lbl = QtWidgets.QLabel(text if text else "写出参考音频原文，复刻会更像")
        self.lbl.setStyleSheet("background: transparent; font-size: 11px; color: #888888; font-weight: normal; padding-left: 10px;")
        self.lbl.setCursor(QtCore.Qt.IBeamCursor)
        
        self._current_text = text
        self.edit = QtWidgets.QLineEdit(text)
        self.edit.setPlaceholderText("写出参考音频原文，复刻会更像")
        self.edit.setStyleSheet("font-size: 11px; font-weight: normal; border: 1px solid rgba(150,150,150,0.3); border-radius: 4px; background: transparent; padding: 2px;")
        
        self.addWidget(self.lbl)
        self.addWidget(self.edit)
        self.setCurrentWidget(self.lbl)
        
        self.lbl.mouseDoubleClickEvent = self._on_double_click
        self.edit.editingFinished.connect(self._on_edit_finished)
        
    def _on_double_click(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self.start_edit()
            
    def start_edit(self):
        self.edit.setText(self._current_text)
        self.setCurrentWidget(self.edit)
        self.edit.setFocus()
        self.edit.selectAll()
        
    def _on_edit_finished(self):
        if self.currentWidget() != self.edit:
            return
        new_text = self.edit.text().strip()
        self._current_text = new_text
        self.lbl.setText(new_text if new_text else "写出参考音频原文，复刻会更像")
        self.setCurrentWidget(self.lbl)
        self.text_changed.emit(new_text)

class AudioItemWidget(QtWidgets.QWidget):
    play_requested = QtCore.Signal(str)
    rename_requested = QtCore.Signal(str, str)

    def __init__(self, file_path: Path, show_prompt: bool = False, parent=None):
        super().__init__(parent)
        self.file_path = str(file_path)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        top_h = QtWidgets.QHBoxLayout()
        top_h.setContentsMargins(0, 0, 0, 0)

        self.play_btn = QtWidgets.QPushButton("▶")
        self.play_btn.setFixedSize(22, 22)
        self.play_btn.setStyleSheet("border: none; background: transparent; color: #1a73e8; font-size: 14px;")
        self.play_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.play_btn.clicked.connect(self._on_play_clicked)

        self.title_lbl = InlineRenameWidget(file_path.stem)
        self.title_lbl.rename_requested.connect(self._on_rename_requested)

        top_h.addWidget(self.play_btn)
        top_h.addWidget(self.title_lbl)

        if show_prompt:
            prompt_path = file_path.with_suffix(".txt")
            init_prompt = prompt_path.read_text(encoding="utf-8").strip() if prompt_path.exists() else ""
            self.prompt_widget = InlinePromptTextWidget(init_prompt)
            self.prompt_widget.text_changed.connect(self._on_prompt_changed)
            top_h.addWidget(self.prompt_widget)

        top_h.addStretch(1)

        layout.addLayout(top_h)

        self.progress = QtWidgets.QProgressBar()
        self.progress.setFixedHeight(4)
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setStyleSheet("""
            QProgressBar { 
                background-color: rgba(150, 150, 150, 0.2); 
                border: none; 
                border-radius: 2px; 
            } 
            QProgressBar::chunk { 
                background-color: #1a73e8; 
                border-radius: 2px; 
            }
        """)
        layout.addWidget(self.progress)

        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        self.setStyleSheet("AudioItemWidget { background: transparent; }")

    def _on_rename_requested(self, new_name: str):
        self.rename_requested.emit(self.file_path, new_name)

    def _on_prompt_changed(self, new_text: str):
        try:
            prompt_path = Path(self.file_path).with_suffix(".txt")
            if new_text:
                prompt_path.write_text(new_text, encoding="utf-8")
            elif prompt_path.exists():
                prompt_path.unlink()
        except Exception as e:
            pass

    def start_rename(self):
        self.title_lbl.start_edit()

    def _on_play_clicked(self):
        self.play_requested.emit(self.file_path)

    def set_playing_state(self, playing: bool):
        self.play_btn.setText("⏸" if playing else "▶")
        if not playing:
            self.progress.setValue(0)

    def set_progress(self, pos: int, duration: int):
        if duration > 0:
            self.progress.setValue(int((pos / duration) * 100))
        else:
            self.progress.setValue(0)

class VoiceRecordDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("录制新音色")
        self.setFixedSize(360, 160)
        self.setModal(True)
        # Use simple standard styles
        self.setStyleSheet("")

        layout = QtWidgets.QVBoxLayout(self)

        self.name_input = QtWidgets.QLineEdit()
        self.name_input.setPlaceholderText("请输入新音色的名称（无需扩展名）")
        self.name_input.setMaxLength(30)
        layout.addWidget(self.name_input)

        self.status_label = QtWidgets.QLabel("状态：准备就绪")
        layout.addWidget(self.status_label)

        btn_layout = QtWidgets.QHBoxLayout()
        self.start_btn = QtWidgets.QPushButton("开始录音")
        self.stop_btn = QtWidgets.QPushButton("停止录音")
        self.stop_btn.setEnabled(False)
        self.cancel_btn = QtWidgets.QPushButton("取消")

        btn_layout.addWidget(self.start_btn)
        btn_layout.addWidget(self.stop_btn)
        btn_layout.addWidget(self.cancel_btn)
        layout.addLayout(btn_layout)

        try:
            self.session = QMediaCaptureSession()
            self.audio_input = QAudioInput()
            self.session.setAudioInput(self.audio_input)

            self.recorder = QMediaRecorder()
            self.session.setRecorder(self.recorder)

            fmt = QMediaFormat()
            fmt.setFileFormat(QMediaFormat.Wave)
            self.recorder.setMediaFormat(fmt)

            self.temp_file = Path(tempfile.gettempdir()) / "filaglyph_temp_record.wav"
            if self.temp_file.exists():
                try:
                    self.temp_file.unlink()
                except Exception:
                    pass

            self.recorder.setOutputLocation(QtCore.QUrl.fromLocalFile(str(self.temp_file)))
        except NameError:
            self.status_label.setText("状态：缺少 QtMultimedia，无法录制音频")
            self.start_btn.setEnabled(False)

        self.start_btn.clicked.connect(self._start_recording)
        self.stop_btn.clicked.connect(self._stop_recording)
        self.cancel_btn.clicked.connect(self.reject)

    def _start_recording(self):
        if not self.name_input.text().strip():
            QtWidgets.QMessageBox.warning(self, "提示", "请先输入音色名称。")
            return
        if not hasattr(self, "recorder"):
            return
        self.recorder.record()
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.name_input.setEnabled(False)
        self.status_label.setText("状态：正在录音...")

    def _stop_recording(self):
        if not hasattr(self, "recorder"):
            return
        self.recorder.stop()
        self.status_label.setText("状态：录音完成")
        self.accept()

    def get_result(self) -> tuple[str, Path] | None:
        if self.result() == QtWidgets.QDialog.Accepted and hasattr(self, "temp_file") and self.temp_file.exists():
            return self.name_input.text().strip(), self.temp_file
        return None


class VideoCard(QtWidgets.QFrame):
    def __init__(self, video_path: str, parent=None):
        super().__init__(parent)
        self.setObjectName("messageCard")
        self.video_path = video_path

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        header = QtWidgets.QHBoxLayout()
        icon = QtWidgets.QLabel("V")
        icon.setObjectName("agentIcon")
        icon.setFixedSize(24, 24)
        icon.setAlignment(QtCore.Qt.AlignCenter)
        header.addWidget(icon)

        title = QtWidgets.QLabel("视频生成成功")
        title.setObjectName("messageTitle")
        header.addWidget(title)
        header.addStretch(1)
        layout.addLayout(header)

        self.path_label = QtWidgets.QLabel(f"视频文件：{Path(video_path).name}")
        self.path_label.setObjectName("messagePreview")
        self.path_label.setFixedHeight(20)
        layout.addWidget(self.path_label)

        actions = QtWidgets.QHBoxLayout()
        actions.setSpacing(10)

        self.play_btn = QtWidgets.QPushButton("立即播放动画")
        self.play_btn.setObjectName("runButton")
        self.play_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.play_btn.setFixedHeight(40)
        self.play_btn.clicked.connect(self._play_video)
        actions.addWidget(self.play_btn, 1)

        self.folder_btn = QtWidgets.QPushButton("打开所在位置")
        self.folder_btn.setObjectName("secondaryButton")
        self.folder_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.folder_btn.setFixedHeight(40)
        self.folder_btn.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_DirOpenIcon))
        self.folder_btn.clicked.connect(self._open_folder)
        actions.addWidget(self.folder_btn)

        layout.addLayout(actions)

    def _play_video(self):
        url = QtCore.QUrl.fromLocalFile(self.video_path)
        QtGui.QDesktopServices.openUrl(url)

    def _open_folder(self):
        folder = str(Path(self.video_path).resolve().parent)
        url = QtCore.QUrl.fromLocalFile(folder)
        QtGui.QDesktopServices.openUrl(url)

