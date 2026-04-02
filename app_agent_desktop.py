from __future__ import annotations

import sys
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict

from datetime import datetime
from PySide6 import QtCore, QtGui, QtWidgets
try:
    from PySide6.QtWebEngineWidgets import QWebEngineView
except Exception:  # pragma: no cover
    QWebEngineView = None
try:
    from PySide6.QtMultimedia import (
        QMediaCaptureSession, QAudioInput, QMediaRecorder, QMediaFormat,
        QMediaPlayer, QAudioOutput
    )
except ImportError:
    pass
import tempfile

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
PYONEDARK_ROOT = ROOT / "third_party" / "PyOneDark_Qt_Widgets_Modern_GUI"
if PYONEDARK_ROOT.exists() and str(PYONEDARK_ROOT) not in sys.path:
    sys.path.insert(0, str(PYONEDARK_ROOT))

from agents.config import infer_provider, load_agent_config_json, save_agent_config_json
from agents.workflow import AgentWorkflow
from ui.palette import get_theme_palette

try:
    from gui.widgets import PyLineEdit, PyPushButton, PyTitleBar
    from gui.core.functions import Functions as PODFunctions
    from gui.core.json_settings import Settings as PODSettings
    from gui.core.json_themes import Themes as PODThemes

    HAS_PYONEDARK = True
except Exception:
    PyLineEdit = None  # type: ignore[assignment]
    PyPushButton = None  # type: ignore[assignment]
    PyTitleBar = None  # type: ignore[assignment]
    PODFunctions = None  # type: ignore[assignment]
    PODSettings = None  # type: ignore[assignment]
    PODThemes = None  # type: ignore[assignment]
    HAS_PYONEDARK = False


def _patch_pyonedark_resource_paths() -> None:
    if not HAS_PYONEDARK or PODFunctions is None:
        return
    base = (PYONEDARK_ROOT / "gui" / "images").resolve()
    svg_icons = (base / "svg_icons").resolve()
    svg_images = (base / "svg_images").resolve()
    images = (base / "images").resolve()

    PODFunctions.set_svg_icon = staticmethod(lambda icon_name: str((svg_icons / icon_name).resolve()))  # type: ignore[assignment]
    PODFunctions.set_svg_image = staticmethod(lambda icon_name: str((svg_images / icon_name).resolve()))  # type: ignore[assignment]
    PODFunctions.set_image = staticmethod(lambda image_name: str((images / image_name).resolve()))  # type: ignore[assignment]


_patch_pyonedark_resource_paths()

VISION_ROLES = {"solver", "architect", "director"}
ROLE_NAMES = ("solver", "architect", "director", "coder")
PROVIDER_BASE_URL = {
    "openai": "https://api.openai.com/v1",
    "gemini": "",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "deepseek": "https://api.deepseek.com/v1",
}
UI_SETTINGS_PATH = ROOT / "config" / "ui_settings.json"
PYONEDARK_SETTINGS_PATH = ROOT / "config" / "settings.json"
BUNDLED_FONT_PATHS = [
    ROOT / "assets" / "fonts" / "SourceHanSansSC-Regular.otf",
]


def _patch_pyonedark_settings_path() -> None:
    if not HAS_PYONEDARK or PODSettings is None:
        return
    PODSettings.json_file = PYONEDARK_SETTINGS_PATH.name
    PODSettings.app_path = str(PYONEDARK_SETTINGS_PATH.parent.resolve())
    PODSettings.settings_path = str(PYONEDARK_SETTINGS_PATH.resolve())


_patch_pyonedark_settings_path()


from ui.components import ThemeToggleBar, DropZone, MessageCard, AgentWorker, CoderRetryWorker, InlineRenameWidget, AudioItemWidget, VoiceRecordDialog, VideoCard


class ClipboardImageSaveWorker(QtCore.QThread):
    finished_save = QtCore.Signal(str, bool, str)

    def __init__(self, image: QtGui.QImage, out_path: Path, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        # Copy image data so the worker is independent from clipboard lifetime.
        self._image = QtGui.QImage(image)
        self._out_path = out_path

    def run(self) -> None:
        ok = False
        error = ""
        try:
            if self._image.isNull():
                error = "剪贴板图像为空"
            else:
                self._out_path.parent.mkdir(parents=True, exist_ok=True)
                ok = self._image.save(str(self._out_path), "PNG")
                if not ok:
                    error = "保存 PNG 失败"
        except Exception as exc:
            error = str(exc)
        self.finished_save.emit(str(self._out_path), ok, error)

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self._role_controls: dict[str, dict[str, QtWidgets.QWidget]] = {}
        self._workflow: AgentWorkflow | None = None

        self._media_player = QMediaPlayer()
        self._audio_output = QAudioOutput()
        self._media_player.setAudioOutput(self._audio_output)
        self._current_audio_widget = None
        self._media_player.positionChanged.connect(self._on_player_pos)
        self._media_player.durationChanged.connect(self._on_player_dur)
        self._media_player.playbackStateChanged.connect(self._on_player_state)

        self.setWindowTitle("FilaGlyph Agent Studio")
        self.setMinimumSize(1260, 780)

        self._image_path: str | None = None
        self._worker: AgentWorker | CoderRetryWorker | None = None
        self._role_controls: Dict[str, Dict[str, QtWidgets.QWidget]] = {}
        self._stage_cards: Dict[str, MessageCard] = {}
        self._active_stage: str | None = None
        self._stop_requested = False
        self._music_options: list[tuple[str, str]] = [("无", "")]
        self._theme_mode = "follow"
        self._page_cards: list[QtWidgets.QFrame] = []
        self._continue_card: MessageCard | None = None
        self._run_button_mode = "start"
        self._clipboard_save_worker: ClipboardImageSaveWorker | None = None
        self._active_run_dir: str = ""
        self._active_render_options: dict[str, Any] = {}

        self._load_ui_settings()

        self._build_ui()
        self._apply_elevations()
        self._load_settings_values()
        self._apply_styles()

    def _new_button(self, text: str, role: str = "neutral") -> QtWidgets.QPushButton:
        if role == "primary" and HAS_PYONEDARK and PyPushButton is not None:
            return PyPushButton(
                text=text,
                radius=10,
                color="#ffffff",
                bg_color="#2563eb",
                bg_color_hover="#1d4ed8",
                bg_color_pressed="#1e40af",
            )
        btn = QtWidgets.QPushButton(text)
        btn.setCursor(QtCore.Qt.PointingHandCursor)
        return btn

    def _new_line_edit(self, placeholder: str) -> QtWidgets.QLineEdit:
        if HAS_PYONEDARK and PyLineEdit is not None:
            if self._is_dark_mode():
                edit = PyLineEdit(
                    place_holder_text=placeholder,
                    radius=10,
                    border_size=2,
                    color="#e5e7eb",
                    selection_color="#ffffff",
                    bg_color="#111827",
                    bg_color_active="#0b1220",
                    context_color="#2563eb",
                )
            else:
                edit = PyLineEdit(
                    place_holder_text=placeholder,
                    radius=10,
                    border_size=2,
                    color="#111827",
                    selection_color="#ffffff",
                    bg_color="#ffffff",
                    bg_color_active="#f8fafc",
                    context_color="#2563eb",
                )
            return edit
        edit = QtWidgets.QLineEdit()
        edit.setPlaceholderText(placeholder)
        return edit

    def _new_combo(self) -> QtWidgets.QComboBox:
        combo = QtWidgets.QComboBox()
        combo.setObjectName("podCombo")
        combo.setCursor(QtCore.Qt.PointingHandCursor)
        combo.setMinimumHeight(36)
        combo.setStyleSheet("QComboBox { combobox-popup: 0; }")
        view = QtWidgets.QListView(combo)
        view.setObjectName("podComboView")
        view.setUniformItemSizes(True)
        view.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        view.setSpacing(4)
        combo.setView(view)
        return combo

    def _build_ui(self) -> None:
        root = QtWidgets.QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)

        root_layout = QtWidgets.QVBoxLayout(root)
        root_layout.setContentsMargins(16, 14, 16, 14)
        root_layout.setSpacing(12)

        main = QtWidgets.QHBoxLayout()
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(16)
        root_layout.addLayout(main, 1)

        sidebar = QtWidgets.QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(220)
        sb = QtWidgets.QVBoxLayout(sidebar)
        sb.setContentsMargins(14, 14, 14, 14)
        sb.setSpacing(12)

        brand = QtWidgets.QLabel("FilaGlyph")
        brand.setObjectName("brand")
        sb.addWidget(brand)

        self.workbench_btn = self._new_button("工作台")
        self.workbench_btn.setObjectName("navActive")
        self.workbench_btn.clicked.connect(lambda: self._switch_page(0))
        sb.addWidget(self.workbench_btn)

        self.audio_btn = self._new_button("音频")
        self.audio_btn.setObjectName("navButton")
        self.audio_btn.clicked.connect(lambda: self._switch_page(1))
        sb.addWidget(self.audio_btn)

        self.history_btn = self._new_button("历史记录")
        self.history_btn.setObjectName("navButton")
        self.history_btn.clicked.connect(lambda: self._switch_page(2))
        sb.addWidget(self.history_btn)

        self.settings_btn = self._new_button("设置")
        self.settings_btn.setObjectName("navButton")
        self.settings_btn.clicked.connect(lambda: self._switch_page(3))
        sb.addWidget(self.settings_btn)
        sb.addStretch(1)

        self.pages = QtWidgets.QStackedWidget()
        self.pages.addWidget(self._build_workbench_page())
        self.pages.addWidget(self._build_audio_page())
        self.pages.addWidget(self._build_history_page())
        self.pages.addWidget(self._build_settings_page())

        main.addWidget(sidebar)
        main.addWidget(self.pages, 1)

        self.status_bar = QtWidgets.QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("灏辩华")

    def _apply_elevations(self) -> None:
        for card in self._page_cards:
            effect = QtWidgets.QGraphicsDropShadowEffect(self)
            effect.setBlurRadius(24)
            effect.setOffset(0, 6)
            effect.setColor(QtGui.QColor(0, 0, 0, 60))
            card.setGraphicsEffect(effect)

    def _build_workbench_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        input_card = QtWidgets.QFrame()
        input_card.setObjectName("card")
        il = QtWidgets.QVBoxLayout(input_card)
        il.setContentsMargins(18, 18, 18, 18)
        il.setSpacing(12)

        title = QtWidgets.QLabel("输入")
        title.setObjectName("sectionTitle")
        il.addWidget(title)

        self.file_info = QtWidgets.QLabel("未选择题目图片")
        self.file_info.setObjectName("fileInfo")
        il.addWidget(self.file_info)

        self.drop_zone = DropZone()
        self.drop_zone.file_dropped.connect(self._on_file_selected)
        il.addWidget(self.drop_zone)

        self.voice_label = QtWidgets.QLabel("音色")
        self.voice_label.setObjectName("fileInfo")
        il.addWidget(self.voice_label)
        self.voice_combo = self._new_combo()
        self.voice_combo.addItem("无", userData="none")
        il.addWidget(self.voice_combo)

        self.music_label = QtWidgets.QLabel("配乐")
        self.music_label.setObjectName("fileInfo")
        il.addWidget(self.music_label)
        self.music_combo = self._new_combo()
        il.addWidget(self.music_combo)
        self._reload_music_options()

        self.run_button = self._new_button("启动", role="primary")
        self.run_button.setObjectName("runButton")
        self.run_button.setProperty("danger", False)
        self.run_button.setProperty("warning", False)
        self.run_button.clicked.connect(self._run_workflow)
        il.addWidget(self.run_button)
        il.addStretch(1)

        output_card = QtWidgets.QFrame()
        output_card.setObjectName("card")
        ol = QtWidgets.QVBoxLayout(output_card)
        ol.setContentsMargins(18, 18, 18, 18)
        ol.setSpacing(12)

        out_title = QtWidgets.QLabel("输出")
        out_title.setObjectName("sectionTitle")
        ol.addWidget(out_title)

        self.output_scroll = QtWidgets.QScrollArea()
        self.output_scroll.setObjectName("outputScroll")
        self.output_scroll.setWidgetResizable(True)
        self.output_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.output_wrap = QtWidgets.QWidget()
        self.output_wrap.setObjectName("outputWrap")
        self.output_stack = QtWidgets.QVBoxLayout(self.output_wrap)
        self.output_stack.setContentsMargins(4, 4, 4, 4)
        self.output_stack.setSpacing(12)
        self.output_stack.addStretch(1)
        self.output_scroll.setWidget(self.output_wrap)
        ol.addWidget(self.output_scroll)

        layout.addWidget(input_card, 3)
        layout.addWidget(output_card, 7)
        self._page_cards.extend([input_card, output_card])
        return page

    def _build_audio_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        voice_card = QtWidgets.QFrame()
        voice_card.setObjectName("card")
        vl = QtWidgets.QVBoxLayout(voice_card)
        vl.setContentsMargins(18, 18, 18, 18)
        vl.setSpacing(12)
        voice_title = QtWidgets.QLabel("音色素材")
        voice_title.setObjectName("sectionTitle")
        vl.addWidget(voice_title)
        self.voice_assets_list = QtWidgets.QListWidget()
        self.voice_assets_list.setObjectName("assetList")
        self.voice_assets_list.setSpacing(6)
        self.voice_assets_list.setIconSize(QtCore.QSize(18, 18))
        self.voice_assets_list.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.voice_assets_list.customContextMenuRequested.connect(
            lambda pos: self._show_asset_context_menu(pos, "voices")
        )
        self.voice_assets_list.itemClicked.connect(
            lambda item: self._on_asset_item_clicked(item, "voices")
        )
        vl.addWidget(self.voice_assets_list, 1)

        music_card = QtWidgets.QFrame()
        music_card.setObjectName("card")
        ml = QtWidgets.QVBoxLayout(music_card)
        ml.setContentsMargins(18, 18, 18, 18)
        ml.setSpacing(12)
        music_title = QtWidgets.QLabel("配乐素材")
        music_title.setObjectName("sectionTitle")
        ml.addWidget(music_title)
        self.music_assets_list = QtWidgets.QListWidget()
        self.music_assets_list.setObjectName("assetList")
        self.music_assets_list.setSpacing(6)
        self.music_assets_list.setIconSize(QtCore.QSize(18, 18))
        self.music_assets_list.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.music_assets_list.customContextMenuRequested.connect(
            lambda pos: self._show_asset_context_menu(pos, "musics")
        )
        self.music_assets_list.itemClicked.connect(
            lambda item: self._on_asset_item_clicked(item, "musics")
        )
        ml.addWidget(self.music_assets_list, 1)

        layout.addWidget(voice_card, 1)
        layout.addWidget(music_card, 1)
        self._page_cards.extend([voice_card, music_card])
        self._refresh_audio_assets_lists()
        return page

    def _build_role_row(self, role: str) -> QtWidgets.QWidget:
        row_widget = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(row_widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        model_input = self._new_line_edit(f"{role.capitalize()} model")
        model_input.setObjectName("settingsInput")
        model_input.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)

        key_input = self._new_line_edit(f"{role.capitalize()} API Key")
        key_input.setEchoMode(QtWidgets.QLineEdit.Password)
        key_input.setObjectName("settingsInput")

        row.addWidget(model_input, 5)
        row.addWidget(key_input, 5)

        self._role_controls[role] = {
            "model": model_input,
            "key": key_input,
        }
        return row_widget

    def _build_settings_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        appearance_card = QtWidgets.QFrame()
        appearance_card.setObjectName("card")
        acl = QtWidgets.QVBoxLayout(appearance_card)
        acl.setContentsMargins(18, 18, 18, 18)
        acl.setSpacing(12)
        theme_title = QtWidgets.QLabel("主题")
        theme_title.setObjectName("sectionTitle")
        acl.addWidget(theme_title)
        self.theme_toggle = ThemeToggleBar()
        self.theme_toggle.mode_changed.connect(self._on_theme_mode_changed)
        acl.addWidget(self.theme_toggle)
        self.theme_toggle.set_mode(self._theme_mode, emit=False, animate=False)

        card = QtWidgets.QFrame()
        card.setObjectName("settingsCard")
        cl = QtWidgets.QVBoxLayout(card)
        cl.setContentsMargins(18, 18, 18, 18)
        cl.setSpacing(14)

        title = QtWidgets.QLabel("API 配置")
        title.setObjectName("sectionTitle")
        cl.addWidget(title)

        form = QtWidgets.QFormLayout()
        form.setLabelAlignment(QtCore.Qt.AlignRight)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(12)

        for role in ROLE_NAMES:
            role_label = QtWidgets.QLabel(role.capitalize())
            role_label.setObjectName("settingsFormLabel")
            form.addRow(role_label, self._build_role_row(role))
        cl.addLayout(form)

        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        self.save_settings_btn = self._new_button("保存", role="primary")
        self.save_settings_btn.setObjectName("saveButton")
        self.save_settings_btn.clicked.connect(self._save_settings_values)
        row.addWidget(self.save_settings_btn)
        cl.addLayout(row)

        layout.addWidget(appearance_card)
        layout.addWidget(card)
        layout.addStretch(1)
        self._page_cards.extend([appearance_card, card])
        return page

    def _build_history_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        card = QtWidgets.QFrame()
        card.setObjectName("card")
        body = QtWidgets.QVBoxLayout(card)
        body.setContentsMargins(20, 20, 20, 20)
        body.setSpacing(14)

        title = QtWidgets.QLabel("历史记录")
        title.setObjectName("sectionTitle")
        body.addWidget(title)

        subtitle = QtWidgets.QLabel("打开过去的流程，查看结果或删除记录。")
        subtitle.setObjectName("fileInfo")
        subtitle.setWordWrap(True)
        body.addWidget(subtitle)

        self.history_scroll = QtWidgets.QScrollArea()
        self.history_scroll.setObjectName("outputScroll")
        self.history_scroll.setWidgetResizable(True)
        self.history_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)

        self.history_wrap = QtWidgets.QWidget()
        self.history_wrap.setObjectName("outputWrap")
        self.history_stack = QtWidgets.QVBoxLayout(self.history_wrap)
        self.history_stack.setContentsMargins(4, 4, 4, 4)
        self.history_stack.setSpacing(12)
        self.history_stack.addStretch(1)
        self.history_scroll.setWidget(self.history_wrap)
        body.addWidget(self.history_scroll, 1)

        layout.addWidget(card)
        self._page_cards.append(card)
        self._refresh_history_page()
        return page

    def _history_runs_root(self) -> Path:
        return ROOT / "outputs" / "agent_runs"

    def _read_run_meta(self, run_dir: Path) -> dict[str, Any]:
        meta_path = run_dir / "meta.json"
        if not meta_path.exists():
            return {}
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_run_meta(self, run_dir: Path, updates: dict[str, Any]) -> None:
        if not run_dir.exists():
            return
        meta = self._read_run_meta(run_dir)
        meta.update(updates)
        (run_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _resolve_run_video_path(self, run_dir: Path, meta: dict[str, Any] | None = None) -> Path | None:
        info = meta or self._read_run_meta(run_dir)
        video_value = str(info.get("video_path", "") or "").strip()
        if video_value:
            candidate = Path(video_value)
            if candidate.exists():
                return candidate
        for candidate in sorted(run_dir.glob("*.mp4"), key=lambda path: path.stat().st_mtime, reverse=True):
            return candidate
        return None

    def _infer_history_item(self, run_dir: Path) -> dict[str, Any]:
        meta = self._read_run_meta(run_dir)
        video_path = self._resolve_run_video_path(run_dir, meta)
        status = str(meta.get("status", "") or "").strip().lower()
        has_solver = (run_dir / "solver_answer.md").exists()
        has_architect = (run_dir / "architect_code.py").exists()
        has_director = (run_dir / "director_plan.md").exists()
        has_coder = (run_dir / "coder_output.md").exists() or (run_dir / "coder_output.py").exists()
        if not status:
            status = "success" if video_path else "failed"
        retryable = has_director and has_coder and video_path is None and status != "cancelled"
        resumable = video_path is None and (
            status == "cancelled"
            or (status == "failed" and not retryable and (has_solver or has_architect or has_director))
        )
        image_name = str(meta.get("image", "") or "").strip()
        render_options = meta.get("render_options", {})
        if not isinstance(render_options, dict):
            render_options = {}
        return {
            "run_dir": run_dir,
            "meta": meta,
            "video_path": video_path,
            "status": status,
            "retryable": retryable,
            "resumable": resumable,
            "image_name": image_name,
            "render_options": render_options,
        }

    def _clear_history_cards(self) -> None:
        while self.history_stack.count() > 1:
            item = self.history_stack.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _refresh_history_page(self) -> None:
        if not hasattr(self, "history_stack"):
            return
        self._clear_history_cards()
        runs_root = self._history_runs_root()
        runs_root.mkdir(parents=True, exist_ok=True)
        runs = sorted(
            [path for path in runs_root.iterdir() if path.is_dir()],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not runs:
            empty = QtWidgets.QFrame()
            empty.setObjectName("historyCard")
            empty_layout = QtWidgets.QVBoxLayout(empty)
            empty_layout.setContentsMargins(18, 18, 18, 18)
            empty_layout.setSpacing(6)
            title = QtWidgets.QLabel("还没有历史记录")
            title.setObjectName("historyTitle")
            desc = QtWidgets.QLabel("新的流程完成后会出现在这里。")
            desc.setObjectName("historyMeta")
            empty_layout.addWidget(title)
            empty_layout.addWidget(desc)
            self.history_stack.insertWidget(0, empty)
            return
        for run_dir in runs:
            item = self._infer_history_item(run_dir)
            self.history_stack.insertWidget(self.history_stack.count() - 1, self._build_history_card(item))

    def _build_history_card(self, item: dict[str, Any]) -> QtWidgets.QFrame:
        run_dir: Path = item["run_dir"]
        video_path: Path | None = item["video_path"]
        status = item["status"]
        retryable = bool(item["retryable"])
        resumable = bool(item["resumable"])

        card = QtWidgets.QFrame()
        card.setObjectName("historyCard")
        layout = QtWidgets.QVBoxLayout(card)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel(run_dir.name)
        title.setObjectName("historyTitle")
        header.addWidget(title)
        header.addStretch(1)

        if video_path:
            badge = QtWidgets.QLabel("成功")
            badge.setObjectName("historyBadgeSuccess")
        elif resumable:
            badge = QtWidgets.QLabel("已终止")
            badge.setObjectName("historyBadgeFailed")
        else:
            badge = QtWidgets.QLabel("失败")
            badge.setObjectName("historyBadgeFailed")
        header.addWidget(badge)
        layout.addLayout(header)

        updated_text = datetime.fromtimestamp(run_dir.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        image_name = item["image_name"] or "未记录图片"
        meta = QtWidgets.QLabel(f"图片：{image_name}    更新时间：{updated_text}")
        meta.setObjectName("historyMeta")
        meta.setWordWrap(True)
        layout.addWidget(meta)

        if video_path:
            video_info = QtWidgets.QLabel(f"视频：{video_path.name}")
        elif resumable:
            video_info = QtWidgets.QLabel("任务已终止，可继续运行。")
        elif retryable:
            video_info = QtWidgets.QLabel("Coder 失败，可重新执行。")
        else:
            video_info = QtWidgets.QLabel(f"状态：{status or 'unknown'}")
        video_info.setObjectName("historyMeta")
        video_info.setWordWrap(True)
        layout.addWidget(video_info)

        actions = QtWidgets.QHBoxLayout()
        actions.setSpacing(10)

        open_btn = QtWidgets.QPushButton("打开流程")
        open_btn.setObjectName("secondaryButton")
        open_btn.setCursor(QtCore.Qt.PointingHandCursor)
        open_btn.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_DialogOpenButton))
        open_btn.clicked.connect(lambda _=False, path=run_dir: self._open_history_run(path))
        actions.addWidget(open_btn)

        if resumable:
            continue_btn = QtWidgets.QPushButton("继续运行")
            continue_btn.setObjectName("runButton")
            continue_btn.setCursor(QtCore.Qt.PointingHandCursor)
            continue_btn.clicked.connect(lambda _=False, path=run_dir: self._continue_history_run(path))
            actions.addWidget(continue_btn)
        delete_btn = QtWidgets.QPushButton("删除记录")
        delete_btn.setObjectName("secondaryButton")
        delete_btn.setCursor(QtCore.Qt.PointingHandCursor)
        delete_btn.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_TrashIcon))
        delete_btn.clicked.connect(lambda _=False, path=run_dir: self._delete_history_run(path))
        actions.addWidget(delete_btn)
        actions.addStretch(1)
        layout.addLayout(actions)
        return card

    def _open_history_run(self, run_dir: Path) -> None:
        self._load_run_into_workbench(run_dir)
        self._switch_page(0)
        self.status_bar.showMessage(f"已打开流程：{run_dir.name}")

    def _retry_history_run(self, run_dir: Path) -> None:
        item = self._infer_history_item(run_dir)
        if not item["retryable"]:
            self._open_history_run(run_dir)
            return
        self._load_run_into_workbench(run_dir)
        self._switch_page(0)
        director_plan_path = run_dir / "director_plan.md"
        payload = {
            "director_plan": director_plan_path.read_text(encoding="utf-8"),
            "run_dir": str(run_dir),
            "render_options": item["render_options"],
        }
        self._retry_coder(payload)

    def _continue_history_run(self, run_dir: Path) -> None:
        item = self._infer_history_item(run_dir)
        self._load_run_into_workbench(run_dir)
        self._switch_page(0)
        payload = {
            "run_dir": str(run_dir),
            "render_options": item["render_options"],
        }
        self._continue_workflow(payload)

    def _delete_history_run(self, run_dir: Path) -> None:
        reply = QtWidgets.QMessageBox.question(
            self,
            "删除记录",
            f"确定删除历史记录 {run_dir.name} 吗？这不会删除已导出的视频文件。",
        )
        if reply != QtWidgets.QMessageBox.Yes:
            return
        shutil.rmtree(run_dir, ignore_errors=True)
        self._refresh_history_page()
        self.status_bar.showMessage(f"已删除历史记录：{run_dir.name}")

    def _load_run_into_workbench(self, run_dir: Path) -> None:
        self._clear_output_cards()
        self._stage_cards = {}
        self._active_stage = None
        self._set_run_button_mode("start")
        self._active_run_dir = str(run_dir)

        meta = self._read_run_meta(run_dir)
        render_options = meta.get("render_options", {}) if isinstance(meta.get("render_options", {}), dict) else {}
        self._active_render_options = dict(render_options)
        image_name = str(meta.get("image", "") or "").strip()
        image_path = run_dir / image_name if image_name else None
        if image_path and image_path.exists():
            self._on_file_selected(str(image_path))
        else:
            self._image_path = None
            self.file_info.setText("未选择题目图片")
            self.drop_zone.set_filename(None)

        coder_name = "coder_output.md"
        if (run_dir / "coder_output.py").exists() and not (run_dir / "coder_output.md").exists():
            coder_name = "coder_output.py"

        for stage, fname in (
            ("solver", "solver_answer.md"),
            ("architect", "architect_code.py"),
            ("director", "director_plan.md"),
            ("coder", coder_name),
        ):
            path = run_dir / fname
            if path.exists():
                self._on_stage_result(stage, path.read_text(encoding="utf-8"))

        video_path = self._resolve_run_video_path(run_dir, meta)
        if video_path is not None:
            card = VideoCard(str(video_path))
            self.output_stack.insertWidget(self.output_stack.count() - 1, card)
        elif self._infer_history_item(run_dir)["resumable"]:
            payload = {
                "run_dir": str(run_dir),
                "render_options": render_options,
            }
            self._show_continue_action(payload, "任务已终止，可从当前进度继续运行。")
        elif (run_dir / "director_plan.md").exists():
            payload = {
                "director_plan": (run_dir / "director_plan.md").read_text(encoding="utf-8"),
                "run_dir": str(run_dir),
                "render_options": render_options,
            }
            self._on_coder_failed(payload)
        QtCore.QTimer.singleShot(0, self._scroll_bottom)

    def _switch_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        if index == 0:
            self._reload_voice_combo()
            self.workbench_btn.setObjectName("navActive")
            self.audio_btn.setObjectName("navButton")
            self.history_btn.setObjectName("navButton")
            self.settings_btn.setObjectName("navButton")
        elif index == 1:
            self.workbench_btn.setObjectName("navButton")
            self.audio_btn.setObjectName("navActive")
            self.settings_btn.setObjectName("navButton")
            self.history_btn.setObjectName("navButton")
        elif index == 2:
            self.workbench_btn.setObjectName("navButton")
            self.audio_btn.setObjectName("navButton")
            self.history_btn.setObjectName("navActive")
            self.settings_btn.setObjectName("navButton")
            self._refresh_history_page()
        else:
            self.workbench_btn.setObjectName("navButton")
            self.audio_btn.setObjectName("navButton")
            self.history_btn.setObjectName("navButton")
            self.settings_btn.setObjectName("navActive")
        self.workbench_btn.style().unpolish(self.workbench_btn)
        self.workbench_btn.style().polish(self.workbench_btn)
        self.audio_btn.style().unpolish(self.audio_btn)
        self.audio_btn.style().polish(self.audio_btn)
        self.history_btn.style().unpolish(self.history_btn)
        self.history_btn.style().polish(self.history_btn)
        self.settings_btn.style().unpolish(self.settings_btn)
        self.settings_btn.style().polish(self.settings_btn)

    def _base_url_for_model(self, model: str) -> str:
        provider = infer_provider(model, None)
        if provider == "google":
            provider = "gemini"
        return PROVIDER_BASE_URL.get(provider, "")

    def _load_settings_values(self) -> None:
        raw = load_agent_config_json()
        roles = raw.get("roles", {})
        for role in ROLE_NAMES:
            controls = self._role_controls[role]
            model_input: QtWidgets.QLineEdit = controls["model"]  # type: ignore[assignment]
            key_input: QtWidgets.QLineEdit = controls["key"]  # type: ignore[assignment]

            role_cfg = roles.get(role, {})
            model = str(role_cfg.get("model", ""))
            model_input.setText(model)
            key_input.setText(str(role_cfg.get("api_key", "")))

    def _save_settings_values(self) -> None:
        raw = load_agent_config_json()
        roles = raw.setdefault("roles", {})

        for role in ROLE_NAMES:
            controls = self._role_controls[role]
            model_input: QtWidgets.QLineEdit = controls["model"]  # type: ignore[assignment]
            key_input: QtWidgets.QLineEdit = controls["key"]  # type: ignore[assignment]

            model = model_input.text().strip()
            if not model:
                QtWidgets.QMessageBox.warning(self, "提示", f"{role} 的模型名称不能为空。")
                return

            role_cfg = roles.setdefault(role, {})
            role_cfg["provider"] = ""
            role_cfg["model"] = model
            role_cfg["api_key"] = key_input.text().strip()
            role_cfg["base_url"] = self._base_url_for_model(model)

        save_agent_config_json(raw)
        self.status_bar.showMessage("API 配置已保存")

    @staticmethod
    def _is_supported_image(path: Path) -> bool:
        return path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

    def _reload_music_options(self) -> None:
        self._music_options = [("无", "")]
        music_dir = ROOT / "materials" / "musics"
        if music_dir.exists():
            for p in sorted(music_dir.iterdir(), key=lambda x: x.name.lower()):
                if p.is_file() and p.suffix.lower() in {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}:
                    rel = str(p.relative_to(ROOT)).replace("\\", "/")
                    self._music_options.append((p.stem, rel))

        self.music_combo.blockSignals(True)
        self.music_combo.clear()
        for label, value in self._music_options:
            self.music_combo.addItem(label, userData=value)
        self.music_combo.blockSignals(False)

    def _reload_voice_combo(self) -> None:
        """Rebuild voice combo in workbench to pick up new voice assets."""
        prev_data = self.voice_combo.currentData()
        self.voice_combo.blockSignals(True)
        self.voice_combo.clear()

        voices_dir = ROOT / "materials" / "voices"
        exts = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
        if voices_dir.exists():
            for p in sorted(voices_dir.iterdir(), key=lambda x: x.name.lower()):
                if p.is_file() and p.suffix.lower() in exts:
                    self.voice_combo.addItem(p.stem, userData=f"clone:{p.name}")

        self.voice_combo.addItem("无", userData="none")
        # Try to restore previous selection
        if prev_data:
            for i in range(self.voice_combo.count()):
                if self.voice_combo.itemData(i) == prev_data:
                    self.voice_combo.setCurrentIndex(i)
                    break
        self.voice_combo.blockSignals(False)

    def _refresh_audio_assets_lists(self) -> None:
        voices_dir = ROOT / "materials" / "voices"
        musics_dir = ROOT / "materials" / "musics"
        voices_dir.mkdir(parents=True, exist_ok=True)
        musics_dir.mkdir(parents=True, exist_ok=True)
        exts = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}

        if hasattr(self, "voice_assets_list"):
            self.voice_assets_list.clear()
            for p in sorted(voices_dir.iterdir(), key=lambda x: x.name.lower()):
                if p.is_file() and p.suffix.lower() in exts:
                    item = QtWidgets.QListWidgetItem()
                    item.setData(QtCore.Qt.UserRole, str(p))
                    widget = AudioItemWidget(p, show_prompt=True)
                    widget.play_requested.connect(self._handle_audio_play)
                    widget.rename_requested.connect(self._handle_inline_rename)
                    item.setSizeHint(widget.sizeHint())
                    item.setFlags(item.flags() & ~QtCore.Qt.ItemIsSelectable)
                    self.voice_assets_list.addItem(item)
                    self.voice_assets_list.setItemWidget(item, widget)
            self._add_plus_item(self.voice_assets_list)
        if hasattr(self, "music_assets_list"):
            self.music_assets_list.clear()
            for p in sorted(musics_dir.iterdir(), key=lambda x: x.name.lower()):
                if p.is_file() and p.suffix.lower() in exts:
                    item = QtWidgets.QListWidgetItem()
                    item.setData(QtCore.Qt.UserRole, str(p))
                    widget = AudioItemWidget(p)
                    widget.play_requested.connect(self._handle_audio_play)
                    widget.rename_requested.connect(self._handle_inline_rename)
                    item.setSizeHint(widget.sizeHint())
                    item.setFlags(item.flags() & ~QtCore.Qt.ItemIsSelectable)
                    self.music_assets_list.addItem(item)
                    self.music_assets_list.setItemWidget(item, widget)
            self._add_plus_item(self.music_assets_list)
        self._reload_music_options()
        self._reload_voice_combo()

    @staticmethod
    def _add_plus_item(list_widget: QtWidgets.QListWidget) -> None:
        """Append a dimmed '+' item as the last entry for adding new assets."""
        item = QtWidgets.QListWidgetItem("+")
        item.setData(QtCore.Qt.UserRole, "__add__")
        item.setTextAlignment(QtCore.Qt.AlignCenter)
        item.setForeground(QtGui.QColor(160, 160, 160))
        font = item.font()
        font.setPointSize(16)
        item.setFont(font)
        item.setFlags(item.flags() & ~QtCore.Qt.ItemIsSelectable)
        list_widget.addItem(item)

    def _on_asset_item_clicked(self, item: QtWidgets.QListWidgetItem, kind: str) -> None:
        """Handle click on the '+' item to add new assets."""
        if item.data(QtCore.Qt.UserRole) == "__add__":
            if kind == "voices":
                list_widget = self.voice_assets_list
                menu = QtWidgets.QMenu(self)
                import_action = menu.addAction("导入本地音频")
                record_action = menu.addAction("直接录音")
                # Show menu below the + item
                rect = list_widget.visualItemRect(item)
                action = menu.exec(list_widget.viewport().mapToGlobal(rect.bottomLeft()))
                if action == import_action:
                    self._add_audio_asset(kind)
                elif action == record_action:
                    self._record_audio_asset(kind)
            else:
                self._add_audio_asset(kind)

    def _record_audio_asset(self, kind: str) -> None:
        """Open a dialog to record voice and save it as an asset."""
        dialog = VoiceRecordDialog(self)
        if dialog.exec() == QtWidgets.QDialog.Accepted:
            result = dialog.get_result()
            if result:
                name, temp_file = result
                target_dir = ROOT / "materials" / ("voices" if kind == "voices" else "musics")
                target_dir.mkdir(parents=True, exist_ok=True)
                new_file = target_dir / f"{name}.wav"
                if new_file.exists():
                    QtWidgets.QMessageBox.warning(self, "提示", "同名文件已存在，录音未保存。")
                    return
                import shutil
                shutil.copy2(temp_file, new_file)
                self._refresh_audio_assets_lists()
                self.status_bar.showMessage(f"音色素材已保存：{name}")

    def _show_asset_context_menu(self, pos, kind: str) -> None:
        """Show a right-click context menu for rename / delete."""
        list_widget = self.voice_assets_list if kind == "voices" else self.music_assets_list
        item = list_widget.itemAt(pos)
        if item is None or item.data(QtCore.Qt.UserRole) == "__add__":
            return

        menu = QtWidgets.QMenu(self)
        rename_action = menu.addAction("重命名")
        delete_action = menu.addAction("删除")

        action = menu.exec(list_widget.viewport().mapToGlobal(pos))
        if action == rename_action:
            widget = list_widget.itemWidget(item)
            if isinstance(widget, AudioItemWidget):
                widget.start_rename()
        elif action == delete_action:
            self._delete_audio_asset_by_item(kind, item)

    def _handle_inline_rename(self, old_file_path_str: str, new_name: str):
        """Rename an audio asset file through inline edit."""
        old_file = Path(old_file_path_str)
        target_dir = old_file.parent
        new_file = target_dir / f"{new_name}{old_file.suffix}"

        if new_file.exists():
            QtWidgets.QMessageBox.warning(self, "提示", "同名文件已存在。")
            self._refresh_audio_assets_lists()
            return

        if self._current_audio_widget and self._current_audio_widget.file_path == str(old_file):
            self._media_player.stop()
            self._media_player.setSource(QtCore.QUrl())
            self._current_audio_widget = None

        try:
            old_file.rename(new_file)
            self._refresh_audio_assets_lists()
            self.status_bar.showMessage(f"已重命名：{old_file.stem} → {new_name}")
        except PermissionError:
            QtWidgets.QMessageBox.warning(self, "错误", "无法重命名：文件正在被占用，例如正在播放。")
            self._refresh_audio_assets_lists()
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "错误", f"重命名失败：{e}")
            self._refresh_audio_assets_lists()

    def _delete_audio_asset_by_item(self, kind: str, item: QtWidgets.QListWidgetItem) -> None:
        """Delete an audio asset by its list item."""
        file_path_str = item.data(QtCore.Qt.UserRole)
        if not file_path_str or file_path_str == "__add__":
            return
        target = Path(file_path_str)

        if self._current_audio_widget and self._current_audio_widget.file_path == str(target):
            self._media_player.stop()
            self._media_player.setSource(QtCore.QUrl())
            self._current_audio_widget = None

        if target.exists():
            try:
                target.unlink()
                self._refresh_audio_assets_lists()
                self.status_bar.showMessage("音频素材已删除")
            except PermissionError:
                QtWidgets.QMessageBox.warning(self, "错误", "无法删除：文件正在被占用，例如正在试听。请稍后再试。")
            except Exception as e:
                QtWidgets.QMessageBox.warning(self, "错误", f"删除失败：{e}")

    def _handle_audio_play(self, file_path_str: str):
        sender_widget = self.sender()
        if self._current_audio_widget == sender_widget:
            if self._media_player.playbackState() == QMediaPlayer.PlayingState:
                self._media_player.pause()
            else:
                self._media_player.play()
        else:
            if self._current_audio_widget:
                self._current_audio_widget.set_playing_state(False)
            self._current_audio_widget = sender_widget
            self._media_player.setSource(QtCore.QUrl.fromLocalFile(file_path_str))
            self._media_player.play()

    def _on_player_pos(self, pos: int):
        if self._current_audio_widget:
            self._current_audio_widget.set_progress(pos, self._media_player.duration())

    def _on_player_dur(self, dur: int):
        if self._current_audio_widget:
            self._current_audio_widget.set_progress(self._media_player.position(), dur)

    def _on_player_state(self, state):
        if self._current_audio_widget:
            self._current_audio_widget.set_playing_state(state == QMediaPlayer.PlayingState)

    @staticmethod
    def _find_asset_file(directory: Path, stem: str) -> Path | None:
        """Find an audio file by its stem name (without extension)."""
        exts = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
        for p in directory.iterdir():
            if p.is_file() and p.suffix.lower() in exts and p.stem == stem:
                return p
        return None

    def _add_audio_asset(self, kind: str) -> None:
        target_dir = ROOT / "materials" / ("voices" if kind == "voices" else "musics")
        target_dir.mkdir(parents=True, exist_ok=True)
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self,
            "选择音频文件",
            str(ROOT),
            "Audio (*.mp3 *.wav *.m4a *.aac *.flac *.ogg *.opus)",
        )
        if not paths:
            return
        for src in paths:
            src_path = Path(src)
            if src_path.exists():
                shutil.copy2(src_path, target_dir / src_path.name)
        self._refresh_audio_assets_lists()
        self.status_bar.showMessage("音频素材已添加")

    def _allocate_clipboard_image_path(self) -> Path:
        runs_root = self._history_runs_root()
        run_stamp = QtCore.QDateTime.currentDateTime().toString("yyyyMMdd_HHmmss")
        run_dir = runs_root / f"{run_stamp}_{uuid.uuid4().hex[:6]}"
        stamp = QtCore.QDateTime.currentDateTime().toString("yyyyMMdd_HHmmss_zzz")
        return run_dir / f"pasted_{stamp}.png"

    @staticmethod
    def _save_clipboard_image(image: QtGui.QImage, out_path: Path) -> bool:
        if image.isNull():
            return False
        out_path.parent.mkdir(parents=True, exist_ok=True)
        return image.save(str(out_path), "PNG")

    def _paste_image_from_clipboard(self) -> bool:
        clipboard = QtWidgets.QApplication.clipboard()
        mime = clipboard.mimeData()
        if not mime:
            return False

        if mime.hasImage():
            if self._clipboard_save_worker is not None and self._clipboard_save_worker.isRunning():
                self.status_bar.showMessage("正在处理上一张粘贴图片，请稍候...")
                return True

            image = clipboard.image()
            if image.isNull():
                return False

            out_path = self._allocate_clipboard_image_path()
            self._on_file_selected(str(out_path))
            self.status_bar.showMessage("正在处理粘贴图片...")

            worker = ClipboardImageSaveWorker(image, out_path, self)
            worker.finished_save.connect(self._on_clipboard_image_saved)
            worker.finished.connect(worker.deleteLater)
            self._clipboard_save_worker = worker
            worker.start()
            return True

        if mime.hasUrls():
            for url in mime.urls():
                local = url.toLocalFile()
                if not local:
                    continue
                candidate = Path(local)
                if candidate.exists() and self._is_supported_image(candidate):
                    self._on_file_selected(str(candidate))
                    self.status_bar.showMessage("已从剪贴板粘贴图片路径")
                    return True
        return False

    @QtCore.Slot(str, bool, str)
    def _on_clipboard_image_saved(self, out_path: str, ok: bool, error: str) -> None:
        self._clipboard_save_worker = None
        if ok:
            self.status_bar.showMessage("已从剪贴板粘贴图片")
            return

        # Only clear selection if this failed file is still the active one.
        if self._image_path == out_path:
            self._image_path = None
            self.file_info.setText("未选择题目图片")
            self.drop_zone.set_filename(None)
        self.status_bar.showMessage(f"粘贴图片失败: {error or '未知错误'}")

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        if self.pages.currentIndex() == 0 and event.matches(QtGui.QKeySequence.Paste):
            if self._paste_image_from_clipboard():
                event.accept()
                return
        super().keyPressEvent(event)

    def _on_file_selected(self, path: str) -> None:
        self._image_path = path
        self.file_info.setText(f"当前题目图片: {Path(path).name}")
        self.drop_zone.set_filename(path)
        self.status_bar.showMessage(f"已选择图片: {Path(path).name}")

    def _push_message(self, title: str, content: str, status: str = "done") -> None:
        if not content or not content.strip():
            return
        card = MessageCard("system", title)
        card.set_content(content)
        card.set_status(status)
        self.output_stack.insertWidget(self.output_stack.count() - 1, card)
        QtCore.QTimer.singleShot(0, self._scroll_bottom)

    def _ensure_stage_card(self, stage: str, title: str) -> MessageCard:
        card = self._stage_cards.get(stage)
        if card is not None:
            return card
        card = MessageCard(stage, title)
        card.set_status("running")
        self._stage_cards[stage] = card
        self.output_stack.insertWidget(self.output_stack.count() - 1, card)
        QtCore.QTimer.singleShot(0, self._scroll_bottom)
        return card

    def _clear_output_cards(self) -> None:
        """Remove all message/video cards from the output stack before a new run."""
        while self.output_stack.count() > 1:
            item = self.output_stack.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _scroll_bottom(self) -> None:
        bar = self.output_scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _run_workflow(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._request_stop()
            return
        if self._run_button_mode == "new_project":
            self._start_new_project()
            return
        if not self._image_path:
            QtWidgets.QMessageBox.warning(self, "提示", "请先上传题目图片。")
            return
        self._set_busy(True)
        self._stop_requested = False
        self._clear_output_cards()
        self._stage_cards = {}
        self._active_stage = None
        self.status_bar.showMessage("正在运行 Agent 工作流...")
        render_options = self._collect_render_options_from_ui()
        self._active_render_options = dict(render_options)
        self._active_run_dir = ""
        self._worker = AgentWorker(self._image_path, render_options=render_options)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.cancelled.connect(self._on_cancelled)
        self._worker.coder_failed.connect(self._on_coder_failed)
        self._worker.progress.connect(self._on_progress)
        self._worker.stage_result.connect(self._on_stage_result)
        self._worker.start()

    def _request_stop(self) -> None:
        if self._worker is None or not self._worker.isRunning():
            return
        if not self._stop_requested:
            self._stop_requested = True
            self._worker.request_cancel()
            self.status_bar.showMessage("已请求停止，正在等待当前步骤结束...（再次点击可强制终止）")
            self.run_button.setText("强制终止")
            self.run_button.setProperty("warning", False)
            self.run_button.setProperty("danger", True)
            self.run_button.style().unpolish(self.run_button)
            self.run_button.style().polish(self.run_button)
            return

        continue_payload = self._worker.continue_payload() if hasattr(self._worker, "continue_payload") else {}
        self._worker.force_terminate()
        if self._active_stage and self._active_stage in self._stage_cards:
            self._stage_cards[self._active_stage].set_status("error")
        self._show_continue_action(continue_payload, "已强制终止当前任务。")
        self.status_bar.showMessage("已强制终止")
        self._worker = None
        self._stop_requested = False
        self._set_busy(False)

    def _on_finished(self, payload: dict) -> None:
        run_dir_str = payload.get("run_dir", "")
        self._active_run_dir = str(run_dir_str or "")
        render_opts = payload.get("render_options", {})
        if isinstance(render_opts, dict):
            self._active_render_options = dict(render_opts)
        self._enable_coder_rerun(payload)
        self._push_message("运行目录", run_dir_str)

        video_paths: list[Path] = []
        outputs_dir = ROOT / "outputs"
        if outputs_dir.exists():
            for p in sorted(outputs_dir.glob("*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True):
                if (datetime.now().timestamp() - p.stat().st_mtime) < 300:
                    video_paths.append(p)

        if not video_paths and run_dir_str:
            run_dir = Path(run_dir_str)
            if run_dir.exists():
                for p in sorted(run_dir.glob("*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True):
                    video_paths.append(p)

        if video_paths:
            resolved_video = video_paths[0]
            v_card = VideoCard(str(resolved_video))
            self.output_stack.insertWidget(self.output_stack.count() - 1, v_card)
            QtCore.QTimer.singleShot(0, self._scroll_bottom)
            if run_dir_str:
                self._write_run_meta(
                    Path(run_dir_str),
                    {
                        "status": "success",
                        "video_path": str(resolved_video.resolve()),
                        "updated_at": datetime.now().isoformat(timespec="seconds"),
                    },
                )
        elif run_dir_str:
            self._write_run_meta(
                Path(run_dir_str),
                {
                    "status": "finished_without_video",
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                },
            )

        self.status_bar.showMessage("完成")
        self._worker = None
        self._stop_requested = False
        self._set_busy(False, next_mode="new_project")
        self._refresh_history_page()

    def _on_failed(self, message: str) -> None:
        if self._active_stage and self._active_stage in self._stage_cards:
            failed_card = self._stage_cards[self._active_stage]
            failed_card.set_status("error")
            self._attach_stage_retry_icon(self._active_stage, failed_card)
            if message:
                failed_card.set_content(message)
        self._push_message("运行失败", message, status="error")
        self.status_bar.showMessage(message if message else "运行失败")
        self._worker = None
        self._stop_requested = False
        self._set_busy(False, next_mode="new_project")
        if message != "运行已取消":
            QtWidgets.QMessageBox.critical(self, "运行失败", message)

    def _on_cancelled(self, payload: dict) -> None:
        if self._active_stage and self._active_stage in self._stage_cards:
            self._stage_cards[self._active_stage].set_status("error")
        self._show_continue_action(payload, "任务已终止，可从当前进度继续运行。")
        run_dir_str = str(payload.get("run_dir", "") or "").strip()
        if run_dir_str:
            self._write_run_meta(
                Path(run_dir_str),
                {
                    "status": "cancelled",
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                },
            )
        self.status_bar.showMessage("任务已终止，可继续运行")
        self._worker = None
        self._stop_requested = False
        self._set_busy(False, next_mode="new_project")
        self._refresh_history_page()

    def _show_continue_action(self, payload: dict, message: str) -> None:
        run_dir_str = str(payload.get("run_dir", "") or "").strip()
        if run_dir_str:
            self._write_run_meta(
                Path(run_dir_str),
                {
                    "status": "cancelled",
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                },
            )
        card = self._stage_cards.get(self._active_stage or "")
        if card is None:
            card = MessageCard("system", "运行终止")
            card.set_content(message)
            card.set_status("error")
            self.output_stack.insertWidget(self.output_stack.count() - 1, card)
        else:
            card.set_content(message)
            card.set_status("error")
        card.add_action_button("continue_run", "继续运行", lambda: self._continue_workflow(payload))
        self._continue_card = card
        QtCore.QTimer.singleShot(0, self._scroll_bottom)

    def _continue_workflow(self, payload: dict) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        run_dir_str = str(payload.get("run_dir", "") or "").strip()
        if not run_dir_str:
            QtWidgets.QMessageBox.warning(self, "提示", "找不到可继续的任务目录。")
            return
        if self._continue_card is not None:
            self._continue_card.remove_action_button("continue_run")
            self._continue_card = None
        self._set_busy(True)
        self._stop_requested = False
        self._active_stage = None
        self._active_run_dir = run_dir_str
        self._active_render_options = self._merge_render_options(payload)
        resume_stage = str(payload.get("resume_from_stage", "") or "").strip().lower()
        stop_stage = str(payload.get("stop_after_stage", "") or "").strip().lower()
        if resume_stage:
            self.status_bar.showMessage(f"正在继续运行（从 {resume_stage} 开始）...")
        else:
            self.status_bar.showMessage("正在继续运行...")
        self._worker = AgentWorker(
            image_path=None,
            render_options=self._active_render_options,
            resume_run_dir=run_dir_str,
            resume_from_stage=resume_stage or None,
            stop_after_stage=stop_stage or None,
        )
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.cancelled.connect(self._on_cancelled)
        self._worker.coder_failed.connect(self._on_coder_failed)
        self._worker.progress.connect(self._on_progress)
        self._worker.stage_result.connect(self._on_stage_result)
        self._worker.start()

    def _on_coder_failed(self, payload: dict) -> None:
        """Handle coder failure without treating it as a full workflow crash."""
        card = self._stage_cards.get("coder") or self._ensure_stage_card("coder", "Coding Agent 代码")
        card.set_status("error")
        self._enable_coder_rerun(payload)
        self._attach_stage_retry_icon("coder", card)

        run_dir_str = str(payload.get("run_dir", "") or "").strip()
        if run_dir_str:
            self._active_run_dir = run_dir_str
        render_opts = payload.get("render_options", {})
        if isinstance(render_opts, dict):
            self._active_render_options = dict(render_opts)
        if run_dir_str:
            self._write_run_meta(
                Path(run_dir_str),
                {
                    "status": "coder_failed",
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                },
            )

        self.status_bar.showMessage("Coder 执行失败，可点击重试")
        self._worker = None
        self._stop_requested = False
        self._set_busy(False, next_mode="new_project")
        self._refresh_history_page()

    def _retry_coder(self, payload: dict) -> None:
        """Re-run only the coder step using preserved earlier outputs."""
        if self._worker is not None and self._worker.isRunning():
            return

        # Remove the old coder card so a fresh one is created
        old_card = self._stage_cards.pop("coder", None)
        if old_card is not None:
            self.output_stack.removeWidget(old_card)
            old_card.setParent(None)
            old_card.deleteLater()

        self._set_busy(True)
        self._stop_requested = False
        self._active_stage = None
        self._active_run_dir = str(payload["run_dir"])
        self._active_render_options = self._merge_render_options(payload)
        self.status_bar.showMessage("Coder 正在重新执行...")

        self._worker = CoderRetryWorker(
            director_plan=payload["director_plan"],
            render_options=self._active_render_options,
            run_dir=payload["run_dir"],
        )
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.cancelled.connect(self._on_cancelled)
        self._worker.coder_failed.connect(self._on_coder_failed)
        self._worker.progress.connect(self._on_progress)
        self._worker.stage_result.connect(self._on_stage_result)
        self._worker.start()

    def _collect_render_options_from_ui(self) -> dict:
        voice_data = str(self.voice_combo.currentData() or "none")
        prompt_text = ""
        if voice_data.startswith("clone:"):
            voice_filename = voice_data[len("clone:"):]
            resolved = ROOT / "materials" / "voices" / voice_filename
            if resolved.exists():
                voice_data = str(resolved)
                txt_path = resolved.with_suffix(".txt")
                if txt_path.exists():
                    prompt_text = txt_path.read_text(encoding="utf-8").strip()
        return {
            "voice": voice_data,
            "prompt_text": prompt_text,
            "bgm_path": str(self.music_combo.currentData() or ""),
            "tts_backend": os.environ.get("FILAGLYPH_TTS_BACKEND", "local").strip() or "local",
            "tts_api_base_url": os.environ.get("COSYVOICE_MODELSCOPE_API_URL", "").strip() or os.environ.get("COSYVOICE_API_URL", "").strip(),
            "tts_api_key": os.environ.get("COSYVOICE_API_KEY", "").strip(),
            "tts_api_timeout": float(os.environ.get("COSYVOICE_API_TIMEOUT_S", "180")),
        }

    def _merge_render_options(self, payload: dict) -> dict:
        base = payload.get("render_options", {})
        merged = dict(base) if isinstance(base, dict) else {}
        ui_options = self._collect_render_options_from_ui()
        merged.update(ui_options)
        # Keep prior successful choices when UI still sits at default "none"/empty.
        if merged.get("voice") in ("none", "") and str(base.get("voice", "")).strip() not in ("", "none"):
            merged["voice"] = base.get("voice")
        if not str(merged.get("prompt_text", "")).strip() and str(base.get("prompt_text", "")).strip():
            merged["prompt_text"] = base.get("prompt_text")
        if not str(merged.get("bgm_path", "")).strip() and str(base.get("bgm_path", "")).strip():
            merged["bgm_path"] = base.get("bgm_path")
        return merged

    def _enable_coder_rerun(self, payload: dict) -> None:
        card = self._stage_cards.get("coder")
        if card is None:
            return

        run_dir = str(payload.get("run_dir", "") or "").strip()
        director_plan = str(payload.get("director", "") or payload.get("director_plan", "") or "").strip()
        if not director_plan:
            director_card = self._stage_cards.get("director")
            director_plan = str(getattr(director_card, "_raw_markdown", "") or "").strip()
        if not run_dir or not director_plan:
            return

        retry_payload = {
            "run_dir": run_dir,
            "director_plan": director_plan,
            "render_options": payload.get("render_options", {}),
        }
        card._retry_payload = retry_payload  # type: ignore[attr-defined]

    def _resolve_run_context(self) -> tuple[str, dict[str, Any]]:
        run_dir = self._active_run_dir
        render_options: dict[str, Any] = dict(self._active_render_options)

        worker = self._worker
        if worker is not None and hasattr(worker, "current_run_dir"):
            try:
                candidate = worker.current_run_dir()
                if candidate is not None:
                    run_dir = str(candidate)
            except Exception:
                pass
        if worker is not None and hasattr(worker, "continue_payload"):
            try:
                payload = worker.continue_payload()
                opts = payload.get("render_options", {}) if isinstance(payload, dict) else {}
                if isinstance(opts, dict) and opts:
                    render_options = dict(opts)
            except Exception:
                pass
        return run_dir, render_options

    def _attach_stage_retry_icon(self, stage: str, card: MessageCard) -> None:
        if stage not in {"solver", "architect", "director", "coder"}:
            return
        tooltip_map = {
            "solver": "重新运行 Solver",
            "architect": "重新运行 Architect",
            "director": "重新运行 Director",
            "coder": "重新运行 Coder",
        }
        card.add_header_icon_button(
            key=f"rerun_{stage}",
            tooltip=tooltip_map[stage],
            callback=lambda _checked=False, s=stage: self._retry_stage(s),
            icon_text="⟳",
        )

    def _retry_stage(self, stage: str) -> None:
        if self._worker is not None and self._worker.isRunning():
            return

        run_dir_str, render_options = self._resolve_run_context()
        if not run_dir_str:
            QtWidgets.QMessageBox.warning(self, "提示", "找不到可重试的任务目录。")
            return
        run_dir = Path(run_dir_str)
        if not run_dir.exists():
            QtWidgets.QMessageBox.warning(self, "提示", "任务目录不存在，无法重试。")
            return

        if stage == "coder":
            director_plan = str(getattr(self._stage_cards.get("director"), "_raw_markdown", "") or "").strip()
            if not director_plan:
                director_path = run_dir / "director_plan.md"
                if director_path.exists():
                    director_plan = director_path.read_text(encoding="utf-8")
            if not director_plan:
                QtWidgets.QMessageBox.warning(self, "提示", "缺少 Director 规划，无法重试 Coder。")
                return
            self._retry_coder(
                {
                    "run_dir": str(run_dir),
                    "director_plan": director_plan,
                    "render_options": render_options,
                }
            )
            return

        self._write_run_meta(
            run_dir,
            {
                "status": f"retrying_{stage}",
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            },
        )
        self._continue_workflow(
            {
                "run_dir": str(run_dir),
                "render_options": render_options,
                "resume_from_stage": stage,
                "stop_after_stage": stage,
            }
        )

    def _on_progress(self, message: str) -> None:
        stage_map = {
            "solver": "解题结果",
            "architect": "Manim Architect 代码",
            "director": "动画规划书",
            "coder": "Coding Agent 代码",
        }
        m = message.lower()
        for stage, title in stage_map.items():
            if stage in m:
                self._active_stage = stage
                self._ensure_stage_card(stage, title).set_status("running")
                break
        self.status_bar.showMessage(message)

    def _on_stage_result(self, stage: str, content: str) -> None:
        title_map = {
            "solver": "解题结果",
            "architect": "Manim Architect 代码",
            "director": "动画规划书",
            "coder": "Coding Agent 代码",
        }
        card = self._ensure_stage_card(stage, title_map.get(stage, stage))
        card.set_content(content)
        card.set_status("done")
        self._attach_stage_retry_icon(stage, card)
        if stage == "coder":
            run_dir = ""
            render_options = {}
            if self._worker is not None and hasattr(self._worker, "continue_payload"):
                try:
                    payload = self._worker.continue_payload()
                    run_dir = str(payload.get("run_dir", "") or "").strip()
                    render_options = payload.get("render_options", {}) if isinstance(payload, dict) else {}
                except Exception:
                    pass
            if run_dir:
                self._active_run_dir = run_dir
            if isinstance(render_options, dict) and render_options:
                self._active_render_options = dict(render_options)
            self._enable_coder_rerun(
                {
                    "run_dir": run_dir,
                    "director_plan": str(getattr(self._stage_cards.get("director"), "_raw_markdown", "") or "").strip(),
                    "render_options": render_options,
                }
            )
        if self._active_stage == stage:
            self._active_stage = None

    def _set_run_button_mode(self, mode: str) -> None:
        self._run_button_mode = mode
        self.run_button.setEnabled(True)
        if mode == "running":
            self.run_button.setText("终止运行")
            self.run_button.setProperty("warning", True)
            self.run_button.setProperty("danger", False)
        elif mode == "new_project":
            self.run_button.setText("新建项目")
            self.run_button.setProperty("warning", False)
            self.run_button.setProperty("danger", False)
        else:
            self.run_button.setText("启动")
            self.run_button.setProperty("warning", False)
            self.run_button.setProperty("danger", False)
        self.run_button.style().unpolish(self.run_button)
        self.run_button.style().polish(self.run_button)

    def _set_busy(self, busy: bool, *, next_mode: str = "start") -> None:
        if busy:
            self._set_run_button_mode("running")
        else:
            self._set_run_button_mode(next_mode)

    def _start_new_project(self) -> None:
        self._worker = None
        self._stop_requested = False
        self._active_stage = None
        self._stage_cards = {}
        if self._continue_card is not None:
            self._continue_card.remove_action_button("continue_run")
            self._continue_card = None
        self._image_path = None
        self.file_info.setText("未选择题目图片")
        self.drop_zone.set_filename(None)
        self._clear_output_cards()
        self.pages.setCurrentIndex(0)
        self._set_run_button_mode("start")
        self.status_bar.showMessage("已新建项目，可开始新任务")

    def _system_is_dark_mode(self) -> bool:
        window_color = self.palette().color(QtGui.QPalette.Window)
        return window_color.lightness() < 128

    def _is_dark_mode(self) -> bool:
        if self._theme_mode == "dark":
            return True
        if self._theme_mode == "light":
            return False
        return self._system_is_dark_mode()

    def _on_theme_mode_changed(self, mode: str) -> None:
        self._theme_mode = mode
        self._save_ui_settings()
        self._apply_styles()

    def _load_ui_settings(self) -> None:
        self._theme_mode = "follow"
        try:
            if UI_SETTINGS_PATH.exists():
                data = json.loads(UI_SETTINGS_PATH.read_text(encoding="utf-8"))
                mode = str(data.get("theme_mode", "follow")).strip().lower()
                if mode in {"light", "dark", "follow"}:
                    self._theme_mode = mode
        except Exception:
            self._theme_mode = "follow"

    def _save_ui_settings(self) -> None:
        UI_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {"theme_mode": self._theme_mode}
        UI_SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def changeEvent(self, event: QtCore.QEvent) -> None:
        if event.type() in (QtCore.QEvent.PaletteChange, QtCore.QEvent.ApplicationPaletteChange):
            self._apply_styles()
        super().changeEvent(event)

    def _apply_styles(self) -> None:
        is_dark = self._is_dark_mode()
        self._sync_pyonedark_theme(is_dark=is_dark)
        colors = self._current_pod_colors(is_dark=is_dark)

        self.setStyleSheet(
            f"""
            #root {{ background: {colors['dark_one']}; }}
            #sidebar {{ background: {colors['dark_two']}; border: 1px solid {colors['dark_three']}; border-radius: 12px; }}
            #brand {{ color: {colors['text_title']}; font-size: 24px; font-weight: 700; }}
            #navButton, #navActive {{ text-align: left; border-radius: 10px; padding: 9px 12px; font-size: 13px; }}
            #navButton {{ color: {colors['text_foreground']}; background: {colors['bg_one']}; border: 1px solid {colors['bg_two']}; }}
            #navButton:hover {{ background: {colors['bg_two']}; }}
            #navActive {{ color: {colors['icon_active']}; background: {colors['context_color']}; border: 1px solid {colors['context_hover']}; }}

            #card {{ background: {colors['bg_one']}; border: 1px solid {colors['bg_two']}; border-radius: 12px; }}
            #settingsCard {{ background: {colors['bg_one']}; border: 1px solid {colors['bg_two']}; border-radius: 12px; }}
            #settingsFormLabel {{ color: {colors['text_title']}; font-size: 13px; font-weight: 600; min-width: 92px; }}
            #outputScroll {{ background: transparent; border: none; }}
            #outputWrap {{ background: transparent; }}
            #sectionTitle {{ color: {colors['text_title']}; font-size: 14px; font-weight: 650; }}
            #fileInfo {{ color: {colors['text_description']}; font-size: 12px; }}

            #settingsInput, #podCombo {{
                color: {colors['text_foreground']};
                background: {colors['dark_three']};
                border: 1px solid {colors['bg_three']};
                border-radius: 10px;
                padding: 6px 8px;
            }}
            #podCombo::down-arrow {{ image: none; width: 0px; height: 0px; }}
            #podCombo::drop-down {{ border: none; width: 0px; }}
            #podCombo QAbstractItemView, #podComboView {{
                color: {colors['text_foreground']};
                background: {colors['bg_one']};
                border: 1px solid {colors['bg_two']};
                border-radius: 10px;
                selection-background-color: {colors['context_color']};
                selection-color: {colors['icon_active']};
                padding: 4px;
                outline: 0;
            }}
            #podCombo QAbstractItemView::item, #podComboView::item {{
                min-height: 34px;
                padding: 8px 10px;
                margin: 2px 4px;
                border-radius: 8px;
                color: {colors['text_foreground']};
            }}
            #podCombo QAbstractItemView::item:hover, #podComboView::item:hover {{
                background: {colors['bg_two']};
                color: {colors['text_title']};
            }}
            #podCombo QAbstractItemView::item:selected, #podComboView::item:selected {{
                background: {colors['context_color']};
                color: #ffffff;
            }}
            #assetList QLabel, #assetList QLineEdit {{
                color: {colors['text_foreground']};
            }}
            #assetList {{
                color: {colors['text_foreground']};
                background: {colors['dark_three']};
                border: 1px solid {colors['bg_two']};
                border-radius: 12px;
                padding: 4px;
                font-size: 13px;
            }}
            #assetList::item {{
                min-height: 48px;
                border-radius: 9px;
                padding: 0px 4px;
                margin: 2px 2px;
            }}
            #assetList::item:hover {{ background: {colors['bg_two']}; }}

            #themeToggle {{
                background: {colors['dark_three']};
                border: 1px solid {colors['bg_two']};
                border-radius: 20px;
            }}
            #themePill {{
                background: {colors['context_color']};
                border-radius: 16px;
            }}
            #themeBtn {{
                border: none;
                background: transparent;
                color: {colors['text_description']};
                font-size: 18px;
                font-weight: 700;
                padding: 0px;
            }}
            #themeBtn[active=\"true\"] {{ color: #ffffff; }}

            #dropZone {{ background: {colors['dark_three']}; border: 2px dashed {colors['context_color']}; border-radius: 12px; min-height: 180px; }}
            #dropZone QLabel {{ color: {colors['text_foreground']}; font-size: 13px; }}

            #runButton, #saveButton {{
                color: #ffffff;
                background: {colors['context_color']};
                border: 1px solid {colors['context_color']};
                border-radius: 10px;
                padding: 10px 14px;
                font-weight: 600;
            }}
            #runButton:hover, #saveButton:hover {{ background: {colors['context_hover']}; }}
            #runButton[warning="true"] {{
                color: #111827;
                background: #fbbc04;
                border: 1px solid #f59e0b;
            }}
            #runButton[warning="true"]:hover {{ background: #f59e0b; }}
            #runButton[danger="true"] {{ background: {colors['red']}; border: 1px solid {colors['red']}; color: #ffffff; }}
            #secondaryButton {{
                color: {colors['text_title']};
                background: {colors['bg_one']};
                border: 1px solid {colors['bg_three']};
                border-radius: 10px;
                padding: 10px 14px;
                font-weight: 600;
            }}
            #secondaryButton:hover {{ background: {colors['bg_two']}; }}

            QMenu {{
                background: {colors['bg_one']};
                border: 1px solid {colors['bg_two']};
                border-radius: 8px;
                padding: 4px;
                color: {colors['text_foreground']};
                font-size: 13px;
            }}
            QMenu::item {{ padding: 6px 24px; border-radius: 6px; }}
            QMenu::item:selected {{ background: {colors['context_color']}; color: #ffffff; }}

            #messageCard {{ background: {colors['dark_three']}; border: 1px solid {colors['bg_two']}; border-radius: 12px; }}
            #agentIcon {{ background: {colors['bg_three']}; color: {colors['icon_active']}; border-radius: 12px; font-size: 11px; font-weight: 700; }}
            #messageTitle {{ color: {colors['text_title']}; font-weight: 650; font-size: 13px; }}
            #messageStatusRunning {{ color: {colors['context_hover']}; font-size: 12px; font-weight: 600; }}
            #messageStatusDone {{ color: {colors['green']}; font-size: 12px; font-weight: 600; }}
            #messageStatusError {{ color: {colors['red']}; font-size: 12px; font-weight: 600; }}
            #messageSpinner {{ border: 1px solid {colors['bg_two']}; border-radius: 4px; background: {colors['dark_four']}; }}
            #messageSpinner::chunk {{ background: {colors['context_color']}; }}
            #headerIconButton {{
                color: {colors['icon_color']};
                background: {colors['dark_four']};
                border: 1px solid {colors['bg_two']};
                border-radius: 12px;
                font-size: 13px;
                font-weight: 700;
            }}
            #headerIconButton:hover {{ background: {colors['bg_two']}; color: {colors['text_title']}; }}
            #messagePreview {{ color: {colors['text_foreground']}; font-size: 12px; }}
            #fadeMask {{ border: none; background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 rgba(0,0,0,0), stop:1 {colors['dark_three']}); }}
            #dividerLine {{ color: {colors['bg_two']}; }}
            #expandButton {{ color: {colors['icon_color']}; background: {colors['dark_four']}; border: 1px solid {colors['bg_two']}; border-radius: 9px; padding: 3px 10px; font-size: 11px; font-weight: 600; }}
            #historyCard {{
                background: {colors['bg_one']};
                border: 1px solid {colors['bg_two']};
                border-radius: 16px;
            }}
            #historyTitle {{ color: {colors['text_title']}; font-size: 14px; font-weight: 650; }}
            #historyMeta {{ color: {colors['text_description']}; font-size: 12px; }}
            #historyBadgeSuccess, #historyBadgeFailed {{
                border-radius: 10px;
                padding: 4px 10px;
                font-size: 11px;
                font-weight: 700;
            }}
            #historyBadgeSuccess {{
                color: #ffffff;
                background: {colors['green']};
            }}
            #historyBadgeFailed {{
                color: #ffffff;
                background: {colors['red']};
            }}

            QTextBrowser#messageFull, QWebEngineView#messageWeb {{
                color: {colors['text_foreground']};
                background: {colors['dark_four']};
                border: 1px solid {colors['bg_two']};
                border-radius: 10px;
                font-size: 12px;
            }}
            QStatusBar {{ color: {colors['text_description']}; background: transparent; }}
            """
        )
        if hasattr(self, "output_scroll"):
            vp = self.output_scroll.viewport()
            vp.setAutoFillBackground(True)
            pal = vp.palette()
            pal.setColor(QtGui.QPalette.Window, QtGui.QColor(colors["bg_one"]))
            vp.setPalette(pal)
        self._refresh_line_edit_theme(colors)

    def _sync_pyonedark_theme(self, *, is_dark: bool) -> None:
        if not HAS_PYONEDARK or PODSettings is None:
            return
        try:
            settings = PODSettings()
            settings.deserialize()
            settings.items["theme_name"] = "dracula" if is_dark else "bright_theme"
            settings.serialize()
            if PODThemes is not None:
                _ = PODThemes()
        except Exception:
            pass

    def _current_pod_colors(self, *, is_dark: bool) -> dict[str, str]:
        return get_theme_palette(is_dark)

    def _refresh_line_edit_theme(self, colors: dict[str, str]) -> None:
        if not HAS_PYONEDARK or PyLineEdit is None:
            return
        edits: list[QtWidgets.QLineEdit] = []
        for controls in self._role_controls.values():
            model_input = controls.get("model")
            key_input = controls.get("key")
            if isinstance(model_input, QtWidgets.QLineEdit):
                edits.append(model_input)
            if isinstance(key_input, QtWidgets.QLineEdit):
                edits.append(key_input)
        for edit in edits:
            if not hasattr(edit, "set_stylesheet"):
                continue
            edit.set_stylesheet(
                radius=10,
                border_size=2,
                color=colors['text_foreground'],
                selection_color=colors['icon_active'],
                bg_color=colors['dark_three'],
                bg_color_active=colors['dark_four'],
                context_color=colors['context_color'],
            )
            edit.setStyleSheet(
                edit.styleSheet()
                + f"""
QLineEdit {{
    border: 1px solid {colors['bg_three']};
}}
QLineEdit:focus {{
    border: 2px solid {colors['context_color']};
}}
"""
            )


def main() -> None:
    QtWidgets.QApplication.setHighDpiScaleFactorRoundingPolicy(
        QtCore.Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle(QtWidgets.QStyleFactory.create("Fusion"))

    bundled_families: list[str] = []
    for font_path in BUNDLED_FONT_PATHS:
        if not font_path.exists():
            continue
        font_id = QtGui.QFontDatabase.addApplicationFont(str(font_path))
        if font_id >= 0:
            bundled_families.extend(QtGui.QFontDatabase.applicationFontFamilies(font_id))

    font = QtGui.QFont()
    prefer_families = []
    if bundled_families:
        prefer_families.extend(bundled_families)
    prefer_families.extend(
        [
            "Source Han Sans SC",
            "Noto Sans CJK SC",
            "Microsoft YaHei UI",
            "PingFang SC",
            "Segoe UI",
        ]
    )
    font.setFamilies(prefer_families)
    font.setPointSize(10)
    font.setHintingPreference(QtGui.QFont.HintingPreference.PreferNoHinting)
    font.setStyleStrategy(QtGui.QFont.StyleStrategy.PreferAntialias)
    app.setFont(font)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
