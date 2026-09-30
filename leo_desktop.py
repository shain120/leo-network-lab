"""Native desktop shell for drafting and confirming a LEO network lab."""
from __future__ import annotations

import argparse
import html
import json
import os
import uuid
import sys
import threading
import functools
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from enum import Enum, auto
from typing import Any, Optional

from PySide6.QtCore import (
    QEasingCurve, QEvent, QObject, QPropertyAnimation, Qt, QTimer, QPointF,
    QUrl, Signal, QRectF, QStringListModel,
)
from PySide6.QtGui import (
    QBrush, QCloseEvent, QColor, QFont, QMouseEvent, QPainter, QPainterPath,
    QPen, QPolygonF, QDesktopServices,
)
from PySide6.QtWidgets import (
    QApplication, QDockWidget, QGraphicsScene, QGraphicsView, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QMainWindow, QMessageBox, QPushButton,
    QStackedWidget, QTabWidget, QTextEdit, QToolBar, QVBoxLayout, QWidget,
    QDialog, QComboBox, QFormLayout, QGraphicsEllipseItem, QGraphicsTextItem,
    QGraphicsLineItem,
    QInputDialog, QSlider,
    QGraphicsOpacityEffect, QGraphicsSceneContextMenuEvent, QMenu, QTextBrowser,
    QDoubleSpinBox, QSpinBox, QToolButton, QFrame, QSplitter, QSizePolicy,
    QScrollArea, QCompleter, QStatusBar, QProgressBar,
)
try:
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
except ImportError:  # Keep the desktop shell usable when only the optional view package is missing.
    QWebEngineView = None  # type: ignore[assignment,misc]
    QWebEnginePage = None  # type: ignore[assignment,misc]
    QWebEngineSettings = None  # type: ignore[assignment,misc]

from leo_lab import Lab, ROLES
from leo_agent import LeoToolCallAgent, LeoToolRegistry
from leo_llm import LLMConfigError, ProviderSettings, UnifiedLLMClient
from leo_network_lab.config import ROLE_IMAGE_MAP, ROLE_VISUAL_MAP


class WorkerSignals(QObject):
    completed = Signal(dict)

COLORS = {
    'background': '#10171e', 'surface': '#19232d', 'raised': '#243340',
    'border': '#526778', 'text': '#e7eef3', 'muted': '#a7b7c5',
    'accent': '#7fd9c2', 'warning': '#efc57d', 'success': '#8bc9a3',
    'error': '#db8e92', 'idle': '#8c9aa6',
}

class ConfigState(Enum):
    IDLE = auto()
    PROMPTING_NODES = auto()
    PROMPTING_LINKS = auto()
    SUMMARY = auto()
    CONFIRMED = auto()


class RuntimeStatusCapsule(QToolButton):
    """Compact, text-backed status trigger; never communicates by colour alone."""
    def __init__(self, system: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.system = system
        self.setObjectName('runtime-capsule')
        self.setProperty('runtimeState', 'ready')
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.setMinimumHeight(32)
        self.setAccessibleName(f'{system} runtime status')
        self.set_status('READY', 'Ready')

    def set_status(self, status: str, stage: str | None = None) -> None:
        normalized = str(status or 'READY').upper()
        running = normalized in {'CONFIGURING', 'QUEUED', 'RUNNING', 'GENERATING', 'ANALYZING', 'PARSING',
                                 'PLAYING', 'WAITING', 'WAITING_FOR_CONFIRMATION', 'PREPARING'}
        state = ('failed' if normalized in {'FAILED', 'ERROR'} else 'warning' if normalized == 'WARNING'
                 else 'complete' if normalized == 'COMPLETED' else 'cancelled' if normalized == 'CANCELLED'
                 else 'running' if running else 'idle' if normalized in {'IDLE', 'PENDING', 'NOT_READY'} else 'ready')
        glyph = {'failed': '●', 'warning': '●', 'complete': '●', 'cancelled': '●',
                 'running': '◉', 'ready': '●', 'idle': '●'}[state]
        display = (stage or normalized).replace('_', ' ').title()
        if display in {'Dataset Ready', 'Completed'}: display = 'Complete'
        self.setText(f'{glyph} {self.system} · {display}')
        self.setProperty('runtimeState', state)
        self.style().unpolish(self); self.style().polish(self)
        self.setToolTip(f'Open {self.system} runtime details: {display}')


class HypatiaRuntimeStore:
    """Authoritative, derived runtime state for every Hypatia UI surface.

    Dataset discovery, a background job, and endpoint membership are distinct
    facts.  This store is the only place that turns those facts into the
    effective status consumed by the panel, footer capsule, and action gate.
    """
    _ACTIVE = {'QUEUED', 'RUNNING'}

    def __init__(self) -> None:
        self.state: dict[str, Any] = {
            'selected_dataset_id': None, 'selected_dataset_status': 'NOT_READY',
            'active_generation_job_id': None, 'active_generation_status': None,
            'active_generation_stage': None, 'target_dataset_id': None,
            'target_dataset': None, 'source_status': 'UNKNOWN',
            'destination_status': 'UNKNOWN', 'analysis_status': None,
            'effective_status': 'NOT_READY', 'stage': 'Dataset not ready',
            'reason': 'Dataset not ready. Generate or select a READY dataset before path analysis.',
            'can_analyze': False, 'job': None, 'dataset': None,
        }

    @staticmethod
    def _is_generation(job: dict[str, Any]) -> bool:
        stage = str(job.get('stage') or '').upper()
        return bool(job.get('dataset_generation')) or stage in {
            'DATASET_GENERATING', 'CUSTOM_DATASET_GENERATING', 'DATASET_READY',
            'DATASET_FAILED', 'GENERATING_GROUND_STATIONS', 'GENERATING_TLES',
            'GENERATING_ISLS', 'GENERATING_GSL_INTERFACES', 'GENERATING_FORWARDING_STATE',
        } or stage.startswith('GENERATING_')

    def recompute(self, *, selected_dataset: dict[str, Any] | None,
                  active_job: dict[str, Any] | None,
                  source_found: bool, destination_found: bool,
                  source_name: str = '', destination_name: str = '') -> dict[str, Any]:
        selected_status = str((selected_dataset or {}).get('status') or 'NOT_READY').upper()
        source_status = 'FOUND' if source_found else 'NOT_FOUND'
        destination_status = 'FOUND' if destination_found else 'NOT_FOUND'
        job = dict(active_job or {})
        job_status = str(job.get('status') or '').upper()
        is_generation = bool(job) and self._is_generation(job)
        stage_id = str(job.get('stage') or '').upper()
        target = None
        if is_generation:
            target = {
                'dataset_id': job.get('dataset_id'), 'network': (job.get('request') or {}).get('network'),
                'duration_s': job.get('duration_s') or (job.get('request') or {}).get('duration_sec'),
                'time_step_ms': job.get('time_step_ms') or (job.get('request') or {}).get('step_ms'),
                'isl_mode': (job.get('request') or {}).get('isl_mode'),
            }

        effective, stage, reason = 'NOT_READY', 'Dataset not ready', 'Dataset not ready. Generate or select a READY dataset before path analysis.'
        if job_status == 'FAILED':
            effective, stage = 'FAILED', 'Failed'
            reason = str(job.get('error') or 'Hypatia dataset generation failed.')
        elif job_status == 'CANCELLED':
            effective, stage = 'NOT_READY', 'Dataset generation cancelled'
            reason = 'Dataset generation was cancelled. Generate a complete dataset before path analysis.'
        elif job_status == 'WARNING':
            effective, stage = 'WARNING', 'No recent progress'
            reason = str(job.get('warning') or job.get('message') or 'Hypatia needs attention before path analysis.')
        elif job_status in self._ACTIVE:
            if is_generation:
                effective = 'GENERATING'
                stage = stage_id or 'DATASET_GENERATING'
                reason = 'Waiting for the new Hypatia dataset to finish generation.'
            else:
                effective = 'ANALYZING'
                stage = stage_id or 'PATH_ANALYSIS'
                reason = 'Hypatia path analysis is running.'
        elif selected_status != 'READY':
            pass
        elif not source_found:
            effective, stage = 'NOT_READY', 'Source ground station not found'
            reason = f'Source ground station not found: {source_name or "enter a source"}.'
        elif not destination_found:
            effective, stage = 'NOT_READY', 'Destination ground station not found'
            reason = f'Destination ground station not found: {destination_name or "enter a destination"}.'
        else:
            effective, stage = 'READY_FOR_PATH_ANALYSIS', 'READY_FOR_PATH_ANALYSIS'
            reason = 'Dataset Ready. Source and destination ground stations are available.'

        self.state = {
            **self.state,
            'selected_dataset_id': (selected_dataset or {}).get('dataset_id'),
            'selected_dataset_status': selected_status,
            'active_generation_job_id': job.get('job_id') if is_generation and job_status in self._ACTIVE else None,
            'active_generation_status': job_status if is_generation else None,
            'active_generation_stage': stage_id if is_generation else None,
            'target_dataset_id': (target or {}).get('dataset_id'), 'target_dataset': target,
            'source_status': source_status, 'destination_status': destination_status,
            'analysis_status': job_status if job and not is_generation else None,
            'effective_status': effective, 'status': effective, 'stage': stage,
            'reason': reason,
            'can_analyze': effective == 'READY_FOR_PATH_ANALYSIS' and source_status == 'FOUND' and destination_status == 'FOUND',
            'job': job or None, 'dataset': selected_dataset,
            'source_found': source_found, 'destination_found': destination_found,
        }
        return dict(self.state)


class RuntimeFooterStatusBar(QStatusBar):
    """Persistent footer runtime row; transient notices never replace it."""
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._content = QWidget(self)
        self._layout = QHBoxLayout(self._content)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)
        self._message = QLabel('')
        self._message.setProperty('tone', 'muted')
        self._message.setAccessibleName('Application footer message')
        self.addPermanentWidget(self._content, 1)

    def set_runtime_widgets(self, widgets: list[QWidget]) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget(): item.widget().setParent(None)
        for widget in widgets:
            self._layout.addWidget(widget)
        self._layout.addWidget(self._message, 1)

    def showMessage(self, message: str, timeout: int = 0) -> None:  # noqa: N802 - Qt API name
        # Keep the footer's runtime controls visible. Qt's default transient
        # message area hides ordinary status widgets, which is not acceptable
        # for a persistent readiness indicator.
        self._message.setText(message)

    def clearMessage(self) -> None:  # noqa: N802 - Qt API name
        self._message.clear()


class RuntimeStatusDialog(QDialog):
    def __init__(self, parent: 'LabWindow', system: str, job: dict[str, Any] | None) -> None:
        super().__init__(parent)
        self.system, self.job = system, job or {}
        self.setWindowTitle(f'{system} Runtime')
        self.setModal(False)
        self.setMinimumWidth(420)
        self.setAccessibleName(f'{system} runtime details')
        root = QVBoxLayout(self); root.setContentsMargins(18, 18, 18, 18); root.setSpacing(12)
        title = label(f'{system} Runtime', heading=True); root.addWidget(title)
        if not job:
            root.addWidget(label('No background activity has been recorded in this session.', muted=True))
        else:
            status = str(job.get('status', 'READY')).replace('_', ' ').title()
            stage = str(job.get('stage', 'Ready')).replace('_', ' ').title()
            elapsed = float(job.get('elapsed_sec') or 0)
            rows = [
                ('Status', status), ('Job', str(job.get('type') or 'Activity')),
                ('Stage', stage), ('Elapsed', f'{int(elapsed // 60):02d}:{int(elapsed % 60):02d}'),
                ('PID', str(job.get('pid') or 'Not available')),
            ]
            if job.get('cpu_count'):
                rows.append(('Threads', f"{job.get('resolved_threads')} resolved from {job.get('requested_threads')} · {job.get('cpu_count')} CPU cores"))
            if job.get('duration_s') is not None:
                rows.append(('Duration', f"{job['duration_s']} s"))
            if job.get('time_step_ms') is not None:
                rows.append(('Time Step', f"{job['time_step_ms']} ms"))
            if job.get('cpu_percent') is not None:
                rows.append(('CPU', f"{float(job['cpu_percent']):.1f}%"))
            if job.get('memory_percent') is not None:
                rows.append(('Memory', f"{float(job['memory_percent']):.2f}%"))
            if job.get('exit_code') is not None:
                rows.append(('Exit Code', str(job['exit_code'])))
            if job.get('dataset_id'): rows.append(('Dataset', str(job['dataset_id'])))
            if job.get('analysis_id'): rows.append(('Analysis', str(job['analysis_id'])))
            form = QFormLayout()
            for key, value in rows:
                value_label = QLabel(value); value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                value_label.setWordWrap(True); form.addRow(key, value_label)
            root.addLayout(form)
            generated, total = job.get('generated_states'), job.get('total_states')
            if isinstance(generated, int) and isinstance(total, int) and total > 0:
                root.addWidget(label('Forwarding State Progress', heading=True))
                progress = QProgressBar(); progress.setRange(0, total); progress.setValue(min(total, generated))
                progress.setTextVisible(True); progress.setFormat(f'{generated} / {total} states · %p%')
                progress.setAccessibleName('Generated forwarding-state progress')
                root.addWidget(progress)
                age = job.get('last_progress_age_sec')
                if isinstance(age, (int, float)):
                    root.addWidget(label(f'Last Progress: {int(age)} s ago', muted=True))
            stages = job.get('progress_stages') or []
            if stages:
                root.addWidget(label('Progress', heading=True))
                current = str(job.get('stage', '')).upper().replace(' ', '_')
                current_index = next((index for index, item in enumerate(stages)
                                      if str(item.get('id')) in current), -1)
                for index, item in enumerate(stages):
                    glyph = '✓' if current_index >= 0 and index < current_index else ('◉' if index == current_index else '○')
                    root.addWidget(QLabel(f"{glyph}  {item.get('label', item.get('id'))}"))
            message = job.get('error') or job.get('warning') or job.get('message')
            if message:
                message_label = QLabel(str(message)); message_label.setWordWrap(True)
                message_label.setProperty('tone', 'muted'); root.addWidget(message_label)
        buttons = QHBoxLayout()
        if self.job.get('log_path'):
            open_log = QPushButton('View Log')
            open_log.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.job['log_path']))))
            buttons.addWidget(open_log)
        if system == 'Hypatia' and (self.job.get('status') in {'QUEUED', 'RUNNING', 'GENERATING', 'ANALYZING'}
                                    and self.job.get('job_id')):
            cancel = QPushButton('Cancel')
            cancel.clicked.connect(self._cancel_hypatia); buttons.addWidget(cancel)
        buttons.addStretch(); close = QPushButton('Close'); close.clicked.connect(self.accept); buttons.addWidget(close)
        root.addLayout(buttons)

    def _cancel_hypatia(self) -> None:
        result = self.parent().lab.call('cancel_hypatia_job', {'job_id': self.job['job_id']})
        if result.get('status') == 'ok': self.accept()
        else: QMessageBox.warning(self, 'Cancel failed', result.get('error', 'Unable to cancel job.'))

class ConfigManager:
    """Handles the stepped configuration of lab drafts."""
    def __init__(self, lab: Lab, window: 'LabWindow') -> None:
        self.lab = lab
        self.window = window
        self.state = ConfigState.IDLE
        self.queue: list[tuple[str, str, str]] = []  # (category, item_id, field_name)
        self.current_idx = 0

    def start_config(self, full: bool = True) -> None:
        self.queue = []
        for d_id in self.lab.drafts:
            fields = ['ip'] if not full else ['ip', 'name', 'role', 'image']
            for field in fields:
                self.queue.append(('Node', d_id, field))
        for l_id in self.lab.link_drafts:
            for field in ['source', 'target']:
                self.queue.append(('Link', l_id, field))
        self.current_idx = 0
        if full:
            self.window.append_ai_text(
                '[AI] 已根據目前拓樸產生預設值；現在逐項確認完整節點與鏈路設定。'
            )
        else:
            self.window.append_ai_text('[AI] 已根據目前拓樸產生預設值；現在從每個節點 IP 開始確認。')
        self.state = ConfigState.PROMPTING_NODES if self.queue else ConfigState.SUMMARY
        self.window.update_config_ui()
        self.prompt_current()

    def prompt_current(self) -> None:
        if self.current_idx >= len(self.queue):
            self.state = ConfigState.SUMMARY
            self.window.update_config_ui()
            self.window.show_summary()
            return
        cat, item_id, field = self.queue[self.current_idx]
        self.state = ConfigState.PROMPTING_NODES if cat == 'Node' else ConfigState.PROMPTING_LINKS
        self.window.update_config_ui()
        val = ''
        if cat == 'Node':
            val = self.lab.drafts[item_id].get(field, '')
        else:
            val = self.lab.link_drafts[item_id].get(field, '')
        field_labels = {
            'name': '名稱', 'role': '角色', 'ip': '服務 IP',
            'image': '容器映像', 'source': '來源', 'target': '目標',
        }
        if cat == 'Node':
            subject = f"節點 {self.lab.drafts[item_id]['name']}"
        else:
            link = self.lab.link_drafts[item_id]
            subject = f"鏈路 {link['source']} ↔ {link['target']}"
        prompt = (
            f'[{self.current_idx + 1}/{len(self.queue)}] '
            f'{subject} · {field_labels[field]}\n'
            f'AI 預設值：{val}\n'
            '按 Enter 接受，或直接輸入新值：'
        )
        self.window.append_ai_text(prompt)

    def handle_input(self, text: str) -> bool:
        if self.state == ConfigState.IDLE or self.state == ConfigState.CONFIRMED:
            return False
        if self.state == ConfigState.SUMMARY:
            if text.lower() in {'start', 'start-full'}:
                self.start_config(full=text.lower() == 'start-full')
                return True
            if text.lower() == 'yes':
                self.state = ConfigState.CONFIRMED
                self.window.update_config_ui()
                self.window.append_ai_text('\n[System] 配置已確認。準備部署... (Ticket 04)')
                return True
            else:
                self.window.append_ai_text(f'請輸入 "yes" 以確認執行，或 "start-full" 重新配置。')
                return True
        if self.state in (ConfigState.PROMPTING_NODES, ConfigState.PROMPTING_LINKS):
            cat, item_id, field = self.queue[self.current_idx]
            if text.strip():
                if cat == 'Node':
                    candidate = dict(self.lab.drafts[item_id])
                    candidate[field] = text.strip()
                    result = self.lab.call('update_draft', {
                        'draft_id': item_id, **candidate,
                    })
                    if result['status'] != 'draft':
                        self.window.append_ai_text(
                            f"✖ {result.get('error', '參數無效')}；請重新輸入。")
                        self.prompt_current()
                        return True
                    self.window.sync_canvas_from_workspace()
                else:
                    candidate = dict(self.lab.link_drafts[item_id])
                    candidate[field] = text.strip()
                    names = ({item['name'] for item in self.lab.drafts.values()} |
                             set(self.lab.nodes))
                    if (candidate['source'] == candidate['target'] or
                            candidate['source'] not in names or
                            candidate['target'] not in names):
                        self.window.append_ai_text('✖ 鏈路端點無效；請重新輸入。')
                        self.prompt_current()
                        return True
                    self.lab.link_drafts[item_id] = candidate
            self.current_idx += 1
            self.prompt_current()
            return True
        return False

class NodeItem(QGraphicsEllipseItem):
    """Draggable node on the topology canvas."""
    def __init__(self, node_id: str, name: str, role: str) -> None:
        super().__init__(-20, -20, 40, 40)
        self.node_id = node_id
        self.name = name
        self.role = role
        self.status = 'Draft'
        self.visual_role = ROLE_VISUAL_MAP.get(role, role)
        self.engineering_symbol = self.visual_role
        self.setFlags(
            QGraphicsEllipseItem.GraphicsItemFlag.ItemIsMovable |
            QGraphicsEllipseItem.GraphicsItemFlag.ItemIsSelectable |
            QGraphicsEllipseItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setPen(QPen(QColor(COLORS['border']), 2))
        self.set_role_visual(role)
        self.text_item = QGraphicsTextItem(self._format_text(), self)
        self.text_item.setDefaultTextColor(QColor(COLORS['text']))
        self._position_label()
        self.links: list[LinkItem] = []

    def _format_text(self) -> str:
        return f"{self.name}\n({self.status})"

    def set_status(self, status: str) -> None:
        self.status = status
        self.text_item.setPlainText(self._format_text())
        self._position_label()

    def _position_label(self) -> None:
        text_rect = self.text_item.boundingRect()
        self.text_item.setPos(-text_rect.width() / 2, self.rect().bottom() + 5)

    def set_role_visual(self, role: str) -> None:
        self.role = role
        self.visual_role = ROLE_VISUAL_MAP.get(role, role)
        self.engineering_symbol = self.visual_role
        size = 64
        self.setRect(-size / 2, -size / 2, size, size)
        self.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        self.update()
        if hasattr(self, 'text_item'):
            self._position_label()

    def paint(self, painter: QPainter, option: Any, widget: Optional[QWidget] = None) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        status_color = {
            'Running': QColor('#65d98b'),
            'Error': QColor('#f07178'),
        }.get(self.status, QColor(COLORS['border']))
        outline = QPen(status_color, 2)
        if self.status == 'Draft':
            outline.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(outline)
        painter.setBrush(QBrush(QColor(COLORS['raised'])))
        painter.drawRoundedRect(self.rect(), 8, 8)

        painter.setPen(QPen(QColor(COLORS['text']), 2,
                            Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                            Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(QBrush(QColor(COLORS['accent'])))
        if self.visual_role == 'satellite':
            self._paint_satellite(painter)
        elif self.visual_role == 'ue':
            self._paint_ue(painter)
        elif self.visual_role == 'gateway':
            self._paint_gateway(painter)
        else:
            self._paint_server(painter)

        if self.isSelected():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(COLORS['accent']), 2))
            painter.drawRoundedRect(self.rect().adjusted(-3, -3, 3, 3), 10, 10)
        painter.restore()

    @staticmethod
    def _paint_satellite(painter: QPainter) -> None:
        painter.drawRect(-9, -8, 18, 16)
        painter.setBrush(QBrush(QColor('#4d7ea8')))
        painter.drawRect(-25, -9, 12, 18)
        painter.drawRect(13, -9, 12, 18)
        painter.drawLine(-13, 0, -9, 0)
        painter.drawLine(9, 0, 13, 0)
        painter.drawLine(-25, -3, -13, -3)
        painter.drawLine(-25, 3, -13, 3)
        painter.drawLine(13, -3, 25, -3)
        painter.drawLine(13, 3, 25, 3)
        painter.drawLine(0, -8, 0, -14)
        painter.drawEllipse(QPointF(0, -16), 2, 2)

    @staticmethod
    def _paint_ue(painter: QPainter) -> None:
        painter.drawRoundedRect(-11, -18, 22, 36, 4, 4)
        painter.setBrush(QBrush(QColor('#4d7ea8')))
        painter.drawRect(-7, -12, 14, 21)
        painter.setBrush(QBrush(QColor(COLORS['text'])))
        painter.drawEllipse(QPointF(0, 13), 1.5, 1.5)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawArc(9, -14, 18, 18, 120 * 16, 100 * 16)
        painter.drawArc(12, -19, 25, 25, 120 * 16, 100 * 16)

    @staticmethod
    def _paint_gateway(painter: QPainter) -> None:
        painter.drawRoundedRect(-19, -16, 38, 32, 4, 4)
        painter.setBrush(QBrush(QColor('#d79a55')))
        painter.drawRect(-12, -9, 24, 5)
        painter.drawRect(-12, 1, 24, 5)
        painter.setBrush(QBrush(QColor(COLORS['text'])))
        painter.drawEllipse(QPointF(8, 10), 2, 2)
        painter.drawLine(-25, 0, -19, 0)
        painter.drawLine(19, 0, 25, 0)
        painter.drawLine(-25, 0, -21, -4)
        painter.drawLine(-25, 0, -21, 4)
        painter.drawLine(25, 0, 21, -4)
        painter.drawLine(25, 0, 21, 4)

    @staticmethod
    def _paint_server(painter: QPainter) -> None:
        painter.drawRoundedRect(-17, -20, 34, 40, 4, 4)
        painter.setBrush(QBrush(QColor('#4d7ea8')))
        for y in (-13, -2, 9):
            painter.drawRoundedRect(-11, y, 22, 7, 2, 2)
        painter.setBrush(QBrush(QColor(COLORS['text'])))
        for y in (-10, 1, 12):
            painter.drawEllipse(QPointF(7, y), 1.5, 1.5)

    def itemChange(self, change: QGraphicsEllipseItem.GraphicsItemChange, value: Any) -> Any:
        if change == QGraphicsEllipseItem.GraphicsItemChange.ItemPositionHasChanged:
            for link in self.links:
                link.update_position()
        return super().itemChange(change, value)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        super().mouseDoubleClickEvent(event)
        if self.status == 'Running':
            window = self.scene().views()[0].window()
            if hasattr(window, 'open_node_terminal'):
                window.open_node_terminal(self.name)

    def contextMenuEvent(self, event: QGraphicsSceneContextMenuEvent) -> None:
        # Right-click to open parameter dialog for draft nodes only
        if self.status == 'Draft':
            menu = QMenu()
            edit_action = menu.addAction("編輯參數")
            action = menu.exec_(event.screenPos())
            if action == edit_action:
                window = self.scene().views()[0].window()
                if isinstance(window, LabWindow):
                    window.edit_node_parameters(self)

class LinkItem(QGraphicsLineItem):
    """Dynamic line connecting two NodeItems."""
    def __init__(self, source: NodeItem, target: NodeItem) -> None:
        super().__init__()
        self.source = source
        self.target = target
        self.setPen(QPen(QColor(COLORS['accent']), 2))
        self.setZValue(-1)
        source.links.append(self)
        target.links.append(self)
        self.update_position()

    def set_active(self, active: bool) -> None:
        color = '#00ff00' if active else COLORS['accent']  # Green if active
        self.setPen(QPen(QColor(color), 2))

    def update_position(self) -> None:
        self.setLine(
            self.source.scenePos().x(), self.source.scenePos().y(),
            self.target.scenePos().x(), self.target.scenePos().y()
        )

class NodeDialog(QDialog):
    """Dialog to collect or edit node details."""
    def __init__(self, parent: QWidget, roles: set[str], initial_data: Optional[dict[str, str]] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle('節點參數')
        self.setFixedWidth(300)
        layout = QFormLayout(self)
        self.name_edit = QLineEdit()
        self.role_combo = QComboBox()
        self.role_combo.addItems(sorted(list(roles)))
        self.ip_edit = QLineEdit()
        self.image_edit = QLineEdit()
        layout.addRow('名稱:', self.name_edit)
        layout.addRow('角色:', self.role_combo)
        layout.addRow('IP 位址:', self.ip_edit)
        layout.addRow('映像檔:', self.image_edit)
        if initial_data:
            self.name_edit.setText(initial_data.get('name', ''))
            self.role_combo.setCurrentText(initial_data.get('role', ''))
            self.ip_edit.setText(initial_data.get('ip', ''))
            self.image_edit.setText(initial_data.get('image', ''))
        btns = QHBoxLayout()
        self.ok_btn = QPushButton('確定')
        self.ok_btn.clicked.connect(self.accept)
        self.cancel_btn = QPushButton('取消')
        self.cancel_btn.clicked.connect(self.reject)
        btns.addWidget(self.ok_btn)
        btns.addWidget(self.cancel_btn)
        layout.addRow(btns)

    def get_data(self) -> dict[str, str]:
        return {
            'name': self.name_edit.text(),
            'role': self.role_combo.currentText(),
            'ip': self.ip_edit.text(),
            'image': self.image_edit.text(),
        }


class FlowDialog(QDialog):
    """Native form for a confirmation-gated session flow proposal."""
    def __init__(self, parent: QWidget, node_names: list[str],
                 selected_names: list[str]) -> None:
        super().__init__(parent)
        self.setWindowTitle('建立實驗流量')
        self.setAccessibleName('建立實驗流量對話框')
        layout = QFormLayout(self)
        self.source = QComboBox()
        self.destination = QComboBox()
        self.source.addItems(node_names)
        self.destination.addItems(node_names)
        if selected_names:
            self.source.setCurrentText(selected_names[0])
        if len(selected_names) > 1:
            self.destination.setCurrentText(selected_names[1])
        self.protocol = QComboBox()
        self.protocol.addItems(['udp', 'tcp'])
        self.rate = QDoubleSpinBox()
        self.rate.setRange(0.1, 100000)
        self.rate.setValue(10.0)
        self.rate.setSuffix(' Mbps')
        self.duration = QSpinBox()
        self.duration.setRange(1, 3600)
        self.duration.setValue(30)
        self.duration.setSuffix(' s')
        layout.addRow('來源', self.source)
        layout.addRow('目標', self.destination)
        layout.addRow('協定', self.protocol)
        layout.addRow('固定速率', self.rate)
        layout.addRow('持續時間', self.duration)
        buttons = QHBoxLayout()
        cancel = QPushButton('取消')
        cancel.clicked.connect(self.reject)
        submit = QPushButton('建立提案')
        submit.clicked.connect(self.accept)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(submit)
        layout.addRow(buttons)

    def get_data(self) -> dict[str, Any]:
        return {'source': self.source.currentText(), 'destination': self.destination.currentText(),
                'protocol': self.protocol.currentText(), 'rate_mbps': self.rate.value(),
                'duration_seconds': self.duration.value()}


class Sns3ExperimentDialog(QDialog):
    """Configuration form backed only by installed, official SNS-3 examples."""
    DEFAULTS = {
        'fixed_rate_cbr': {'packetSize': '512', 'interval': '1s', 'duration': '10', 'scenario': 'simple'},
        'random_access': {'utsPerBeam': '1', 'endUsersPerUt': '1'},
        'acm_training': {'utsPerBeam': '1', 'simDurationInSeconds': '10'},
    }
    LABELS = {
        'packetSize': 'Packet size (bytes)', 'interval': 'CBR interval (ns-3 Time)',
        'duration': 'Simulation time (seconds)', 'scenario': 'Official scenario preset',
        'utsPerBeam': 'UTs per active beam', 'endUsersPerUt': 'End users per UT',
        'simDurationInSeconds': 'Simulation time (seconds)',
    }

    def __init__(self, parent: QWidget, experiments: dict[str, Any], capability: dict[str, Any]) -> None:
        super().__init__(parent)
        self.setWindowTitle('SNS-3 Satellite Experiment')
        self.setMinimumWidth(480)
        self._experiments = experiments
        self._fields: dict[str, QLineEdit] = {}
        layout = QVBoxLayout(self)
        status = ('✓ ' if capability.get('available') else '✖ ') + (
            f"SNS-3 {capability.get('SNS3_VERSION', '')}" if capability.get('available')
            else 'SNS-3 unavailable: ' + ', '.join(capability.get('reasons', [])))
        layout.addWidget(label(status, muted=not capability.get('available')))
        self.experiment = QComboBox()
        for key, spec in experiments.items():
            self.experiment.addItem(f"{key} · {spec['official_example']}", key)
        layout.addWidget(label('Experiment / official example'))
        layout.addWidget(self.experiment)
        self.form = QFormLayout()
        layout.addLayout(self.form)
        self.notes = label('', muted=True)
        layout.addWidget(self.notes)
        buttons = QHBoxLayout()
        cancel = QPushButton('Cancel')
        cancel.clicked.connect(self.reject)
        run = QPushButton('Run real SNS-3')
        run.setEnabled(bool(capability.get('available')))
        run.clicked.connect(self.accept)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(run)
        layout.addLayout(buttons)
        self.experiment.currentIndexChanged.connect(self._rebuild)
        self._rebuild()

    def _rebuild(self) -> None:
        while self.form.rowCount():
            self.form.removeRow(0)
        self._fields = {}
        key = self.experiment.currentData()
        spec = self._experiments[key]
        for name in spec['allowed_arguments']:
            field = QLineEdit(self.DEFAULTS[key][name])
            self._fields[name] = field
            self.form.addRow(self.LABELS[name], field)
        self.notes.setText('\n'.join('• ' + note for note in spec['notes']))

    def values(self) -> tuple[str, dict[str, Any]]:
        result: dict[str, Any] = {key: field.text().strip() for key, field in self._fields.items()}
        for key in ('packetSize', 'utsPerBeam', 'endUsersPerUt'):
            if key in result:
                result[key] = int(result[key])
        for key in ('duration', 'simDurationInSeconds'):
            if key in result:
                result[key] = float(result[key])
        return self.experiment.currentData(), result

def label(text: str, *, muted: bool = False, heading: bool = False) -> QLabel:
    widget = QLabel(text)
    widget.setWordWrap(True)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if muted:
        widget.setProperty('tone', 'muted')
    if heading:
        widget.setProperty('heading', True)
    return widget

def panel(title: str, description: str) -> QWidget:
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(16, 16, 16, 16)
    heading = label(title)
    heading.setProperty('heading', True)
    layout.addWidget(heading)
    layout.addWidget(label(description, muted=True))
    layout.addStretch()
    return widget

def unavailable(text: str, reason: str) -> QPushButton:
    button = QPushButton(text)
    button.setEnabled(False)
    button.setToolTip(reason)
    button.setAccessibleDescription(reason)
    return button


class SatelliteExperimentStore(QObject):
    """The one local source of truth for selector, canvas, coverage, and AI UI.

    It stores configuration and visual UT placement only.  It never stores a
    calculated gain, rate, SINR, or other purported SNS-3 observation.
    """
    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.scenarios: list[dict[str, Any]] = []
        self.selectedScenarioId: Optional[str] = None
        self.experimentType = 'fixed_rate_cbr'
        self.activeBeams: list[int] = []
        self.selectedBeamId: Optional[int] = None
        self.utsPerBeam = 3
        self.endUsersPerUt = 1
        self.simulationTime = 10
        self.trafficDirection = 'Return Link'
        self.trafficRateKbps = 128
        self.packetSize = 512
        self.packetInterval = 0.032
        self.protocol = 'UDP'
        self.statistics: list[str] = []
        self.wizardState: dict[str, Any] = {}
        self.dirtyParameters: list[str] = []
        self.selectedUtId: Optional[str] = None
        self.satellitePlaced = False
        self.satellitePlacementMode = False
        self.uePlacementMode = False
        self._positions: dict[int, list[dict[str, Any]]] = {}

    def load_scenarios(self, scenarios: list[dict[str, Any]]) -> None:
        self.scenarios = scenarios
        if self.selectedScenarioId not in {s['id'] for s in scenarios}:
            self.selectedScenarioId = scenarios[0]['id'] if scenarios else None
        self._reset_beams()

    def scenario(self) -> Optional[dict[str, Any]]:
        return next((s for s in self.scenarios if s['id'] == self.selectedScenarioId), None)

    def beam_records(self) -> list[dict[str, Any]]:
        return list((self.scenario() or {}).get('beams', []))

    def set_scenario(self, scenario_id: str) -> None:
        if scenario_id not in {s['id'] for s in self.scenarios}:
            return
        self.selectedScenarioId = scenario_id
        self._reset_beams()

    def set_active_beams(self, beam_ids: list[int]) -> None:
        available = {record['beamId'] for record in self.beam_records()}
        self.activeBeams = [beam for beam in dict.fromkeys(beam_ids) if beam in available]
        if not self.activeBeams and available:
            self.activeBeams = [min(available)]
        if self.selectedBeamId not in self.activeBeams:
            self.selectedBeamId = self.activeBeams[0] if self.activeBeams else None
        self._build_positions()
        self.changed.emit()

    def set_uts_per_beam(self, value: int) -> None:
        self.utsPerBeam = max(1, min(int(value), 500))
        self._build_positions()
        self.changed.emit()

    def set_selected_beam(self, beam_id: int) -> None:
        if beam_id in {record['beamId'] for record in self.beam_records()}:
            self.selectedBeamId = beam_id
            self.selectedUtId = None
            self.changed.emit()

    def set_selected_ut(self, ut_id: str) -> None:
        self.selectedUtId = ut_id
        self.changed.emit()

    def enter_ue_placement(self) -> bool:
        if not self.satellitePlaced or self.selectedBeamId is None:
            return False
        self.uePlacementMode = True
        self.changed.emit()
        return True

    def add_ue(self, x: float, y: float) -> Optional[dict[str, Any]]:
        if not self.uePlacementMode or self.selectedBeamId is None:
            return None
        uts = self._positions.setdefault(self.selectedBeamId, [])
        ut = {'ueId': f'UE{sum(len(items) for items in self._positions.values()) + 1}',
              'beamId': self.selectedBeamId, 'beamRelativeX': max(.05, min(.95, x)),
              'beamRelativeY': max(.05, min(.95, y)), 'pendingSimulation': True}
        uts.append(ut); self.selectedUtId = ut['ueId']
        self.changed.emit()
        return ut

    def set_ut_position(self, ut_id: str, x: float, y: float) -> None:
        for uts in self._positions.values():
            for ut in uts:
                if ut['ueId'] == ut_id:
                    ut['beamRelativeX'] = max(0.05, min(0.95, x))
                    ut['beamRelativeY'] = max(0.05, min(0.95, y))
                    ut['pendingSimulation'] = True
                    self.changed.emit()
                    return

    def uts_for_selected_beam(self) -> list[dict[str, Any]]:
        return list(self._positions.get(self.selectedBeamId or -1, []))

    def beam_record(self, beam_id: Optional[int] = None) -> Optional[dict[str, Any]]:
        selected = self.selectedBeamId if beam_id is None else beam_id
        return next((record for record in self.beam_records() if record['beamId'] == selected), None)

    def total_ut_count(self) -> int:
        return len(self.activeBeams) * self.utsPerBeam

    def enter_satellite_placement(self) -> None:
        self.satellitePlacementMode = True
        self.changed.emit()

    def place_satellite_system(self) -> None:
        self.satellitePlaced = True
        self.satellitePlacementMode = False
        self.changed.emit()

    def _reset_beams(self) -> None:
        ids = [record['beamId'] for record in self.beam_records()]
        self.activeBeams = ids[:1]
        self.selectedBeamId = self.activeBeams[0] if self.activeBeams else None
        self._build_positions()
        self.changed.emit()

    def _build_positions(self) -> None:
        # UE placement is an editor action.  Configuration does not invent UT
        # locations or pretend that planned ``utsPerBeam`` are deployed UEs.
        self._positions = {beam: self._positions.get(beam, []) for beam in self.activeBeams}


class InfoLabel(QLabel):
    def __init__(self, text: str, help_text: str) -> None:
        super().__init__(text + '  ⓘ')
        self.setToolTip(help_text)
        self.setAccessibleDescription(help_text)


class SatelliteExperimentCanvas(QWidget):
    satelliteClicked = Signal()

    def __init__(self, store: SatelliteExperimentStore) -> None:
        super().__init__()
        self.store = store
        self.hovered: Optional[int] = None
        self.setMinimumHeight(370)
        self._dragging: Optional[str] = None
        self.setCursor(Qt.CursorShape.CrossCursor)
        store.changed.connect(self.update)

    def _disk(self) -> tuple[QPointF, float]:
        radius = max(60.0, min(self.width() * .30, self.height() * .40))
        return QPointF(self.width() * .55, self.height() * .54), radius

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(COLORS['background']))
        w, h = self.width(), self.height()
        if not self.store.satellitePlaced:
            painter.setPen(QColor(COLORS['muted']))
            prompt = ('Click to place the configured Satellite System.' if self.store.satellitePlacementMode
                      else 'Create a Satellite System to begin.\nClick here or use the left 「Satellite System」 button.')
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, prompt)
            return
        center, radius = self._disk()
        # This is the selected beam's editor coverage, not a measured gain map.
        painter.setPen(QPen(QColor(COLORS['border']), 1))
        painter.setBrush(QColor('#3a4a51')); painter.drawEllipse(center, radius, radius)
        painter.setBrush(QColor('#385961')); painter.drawEllipse(center, radius * .65, radius * .65)
        painter.setBrush(QColor('#2d7880')); painter.drawEllipse(center, radius * .32, radius * .32)
        painter.setPen(QColor(COLORS['muted']))
        painter.drawText(QRectF(center.x() - radius, center.y() - radius - 24, 80, 20), 'Low')
        painter.drawText(QRectF(center.x() - 34, center.y() - radius * .65 - 20, 80, 20), 'Medium')
        painter.drawText(QRectF(center.x() - 18, center.y() - 9, 48, 18), 'High')
        painter.setPen(QColor(COLORS['text']))
        painter.drawText(QRectF(18, 15, w - 36, 28), Qt.AlignmentFlag.AlignLeft,
                         f'Beam {self.store.selectedBeamId or "--"}')
        painter.setPen(QColor(COLORS['muted']))
        subtext = (f'● Place UE Mode  ·  Click inside Beam {self.store.selectedBeamId} to place terminals  ·  Placed: {len(self.store.uts_for_selected_beam())}'
                   if self.store.uePlacementMode else
                   f'{len(self.store.uts_for_selected_beam())} terminals  ·  Simulation pending')
        painter.drawText(QRectF(18, 43, w - 36, 22), Qt.AlignmentFlag.AlignLeft, subtext)
        painter.drawText(QRectF(18, 68, w - 36, 20), Qt.AlignmentFlag.AlignLeft,
                         'Energy zones are placement guidance. Real gain is obtained from the next SNS-3 run.')
        if self.store.uePlacementMode:
            finish = QRectF(w - 100, 42, 76, 28)
            painter.setPen(QPen(QColor(COLORS['accent']), 1)); painter.setBrush(QColor(COLORS['raised'])); painter.drawRoundedRect(finish, 5, 5)
            painter.setPen(QColor(COLORS['accent'])); painter.drawText(finish, Qt.AlignmentFlag.AlignCenter, 'Finish')
        for ut in self.store.uts_for_selected_beam():
            point = QPointF(center.x() + (ut['beamRelativeX'] - .5) * 2 * radius,
                            center.y() + (ut['beamRelativeY'] - .5) * 2 * radius)
            selected = ut['ueId'] == self.store.selectedUtId
            painter.setPen(QPen(QColor('#f4d27a') if selected else QColor(COLORS['text']), 2))
            painter.setBrush(QColor('#f4d27a') if selected else QColor(COLORS['raised']))
            painter.drawEllipse(point, 7 if selected else 5, 7 if selected else 5)
            if selected: painter.drawText(point + QPointF(10, -8), ut['ueId'])
        scenario = self.store.scenario() or {}
        name = scenario.get('displayName', scenario.get('name', 'Satellite System'))
        satellite_count = scenario.get('satelliteCount', '—')
        node = QRectF(w - 278, 18, 260, 76)
        painter.setPen(QPen(QColor(COLORS['accent']), 2)); painter.setBrush(QColor('#203844')); painter.drawRoundedRect(node, 10, 10)
        painter.setPen(QColor(COLORS['text'])); painter.drawText(node, Qt.AlignmentFlag.AlignCenter,
            f'🛰  {name}\nLEO Satellite System · {satellite_count} Satellites\n{len(self.store.activeBeams)} Active Beam · {self.store.utsPerBeam} UT/Beam        ›')

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self.store.satellitePlacementMode:
            self.store.place_satellite_system(); return
        if not self.store.satellitePlaced:
            self.satelliteClicked.emit(); return
        center, radius = self._disk()
        if self.store.uePlacementMode and QRectF(self.width() - 100, 42, 76, 28).contains(event.position()):
            self.store.uePlacementMode = False; self.store.changed.emit(); return
        if self.store.uePlacementMode:
            self.store.add_ue((event.position().x() - center.x()) / (2 * radius) + .5,
                              (event.position().y() - center.y()) / (2 * radius) + .5)
            return
        for ut in self.store.uts_for_selected_beam():
            point = QPointF(center.x() + (ut['beamRelativeX'] - .5) * 2 * radius,
                            center.y() + (ut['beamRelativeY'] - .5) * 2 * radius)
            if (point - event.position()).manhattanLength() < 14:
                self._dragging = ut['ueId']; self.store.set_selected_ut(ut['ueId']); return
        if QRectF(self.width() - 278, 18, 260, 76).contains(event.position()):
            self.satelliteClicked.emit()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._dragging:
            center, radius = self._disk()
            self.store.set_ut_position(self._dragging,
                (event.position().x() - center.x()) / (2 * radius) + .5,
                (event.position().y() - center.y()) / (2 * radius) + .5)

    def mouseReleaseEvent(self, _event: QMouseEvent) -> None:
        self._dragging = None


class BeamCoverageCanvas(QWidget):
    BASE_HEX_RADIUS = 14.0

    def __init__(self, store: SatelliteExperimentStore) -> None:
        super().__init__()
        self.store = store
        self.hovered: Optional[int] = None
        self.layout_mode = 'Spiral'
        self.pulse_beam: Optional[int] = None
        self._fit_scale = 1.0
        self.setMinimumHeight(230)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)
        store.changed.connect(self.update)

    def has_physical_geometry(self) -> bool:
        scenario = self.store.scenario() or {}
        geometry = scenario.get('beamGeometry') or scenario.get('beam_geometry')
        records = self.store.beam_records()
        if isinstance(geometry, dict) and geometry:
            return all(str(record['beamId']) in geometry or record['beamId'] in geometry
                       for record in records)
        # Also accept geometry parsed directly onto the authoritative beam
        # records; never infer coordinates from beam IDs.
        return bool(records) and all('x' in record and 'y' in record for record in records)

    @staticmethod
    def _spiral_axial(count: int) -> list[tuple[int, int]]:
        """Return a stable center-out axial spiral, spreading a partial ring."""
        if count <= 0:
            return []
        cells = [(0, 0)]
        directions = ((1, 0), (1, -1), (0, -1), (-1, 0), (-1, 1), (0, 1))
        ring = 1
        while len(cells) < count:
            ring_cells: list[tuple[int, int]] = []
            q, r = -ring, ring
            for dq, dr in directions:
                for _ in range(ring):
                    ring_cells.append((q, r))
                    q, r = q + dq, r + dr
            remaining = count - len(cells)
            if remaining >= len(ring_cells):
                cells.extend(ring_cells)
            else:
                # Evenly sample the circumference so an incomplete final ring
                # stays visually balanced instead of bunching on one side.
                selected: list[tuple[int, int]] = []
                used: set[int] = set()
                for index in range(remaining):
                    candidate = int(round(index * len(ring_cells) / remaining)) % len(ring_cells)
                    while candidate in used:
                        candidate = (candidate + 1) % len(ring_cells)
                    used.add(candidate)
                    selected.append(ring_cells[candidate])
                cells.extend(selected)
            ring += 1
        return cells

    def _physical_points(self, records: list[dict[str, Any]]) -> Optional[list[tuple[int, float, float]]]:
        scenario = self.store.scenario() or {}
        geometry = scenario.get('beamGeometry') or scenario.get('beam_geometry')
        if not isinstance(geometry, dict):
            if all('x' in record and 'y' in record for record in records):
                return [(record['beamId'], float(record['x']), float(record['y'])) for record in records]
            return None
        values: list[tuple[int, float, float]] = []
        for record in records:
            raw = geometry.get(record['beamId'], geometry.get(str(record['beamId'])))
            if not isinstance(raw, dict) or not all(key in raw for key in ('x', 'y')):
                return None
            values.append((record['beamId'], float(raw['x']), float(raw['y'])))
        return values

    def set_layout_mode(self, mode: str) -> None:
        if mode == 'Physical' and not self.has_physical_geometry():
            mode = 'Spiral'
        if mode in ('Physical', 'Spiral', 'ID Grid'):
            self.layout_mode = mode
            self.update()

    def focus_beam(self, beam_id: int) -> None:
        if beam_id not in {record['beamId'] for record in self.store.beam_records()}:
            return
        self.pulse_beam = beam_id
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        self.update()
        QTimer.singleShot(900, self._clear_pulse)

    def _clear_pulse(self) -> None:
        if self.pulse_beam is not None:
            self.pulse_beam = None
            self.update()

    def _points(self) -> list[tuple[int, QPointF]]:
        records = self.store.beam_records()
        if not records: return []
        import math
        if self.layout_mode == 'Physical':
            physical = self._physical_points(records)
            if physical is not None:
                self._fit_scale = 1.0
                min_x, max_x = min(x for _, x, _ in physical), max(x for _, x, _ in physical)
                min_y, max_y = min(y for _, _, y in physical), max(y for _, _, y in physical)
                span_x, span_y = max(1e-6, max_x - min_x), max(1e-6, max_y - min_y)
                return [(beam_id, QPointF(14 + (x - min_x) / span_x * max(1, self.width() - 28),
                                           18 + (y - min_y) / span_y * max(1, self.height() - 36)))
                        for beam_id, x, y in physical]
        if self.layout_mode == 'ID Grid':
            columns = max(1, int(math.sqrt(len(records))))
            raw = [(index % columns, index // columns) for index in range(len(records))]
            raw = [(x - (columns - 1) / 2, y - (len(records) // columns) / 2) for x, y in raw]
        else:
            axial = self._spiral_axial(len(records))
            raw = [(math.sqrt(3) * (q + r / 2), 1.5 * r) for q, r in axial]
        min_x, max_x = min(x for x, _ in raw), max(x for x, _ in raw)
        min_y, max_y = min(y for _, y in raw), max(y for _, y in raw)
        width = max(1.0, max_x - min_x); height = max(1.0, max_y - min_y)
        # Fit is one shared view transform. Hexes keep a single logical radius;
        # status (active/selected/has-UE) never changes cell geometry.
        pixel_scale = min((self.width() - 28) / width, (self.height() - 42) / height, 28.0)
        self._fit_scale = max(0.7, min(1.4, pixel_scale / 28.0))
        scale = pixel_scale
        return [(record['beamId'], QPointF(self.width() / 2 + x * scale, self.height() / 2 + y * scale - 4))
                for record, (x, y) in zip(records, raw)]

    def _hex(self, center: QPointF, radius_override: Optional[float] = None) -> QPolygonF:
        import math
        radius = radius_override or self.BASE_HEX_RADIUS * self._fit_scale
        return QPolygonF([QPointF(center.x() + radius * math.cos(math.pi / 6 + index * math.pi / 3),
                                  center.y() + radius * math.sin(math.pi / 6 + index * math.pi / 3))
                          for index in range(6)])

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(COLORS['background']))
        points = self._points()
        if not points:
            painter.setPen(QColor(COLORS['muted']))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, 'Select a satellite scenario to load beams.')
            return
        states: list[tuple[int, QPointF, bool, bool, bool]] = []
        for beam_id, point in points:
            selected = beam_id == self.store.selectedBeamId
            active = beam_id in self.store.activeBeams
            has_ue = bool(self.store._positions.get(beam_id))
            if selected:
                fill = QColor('#38a9aa') if has_ue else QColor(COLORS['raised'])
                outline = QColor('#f4d27a')
                width = 3
            elif has_ue:
                fill = QColor('#287b82')
                outline = QColor('#69e4df')
                width = 2
            elif active:
                fill = QColor(COLORS['accent'])
                outline = QColor(COLORS['border'])
                width = 1
            else:
                fill = QColor(COLORS['background'])
                outline = QColor(COLORS['border'])
                width = 1
            painter.setPen(QPen(outline, width)); painter.setBrush(fill)
            painter.drawPolygon(self._hex(point))
            states.append((beam_id, point, selected, active, has_ue))

        # Effects, badges and labels are separate layers. In particular, draw
        # every polygon before any badge so neighbouring cells cannot cover it.
        for beam_id, point, selected, _active, has_ue in states:
            if beam_id == self.pulse_beam:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor('#f7d77d'), 3))
                painter.drawPolygon(self._hex(point, 20.0 * self._fit_scale))
            if has_ue:
                # Badge stays inside the cell's upper-right quadrant.
                radius = self.BASE_HEX_RADIUS * self._fit_scale
                badge = QPointF(point.x() + radius * .45, point.y() - radius * .42)
                count = len(self.store._positions[beam_id])
                badge_text = '99+' if count > 99 else '9+' if count > 9 else str(count)
                badge_radius = max(5.0, min(7.0, radius * .28))
                painter.setPen(QPen(QColor(COLORS['background']), 1)); painter.setBrush(QColor('#f4d27a'))
                painter.drawEllipse(badge, badge_radius, badge_radius)
                painter.setPen(QColor(COLORS['background']))
                painter.drawText(QRectF(badge.x() - badge_radius, badge.y() - badge_radius,
                                        badge_radius * 2, badge_radius * 2), Qt.AlignmentFlag.AlignCenter, badge_text)
        for beam_id, point, selected, _active, _has_ue in states:
            if selected or beam_id == self.hovered:
                painter.setPen(QColor(COLORS['text']))
                painter.drawText(QRectF(point.x() - 13, point.y() - 8, 26, 16), Qt.AlignmentFlag.AlignCenter, str(beam_id))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        for beam_id, point in self._points():
            if (point - event.position()).manhattanLength() < 18:
                self.store.set_selected_beam(beam_id); self.focus_beam(beam_id); return

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        previous = self.hovered; self.hovered = None
        for beam_id, point in self._points():
            if (point - event.position()).manhattanLength() < 18:
                self.hovered = beam_id
                record = self.store.beam_record(beam_id) or {}
                ut_count = len(self.store._positions.get(beam_id, []))
                self.setToolTip(f"Beam {beam_id}\nStatus: {'Active' if beam_id in self.store.activeBeams else 'Available'}\nUT: {ut_count}\nServing SAT: Pending simulation\nGateway: GW-{record.get('gatewayId', 'pending')}\nClick to inspect")
                break
        if previous != self.hovered: self.update()


class BeamCoveragePanel(QWidget):
    def __init__(self, store: SatelliteExperimentStore) -> None:
        super().__init__()
        self.store = store
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        top = QHBoxLayout()
        top.addWidget(label('Beam'))
        self.selector = QComboBox(); self.selector.setAccessibleName('SNS-3 Beam selector')
        top.addWidget(self.selector, 1)
        self.ut_count = label('UTs: 0', muted=True); top.addWidget(self.ut_count)
        layout.addLayout(top)
        layout_row = QHBoxLayout()
        layout_row.addWidget(label('Layout'))
        self.layout_selector = QComboBox(); self.layout_selector.setAccessibleName('Beam layout selector')
        self.layout_selector.addItems(['Spiral', 'Physical', 'ID Grid'])
        self.layout_selector.setToolTip('Spiral is a stable schematic center-out layout. Physical requires verified scenario beam geometry.')
        self.layout_selector.currentTextChanged.connect(self._layout_changed)
        layout_row.addWidget(self.layout_selector, 1)
        layout.addLayout(layout_row)
        self.stats = label('Available 0 · Active 0 · Has UE 0 · Selected —', muted=True); layout.addWidget(self.stats)
        jump = QHBoxLayout(); jump.addWidget(label('Jump to Beam'))
        self.jump_input = QLineEdit(); self.jump_input.setPlaceholderText('Beam ID'); self.jump_input.setMaximumWidth(84); self.jump_input.returnPressed.connect(self._jump)
        jump.addWidget(self.jump_input); layout.addLayout(jump)
        self.coverage = BeamCoverageCanvas(store); layout.addWidget(self.coverage, 1)
        self.layout_hint = label('Schematic View ⓘ', muted=True)
        self.layout_hint.setToolTip('This honeycomb is a schematic beam selector and does not represent geographic coverage.')
        layout.addWidget(self.layout_hint)
        self.legend = label('● Selected   ■ Has UE   ⬡ Active   ◇ Available', muted=True)
        self.legend.setAccessibleName('Beam state legend')
        layout.addWidget(self.legend)
        self.details = label('', muted=True); self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.details)
        self.selector.currentIndexChanged.connect(self._selected)
        store.changed.connect(self.refresh)
        self.refresh()

    def _selected(self, _index: int) -> None:
        value = self.selector.currentData()
        if value is not None:
            beam_id = int(value)
            self.store.set_selected_beam(beam_id)
            self.coverage.focus_beam(beam_id)

    def _layout_changed(self, value: str) -> None:
        self.coverage.set_layout_mode(value)
        self.layout_hint.setText('Physical Layout' if value == 'Physical' else 'Schematic View ⓘ')

    def _jump(self) -> None:
        try: beam_id = int(self.jump_input.text().strip())
        except ValueError: return
        if self.store.beam_record(beam_id) is None:
            self.jump_input.setToolTip(f'Beam {beam_id} is not available in this scenario.')
            return
        self.store.set_selected_beam(beam_id)
        self.coverage.focus_beam(beam_id)
        self.jump_input.setToolTip(f'Focused Beam {beam_id}')

    def refresh(self) -> None:
        self.selector.blockSignals(True); self.selector.clear()
        records = self.store.beam_records()
        for record in records:
            beam_id = record['beamId']
            count = self.store.utsPerBeam if beam_id in self.store.activeBeams else 0
            self.selector.addItem(f'Beam {beam_id}     {count} UT', beam_id)
        current = self.selector.findData(self.store.selectedBeamId)
        self.selector.setCurrentIndex(current)
        self.selector.setEnabled(bool(records)); self.selector.blockSignals(False)
        physical_available = self.coverage.has_physical_geometry()
        model = self.layout_selector.model()
        physical_item = model.item(1) if hasattr(model, 'item') else None
        if physical_item is not None:
            physical_item.setEnabled(physical_available)
            physical_item.setToolTip('Physical Layout unavailable: this scenario has no verified beam geometry.'
                                     if not physical_available else 'Use scenario-provided physical beam coordinates.')
        if not physical_available and self.layout_selector.currentText() == 'Physical':
            self.layout_selector.setCurrentText('Spiral')
        self.coverage.set_layout_mode(self.layout_selector.currentText())
        self.layout_hint.setText('Physical Layout' if self.layout_selector.currentText() == 'Physical' else 'Schematic View ⓘ')
        record = self.store.beam_record()
        uts = self.store.uts_for_selected_beam()
        self.ut_count.setText(f'UTs: {len(uts)}')
        total_ue = sum(len(items) for items in self.store._positions.values())
        self.stats.setText(f'Available {len(records)} · Active {len(self.store.activeBeams)} · Has UE {total_ue} · Selected {self.store.selectedBeamId or "—"}')
        if record:
            self.details.setText(
                f"Current Beam: {record['beamId']}\n"
                f"Serving SAT: Pending simulation\n"
                f"Gateway: GW-{record['gatewayId']}    User frequency ID: {record['userFrequencyId']}\n"
                f"Feeder frequency ID: {record['feederFrequencyId']}    UT count: {len(uts)}\n"
                + (f"Selected UE: {self.store.selectedUtId}\n" if self.store.selectedUtId else '')
                + ('Position pending next simulation.' if any(ut['pendingSimulation'] for ut in uts) else ''))
        else:
            self.details.setText('No beam data loaded. Select a Satellite Scenario.')


class HypatiaWebPage(QWebEnginePage if QWebEnginePage is not None else QObject):
    """Surface Cesium/imagery console failures to the desktop log path."""
    console_message = Signal(str, bool)

    def javaScriptConsoleMessage(self, level: Any, message: str, line: int, source: str) -> None:
        text = f'Cesium JS [{level}] {source}:{line}: {message}'
        # Route/coordinate diagnostics are developer-console information, not
        # user-facing map failures.  Qt only promotes JavaScript errors.
        enum = getattr(QWebEnginePage, 'JavaScriptConsoleMessageLevel', None)
        error_level = getattr(enum, 'ErrorMessageLevel', None)
        self.console_message.emit(text, level == error_level)
        print(text, file=sys.stderr)


class HypatiaMapCanvas(QWidget):
    """Lazy WebEngine host for genuine generated Hypatia/Cesium documents."""
    console_message = Signal(str, bool)
    visualization_status = Signal(dict)
    def __init__(self) -> None:
        super().__init__(); self.setMinimumHeight(230); self.web: Optional[QWebEngineView] = None; self._server = None; self._pending_timeline: dict[str, Any] | None = None; self.native_url: QUrl | None = None
        self._layout = QVBoxLayout(self); self._layout.setContentsMargins(0, 0, 0, 0)
        self.placeholder = label('Select a discovered Hypatia dataset and generate its visualization.', muted=True)
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter); self._layout.addWidget(self.placeholder)

    def load_visualization(self, filename: str, timeline: dict[str, Any] | None = None) -> None:
        """Load one native page and inject its timeline only after loadFinished.

        A WebEngine navigation discards the old document immediately.  Queue a
        completed Hypatia timeline *before* navigation, rather than executing
        ``loadHypatiaTimeline`` against the prior snapshot page and then
        replacing it.  This matters for the default view's Reload action.
        """
        if timeline is not None:
            self._pending_timeline = timeline
        if QWebEngineView is None:
            self.placeholder.setText('QWebEngineView is unavailable. Install the PySide6 WebEngine runtime.')
            return
        if self.web is None:
            self.web = QWebEngineView(self)
            # Generated documents are application-owned local files. Cesium
            # must be allowed to fetch the explicit public imagery provider;
            # this does not expose arbitrary user-local HTML in the view.
            if QWebEngineSettings is not None:
                self.web.settings().setAttribute(
                    QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
            if QWebEnginePage is not None:
                page = HypatiaWebPage(self.web)
                page.console_message.connect(self.console_message.emit)
                self.web.setPage(page)
            self.web.loadFinished.connect(self._loaded)
            self._layout.addWidget(self.web)
        self.placeholder.hide()
        self.web.show()
        file_path = Path(filename).resolve()
        # A localhost origin avoids the local-file → remote-imagery security
        # path that makes Cesium's tile provider fail in WebEngine.  The server
        # is application-owned, loopback-only, and scoped to this one output
        # directory; users never start or configure it themselves.
        if self._server is not None:
            self._server.shutdown(); self._server.server_close(); self._server = None
        output_root = file_path.parent
        asset_root = Path(__file__).resolve().parent / '.hypatia-vendor/cesium-1.57.0/Build/Cesium'
        class AssetHandler(SimpleHTTPRequestHandler):
            def translate_path(self, requested: str) -> str:
                if requested.startswith('/cesium/'):
                    relative = requested[len('/cesium/'):].split('?', 1)[0]
                    candidate = (asset_root / relative).resolve()
                    if asset_root == candidate or asset_root in candidate.parents:
                        return str(candidate)
                return super().translate_path(requested)
            def log_message(self, *_args: Any) -> None:
                return
        handler = functools.partial(AssetHandler, directory=str(output_root))
        self._server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.native_url = QUrl(f'http://127.0.0.1:{self._server.server_port}/{file_path.name}')
        self.web.load(self.native_url)

    def open_native_in_browser(self) -> bool:
        """Developer-only comparison hook: open the exact embedded native URL."""
        if self.native_url is None:
            return False
        return QDesktopServices.openUrl(self.native_url)

    def closeEvent(self, event: Any) -> None:
        if self._server is not None:
            self._server.shutdown(); self._server.server_close(); self._server = None
        super().closeEvent(event)

    def _loaded(self, success: bool) -> None:
        if not success:
            self.placeholder.setText('Hypatia HTML could not be loaded. See job details.')
            self.placeholder.show()
        elif self.web is not None:
            self.web.page().runJavaScript('window.leoVisualizationStatus && window.leoVisualizationStatus();', self._status)
            if self._pending_timeline is not None:
                timeline, self._pending_timeline = self._pending_timeline, None
                self.load_timeline(timeline)

    def _status(self, value: Any) -> None:
        if isinstance(value, dict): self.visualization_status.emit(value)

    def camera(self, command: dict[str, Any]) -> bool:
        if self.web is None:
            return False
        payload = json.dumps(command).replace('</', '<\\/')
        self.web.page().runJavaScript(f'window.leoSatelliteCamera && window.leoSatelliteCamera({payload});')
        return True

    def update_route(self, observation: dict[str, Any]) -> bool:
        # Deprecated custom-renderer bridge; native SatViz owns its path.
        if self.web is None: return False
        payload = json.dumps({'source': observation.get('source_endpoint'),
                              'destination': observation.get('destination_endpoint'),
                              'gen_time_ms': observation.get('gen_time_ms'),
                              'route_nodes': observation.get('route_nodes', [])})
        self.web.page().runJavaScript(f'window.leoUpdateRoute && window.leoUpdateRoute({payload});')
        return True

    def load_timeline(self, timeline: dict[str, Any]) -> bool:
        # Deprecated custom-renderer bridge; production uses native view.html.
        if self.web is None: self._pending_timeline = timeline; return False
        # One line per completed timeline load: useful when diagnosing a bridge
        # payload, without adding noise to the 200 ms UI poll loop.
        print('[Hypatia] source_ground_station =', timeline.get('source'))
        print('[Hypatia] destination_ground_station =', timeline.get('destination'))
        # Cesium's clock begins at zero for this view, while Hypatia timeline
        # files use absolute GEN_TIME. Normalize only the browser copy; the
        # cached Hypatia evidence remains in its original absolute times.
        browser_timeline = json.loads(json.dumps(timeline))
        origin = int(browser_timeline.get('start_time_ms', 0))
        if origin:
            browser_timeline['start_time_ms'] = 0
            browser_timeline['end_time_ms'] = int(browser_timeline['end_time_ms']) - origin
            browser_timeline.setdefault('metadata', {})['absolute_start_time_ms'] = origin
            for samples in browser_timeline.get('satellite_tracks', {}).values():
                for sample in samples:
                    sample['time_ms'] = int(sample['time_ms']) - origin
            for event in browser_timeline.get('route_events', []):
                event['time_ms'] = int(event['time_ms']) - origin
                if 'start_time_ms' in event:
                    event['start_time_ms'] = int(event['start_time_ms']) - origin
            for sample in browser_timeline.get('rtt_samples', []):
                sample['time_ms'] = int(sample['time_ms']) - origin
        payload = json.dumps(browser_timeline).replace('</', '<\\/')
        self.web.page().runJavaScript(f'window.loadHypatiaTimeline && window.loadHypatiaTimeline({payload});')
        return True

    def timeline_command(self, command: dict[str, Any]) -> bool:
        if self.web is None: return False
        name = command.get('timeline_command'); args = command.get('arguments', {})
        if name not in {'playTimeline', 'pauseTimeline', 'restartTimeline', 'setSimulationTime', 'setPlaybackSpeed', 'focusTarget', 'setDisplayMode'}: return False
        argument = args.get('ms', args.get('multiplier', args.get('target', args.get('mode', None))))
        native = {'playTimeline': 'play', 'pauseTimeline': 'pause', 'restartTimeline': 'restart',
                  'setSimulationTime': 'seek', 'setPlaybackSpeed': 'setSpeed', 'focusTarget': None}.get(name)
        if native:
            native_call = f'window.leoHypatia.{native}()' if argument is None else f'window.leoHypatia.{native}({json.dumps(argument)})'
            legacy_call = f'window.{name} && window.{name}()' if argument is None else f'window.{name} && window.{name}({json.dumps(argument)})'
            call = f'window.leoHypatia ? {native_call} : ({legacy_call})'
        else:
            call = f'window.{name} && window.{name}()' if argument is None else f'window.{name} && window.{name}({json.dumps(argument)})'
        self.web.page().runJavaScript(call); return True


class HypatiaMapPanel(QWidget):
    def __init__(self, lab: Lab) -> None:
        super().__init__(); self.lab = lab; self.runtime_store = HypatiaRuntimeStore(); self._job_id: str | None = None; self._loaded_html: str | None = None; self._network_datasets: list[dict[str, Any]] = []; self._selected_dataset_id: str | None = None; self._preferred_dataset_id: str | None = None; self._continue_after_custom = False; self._continue_after_hypatia_wizard = False; self._hypatia_wizard_callback = None
        self._last_failed_job: dict[str, Any] | None = None
        self._last_hypatia_job: dict[str, Any] | None = None
        # Opening the panel must be read-only.  A native timeline is loaded
        # only after the user/assistant has analysed an explicit request.
        self._default_native_view = False
        layout = QVBoxLayout(self); layout.setContentsMargins(12, 12, 12, 12)
        row = QHBoxLayout(); row.addWidget(label('Network'))
        self.constellation = QComboBox(); self.constellation.currentTextChanged.connect(self._refresh_dataset_selection); row.addWidget(self.constellation, 1); layout.addLayout(row)
        dataset = QHBoxLayout(); dataset.addWidget(label('Dataset'))
        self.dataset = label('Scanning installed Hypatia datasets…', muted=True); dataset.addWidget(self.dataset, 1)
        self.generate_dataset = QPushButton('Generate New Dataset'); self.generate_dataset.clicked.connect(self._toggle_dataset_form)
        self.generate_dataset.setToolTip('Advanced operation. Dataset generation is a separate background job and never starts from Analyze Path.')
        self.generate_dataset.setVisible(False)
        dataset.addWidget(self.generate_dataset); layout.addLayout(dataset)
        self.dataset_form = QFrame(); form = QFormLayout(self.dataset_form); form.setContentsMargins(0, 4, 0, 8)
        self.dataset_preset = QComboBox(); self.dataset_preset.addItems(['Quick Test', 'Demo', 'Detailed', 'Custom'])
        self.dataset_duration = QSpinBox(); self.dataset_duration.setRange(1, 3600); self.dataset_duration.setSuffix(' s')
        self.dataset_step = QSpinBox(); self.dataset_step.setRange(1, 60000); self.dataset_step.setSuffix(' ms')
        self.dataset_isl = QComboBox(); self.dataset_isl.addItems(['isls_plus_grid', 'isls_none'])
        self.dataset_gs = QComboBox(); self.dataset_gs.addItems(['ground_stations_top_100', 'ground_stations_paris_moscow_grid', 'Custom'])
        self.dataset_routing = QComboBox(); self.dataset_routing.addItems(['algorithm_free_one_only_over_isls', 'algorithm_free_one_only_gs_relays', 'algorithm_paired_many_only_over_isls'])
        self.dataset_threads = QComboBox(); self.dataset_threads.setEditable(True)
        self.dataset_threads.addItems(['Auto', '1', '2', '4', '6', '8', '12', '16'])
        self.dataset_threads.setCurrentText('Auto')
        capabilities = self.lab.hypatia_visualization.adapter.capabilities()
        self.dataset_threads.setToolTip(
            f"Auto resolves to {capabilities.get('auto_threads')} of {capabilities.get('cpu_count')} CPU cores. "
            'This worker count applies to forwarding-state generation.')
        self.estimated_states = label('', muted=True)
        self.start_dataset_generation = QPushButton('Start Dataset Generation'); self.start_dataset_generation.clicked.connect(self._generate_dataset)
        form.addRow('Preset', self.dataset_preset); form.addRow('Duration Time', self.dataset_duration)
        form.addRow('Time Step', self.dataset_step); form.addRow('ISL', self.dataset_isl)
        form.addRow('Ground Station Set', self.dataset_gs); form.addRow('Routing Algorithm', self.dataset_routing)
        form.addRow('Threads', self.dataset_threads); form.addRow('Estimated States', self.estimated_states)
        form.addRow('', self.start_dataset_generation); self.dataset_form.setVisible(False); layout.addWidget(self.dataset_form)
        self.dataset_preset.currentTextChanged.connect(self._apply_dataset_preset)
        self.dataset_duration.valueChanged.connect(self._update_estimated_states)
        self.dataset_step.valueChanged.connect(self._update_estimated_states)
        self._apply_dataset_preset('Quick Test')
        endpoints = QHBoxLayout(); endpoints.addWidget(label('Source'))
        self.source = QLineEdit('Taipei'); endpoints.addWidget(self.source, 1); endpoints.addWidget(label('Destination'))
        self.destination = QLineEdit('Tokyo'); endpoints.addWidget(self.destination, 1); layout.addLayout(endpoints)
        self.source.setReadOnly(False); self.destination.setReadOnly(False)
        validation = QHBoxLayout(); self.source_validation = label('', muted=True); self.destination_validation = label('', muted=True)
        validation.addWidget(self.source_validation, 1); validation.addWidget(self.destination_validation, 1); layout.addLayout(validation)
        self.source_validation.setVisible(False); self.destination_validation.setVisible(False)
        status_row = QHBoxLayout(); status_row.addWidget(label('Status'))
        self.status = label('● Status: Pending dataset selection', muted=True)
        self.status.setAccessibleName('Hypatia readiness status')
        status_row.addWidget(self.status, 1); layout.addLayout(status_row)
        self._station_model = QStringListModel(self)
        for editor in (self.source, self.destination):
            completer = QCompleter(self._station_model, editor); completer.setCaseSensitivity(Qt.CaseInsensitive)
            completer.setFilterMode(Qt.MatchContains); editor.setCompleter(completer)
        self.add_station = QPushButton('Add missing station to Custom Dataset')
        self.add_station.clicked.connect(self._prepare_custom_station); self.add_station.setVisible(False); layout.addWidget(self.add_station)
        self.custom_station_form = QFrame(); custom = QFormLayout(self.custom_station_form)
        self.custom_name = QLineEdit(); self.custom_latitude = QDoubleSpinBox(); self.custom_latitude.setRange(-90, 90); self.custom_latitude.setDecimals(6)
        self.custom_longitude = QDoubleSpinBox(); self.custom_longitude.setRange(-180, 180); self.custom_longitude.setDecimals(6)
        self.custom_base = label('', muted=True); self.custom_confirm = QPushButton('Add & Generate Dataset'); self.custom_confirm.clicked.connect(self._create_and_generate_custom_dataset)
        custom.addRow('Name', self.custom_name); custom.addRow('Latitude', self.custom_latitude); custom.addRow('Longitude', self.custom_longitude)
        custom.addRow('Base Set', self.custom_base); custom.addRow('', self.custom_confirm)
        self.custom_station_form.setVisible(False); layout.addWidget(self.custom_station_form)
        self.source.textChanged.connect(self._refresh_dataset_selection); self.destination.textChanged.connect(self._refresh_dataset_selection)
        self.source.editingFinished.connect(self._sync_hypatia_wizard_from_ui)
        self.destination.editingFinished.connect(self._sync_hypatia_wizard_from_ui)
        # ``activated`` is user-only; ``currentTextChanged`` also fires while
        # restoring a persisted wizard and would overwrite it at startup.
        self.constellation.activated.connect(self._sync_hypatia_wizard_from_ui)
        self.canvas = HypatiaMapCanvas(); layout.addWidget(self.canvas, 1)
        controls = QHBoxLayout(); self.analyse = QPushButton('Analyze Path')
        self.analyse.clicked.connect(self._analyse); controls.addWidget(self.analyse)
        self.cancel = QPushButton('Cancel'); self.cancel.clicked.connect(self._cancel); self.cancel.setVisible(False); controls.addWidget(self.cancel)
        self.retry = QPushButton('Retry'); self.retry.clicked.connect(self._retry_last_generation); self.retry.setVisible(False); controls.addWidget(self.retry)
        self.view_log = QPushButton('View Log'); self.view_log.clicked.connect(self._view_last_log); self.view_log.setVisible(False); controls.addWidget(self.view_log)
        if os.environ.get('HYPATIA_DEVELOPER_MODE') == '1':
            self.open_native = QPushButton('Open Native SatViz in Browser')
            self.open_native.clicked.connect(self._open_native_in_browser)
            controls.addWidget(self.open_native)
        self.gating_reason = label('', muted=True); self.gating_reason.setAccessibleName('Analyze Path availability reason')
        controls.addWidget(self.gating_reason, 1)
        layout.addLayout(controls)
        self.canvas.console_message.connect(self._map_console_message)
        self.canvas.visualization_status.connect(self._visualization_status)
        self._load_runtime()
        self.poller = QTimer(self); self.poller.setInterval(200); self.poller.timeout.connect(self._poll); self.poller.start()

    _STAGE_LABELS = {
        'DATASET_GENERATING': 'Generating Dataset',
        'GENERATING_GROUND_STATIONS': 'Generating Ground Stations',
        'GENERATING_TLES': 'Generating TLEs',
        'GENERATING_ISLS': 'Generating ISLs',
        'GENERATING_GSL_INTERFACES': 'Generating GSL Interfaces',
        'GENERATING_FORWARDING_STATE': 'Generating Forwarding State',
        'ENDPOINT_VALIDATION': 'Validating Endpoints',
        'ROUTE_CALCULATING': 'Preparing Route',
        'ROUTE_ANALYSIS': 'Analyzing Path / RTT',
        'VISUALIZATION_GENERATING': 'Generating Native SatViz',
        'DATASET_READY': 'Dataset Ready',
        'READY_FOR_PATH_ANALYSIS': 'Ready for Path Analysis',
    }

    def _set_readiness_text(self, state: str, text: str, reason: str = '') -> None:
        """Render the visible panel state from the one readiness decision."""
        normalized = state.upper()
        glyph = {'READY': '●', 'READY_FOR_PATH_ANALYSIS': '●', 'COMPLETED': '●', 'GENERATING': '◉', 'ANALYZING': '◉',
                 'PENDING': '●', 'NOT_READY': '●', 'FAILED': '●', 'CANCELLED': '●'}.get(normalized, '●')
        color = {'READY': COLORS['success'], 'READY_FOR_PATH_ANALYSIS': COLORS['success'], 'COMPLETED': COLORS['success'],
                 'GENERATING': COLORS['warning'], 'ANALYZING': COLORS['warning'],
                 'PENDING': COLORS['idle'], 'NOT_READY': COLORS['idle'],
                 'FAILED': COLORS['error'], 'CANCELLED': COLORS['idle']}.get(normalized, COLORS['muted'])
        self.status.setText(f'{glyph} Status: {text}')
        self.status.setStyleSheet(f'color:{color}; font-weight:600;')
        self.gating_reason.setText(reason)

    def _active_job(self) -> dict[str, Any] | None:
        job_id = self._job_id
        if not job_id:
            recovered = next((job for job in self.lab.hypatia_visualization.jobs.values()
                              if job.get('dataset_generation') and job.get('status') in {'QUEUED', 'RUNNING', 'WARNING', 'FAILED'}), None)
            job_id = str(recovered['job_id']) if recovered else None
        if not job_id:
            return None
        try:
            job = self.lab.hypatia_visualization.status(job_id)
            # An orchestration job may be waiting on a dedicated dataset child.
            # The child owns the actual PID/progress and therefore is the one
            # authoritative runtime input for all visual state while it runs.
            child_id = job.get('dataset_generation_job_id')
            if isinstance(child_id, str) and child_id in self.lab.hypatia_visualization.jobs:
                child = self.lab.hypatia_visualization.status(child_id)
                if child.get('status') in {'QUEUED', 'RUNNING', 'WARNING', 'FAILED', 'CANCELLED'}:
                    return child
            return job
        except (KeyError, OSError):
            return None

    def readiness(self) -> dict[str, Any]:
        """Return the shared authoritative Hypatia runtime snapshot."""
        job = self._active_job()
        selected = next((item for item in self._network_datasets
                         if item.get('dataset_id') == self._selected_dataset_id), None)
        names = (selected or {}).get('ground_stations', [])
        known = {self.lab.hypatia_visualization.endpoint_resolver.normalized_name(name) for name in names}
        source = self.source.text().strip()
        destination = self.destination.text().strip()
        source_found = self.lab.hypatia_visualization.endpoint_resolver.normalized_name(source) in known
        destination_found = self.lab.hypatia_visualization.endpoint_resolver.normalized_name(destination) in known
        if not job and self._last_failed_job:
            job = self._last_failed_job
        # Source/destination appear in messages as well as the boolean facts.
        if job:
            job = {**job, 'source': source, 'destination': destination}
        return self.runtime_store.recompute(selected_dataset=selected, active_job=job,
                                            source_found=source_found, destination_found=destination_found,
                                            source_name=source, destination_name=destination)

    def _apply_readiness(self) -> dict[str, Any]:
        state = self.readiness()
        running = state['status'] in {'GENERATING', 'ANALYZING'}
        failed = state['status'] == 'FAILED'
        warning = state['status'] == 'WARNING'
        failed_job = state.get('job') or {}
        self.analyse.setEnabled(bool(state['can_analyze']))
        self.cancel.setVisible(running)
        self.retry.setVisible(failed and bool(failed_job.get('dataset_generation')))
        self.view_log.setVisible((failed or warning) and bool(failed_job.get('stdout_log') or failed_job.get('stderr_log') or failed_job.get('log_path')))
        stage = self._STAGE_LABELS.get(str(state['stage']).upper(), str(state['stage']).replace('_', ' ').title())
        self._set_readiness_text(state['status'], stage, state['reason'])
        target = state.get('target_dataset')
        if state['status'] == 'GENERATING' and isinstance(target, dict):
            target_parts = [target.get('network'),
                            f"{target['duration_s']} s" if target.get('duration_s') is not None else None,
                            f"{target['time_step_ms']} ms" if target.get('time_step_ms') is not None else None,
                            target.get('isl_mode')]
            target_label = ' · '.join(str(part) for part in target_parts if part)
            previous = state.get('dataset') or {}
            previous_label = (f"{previous.get('duration_s')} s · {previous.get('step_ms')} ms"
                              if previous else 'None')
            self.dataset.setText(f'Creating New Dataset · {target_label}\nPrevious Ready Dataset: {previous_label}')
            self.dataset.setToolTip('The previous dataset remains available but is not used for the current NEW-dataset request.')
        return state

    def runtime_status_record(self) -> dict[str, Any]:
        """Expose readiness plus operational provenance to the shared dialog."""
        state = self.readiness()
        record = dict(state.get('job') or {})
        if isinstance(record.get('started_at_monotonic'), (int, float)):
            record['elapsed_sec'] = max(0.0, time.monotonic() - float(record['started_at_monotonic']))
        elif isinstance(record.get('started_at'), (int, float)):
            record['elapsed_sec'] = max(0.0, time.time() - float(record['started_at']))
        record.update(status=state['status'], stage=state['stage'], message=state['reason'])
        dataset = state.get('dataset')
        if dataset:
            record.setdefault('dataset_id', dataset.get('dataset_id'))
        record.setdefault('type', 'hypatia_readiness')
        return record

    def _view_last_log(self) -> None:
        job = self._last_failed_job or self._last_hypatia_job or {}
        path = job.get('stdout_log') or job.get('stderr_log') or job.get('log_path')
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _retry_last_generation(self) -> None:
        if not self._last_failed_job:
            return
        failed = self._last_failed_job
        self._last_failed_job = None
        request = failed.get('request') if isinstance(failed.get('request'), dict) else {}
        if failed.get('dataset_generation') and request:
            try:
                job = self.lab.hypatia_visualization.generate_network_dataset(
                    network=request['network'], duration_sec=int(request['duration_sec']), step_ms=int(request['step_ms']),
                    isl_mode=request['isl_mode'], ground_station_set=request['ground_station_set'],
                    routing_algorithm=request['routing_algorithm'], threads=request.get('requested_threads', request.get('threads', 'auto')),
                    force_new_dataset=bool(request.get('force_new_dataset', False)))
            except (KeyError, ValueError) as error:
                self._last_failed_job = failed
                self._set_readiness_text('FAILED', 'Failed', f'Unable to retry dataset generation: {error}')
                return
            self._job_id = job['job_id']; self._last_hypatia_job = dict(job)
            self.generate_dataset.setEnabled(False); self.start_dataset_generation.setEnabled(False)
            self._apply_readiness()
            return
        self._generate_dataset()

    def _load_runtime(self) -> None:
        result = self.lab.call('hypatia_visualization_runtime', {})
        data = result.get('data', {})
        constellations = self.lab.hypatia_visualization.adapter.get_available_constellations()
        self.constellation.clear(); self.constellation.addItems(constellations)
        index = self.constellation.findText('starlink_550')
        if index >= 0: self.constellation.setCurrentIndex(index)
        self.constellation.setEnabled(True)
        ready = data.get('status') == 'READY' and bool(constellations)
        self._network_datasets = data.get('network_datasets', [])
        if not ready:
            missing = ', '.join(data.get('missing_components', [])) or result.get('error', 'Data unavailable')
            self._set_readiness_text('PENDING', 'Hypatia components unavailable', missing)
        else:
            # Discover and validate endpoints, but never start a viewer or
            # background Hypatia job as a side effect of panel construction.
            self.apply_hypatia_wizard_state(self.lab.hypatia_wizard.snapshot())

    def _load_default_native_view(self) -> None:
        """Load the verified city-to-city snapshot without dataset generation."""
        if self._job_id is not None:
            return
        try:
            job = self.lab.hypatia_visualization.generate_default_native_timeline()
        except ValueError as error:
            self.status.setText('Default Hypatia view unavailable: ' + str(error))
            return
        self._last_failed_job = None
        self._job_id = job['job_id']; self._last_hypatia_job = dict(job)
        self._apply_readiness()

    def _refresh_dataset_selection(self, *_args: Any) -> None:
        source, destination = self.source.text().strip(), self.destination.text().strip()
        datasets = [item for item in self._network_datasets if item.get('network') == self.constellation.currentText()
                    and item.get('status') == 'READY']
        if datasets:
            normalize = self.lab.hypatia_visualization.endpoint_resolver.normalized_name
            source_key, destination_key = normalize(source), normalize(destination)

            def has_both_endpoints(value: dict[str, Any]) -> bool:
                names = {normalize(name) for name in value.get('ground_stations', [])}
                return source_key in names and destination_key in names

            # A selected wizard dataset remains authoritative.  Otherwise choose
            # a READY dataset that can actually analyse the currently entered
            # endpoints before preferring a longer-duration, incompatible one.
            # This prevents the contradictory "Hypatia Ready" / disabled
            # Analyze Path state when a compatible Custom Dataset already exists.
            preferred = next((value for value in datasets if value['dataset_id'] == self._preferred_dataset_id), None)
            endpoint_compatible = [value for value in datasets if has_both_endpoints(value)]
            # A persisted preference may refer to a base dataset that lacks a
            # newly entered endpoint.  In that case it is not a valid current
            # selection; use an installed compatible dataset instead.
            candidates = ([preferred] if preferred is not None and has_both_endpoints(preferred)
                          else endpoint_compatible or ([preferred] if preferred is not None else datasets))
            item = sorted(candidates, key=lambda value: (-value['duration_s'], value['step_ms']))[0]
            self._selected_dataset_id = item['dataset_id']
            self.lab.hypatia_visualization.selected_network_dataset_id = item['dataset_id']
            names = item.get('ground_stations', []); self._station_model.setStringList(names)
            station_names = {normalize(name) for name in names}
            source_found = source_key in station_names
            destination_found = destination_key in station_names
            self.dataset.setText(f"Dataset Ready · {item['duration_s']} s · {item['step_ms']} ms · {item['isl_mode']}")
            self.source_validation.setText('● Available' if source_found else f'× {self.source.text().strip()} is not available in this dataset')
            self.destination_validation.setText('● Available' if destination_found else f'× {self.destination.text().strip()} is not available in this dataset')
            self.source_validation.setVisible(True)
            self.destination_validation.setVisible(True)
            active = self._active_job()
            self.add_station.setVisible((not source_found or not destination_found) and not active)
            missing = self.source.text().strip() if not source_found else self.destination.text().strip()
            self.add_station.setText(f'Add {missing} to Custom Dataset')
        else:
            self._station_model.setStringList([]); self.source_validation.setText(''); self.destination_validation.setText('')
            self.add_station.setVisible(False); self.custom_station_form.setVisible(False)
            self._selected_dataset_id = None
            self.lab.hypatia_visualization.selected_network_dataset_id = None
            self.dataset.setText('No installed dataset for selected network')
        self._apply_readiness()

    def _sync_hypatia_wizard_from_ui(self, *_args: Any) -> None:
        """The panel is one editor of the same persisted assistant state."""
        changes = {'network': self.constellation.currentText(),
                   'source': self.source.text().strip(),
                   'destination': self.destination.text().strip()}
        if self._selected_dataset_id:
            selected = next((item for item in self._network_datasets
                             if item.get('dataset_id') == self._selected_dataset_id), None)
            if selected is not None:
                self.lab.hypatia_wizard.set_dataset(selected)
        result = self.lab.call('sync_hypatia_wizard', {'changes': changes})
        if result.get('status') != 'ok':
            self.status.setText('Hypatia wizard sync failed: ' + result.get('error', 'unknown error'))

    def apply_hypatia_wizard_state(self, state: dict[str, Any]) -> None:
        """Apply assistant changes without launching a simulation or loading HTML."""
        network = state.get('network')
        if isinstance(network, str):
            index = self.constellation.findText(network)
            if index >= 0: self.constellation.setCurrentIndex(index)
        for editor, key in ((self.source, 'source'), (self.destination, 'destination')):
            value = state.get(key)
            if isinstance(value, str) and value != editor.text(): editor.setText(value)
        dataset_id = state.get('dataset_id')
        if isinstance(dataset_id, str): self._preferred_dataset_id = dataset_id
        self._refresh_dataset_selection()

    def _toggle_dataset_form(self) -> None:
        self.dataset_form.setVisible(not self.dataset_form.isVisible())

    def _apply_dataset_preset(self, preset: str) -> None:
        values = {'Quick Test': (20, 1000), 'Demo': (60, 500), 'Detailed': (200, 100)}
        custom = preset == 'Custom'
        if preset in values:
            self.dataset_duration.setValue(values[preset][0]); self.dataset_step.setValue(values[preset][1])
        self.dataset_duration.setEnabled(True); self.dataset_step.setEnabled(True)
        self._update_estimated_states()

    def _update_estimated_states(self, *_args: Any) -> None:
        states = self.dataset_duration.value() * 1000 // self.dataset_step.value()
        self.estimated_states.setText(f'{states:,} · Longer duration and smaller steps cost more to generate.')

    def _prepare_custom_station(self) -> None:
        if not self._selected_dataset_id: return
        source_check = self.lab.hypatia_visualization.check_ground_station(self._selected_dataset_id, self.source.text())
        missing = self.source.text().strip() if source_check['status'] == 'NOT_FOUND' else self.destination.text().strip()
        resolved = self.lab.hypatia_visualization.resolve_ground_station(missing)
        self.custom_name.setText(missing); self.custom_base.setText(self.dataset.text())
        if resolved['status'] == 'FOUND_IN_LOCAL_CATALOG':
            station = resolved['station']; self.custom_name.setText(station['name'])
            self.custom_latitude.setValue(station['latitude']); self.custom_longitude.setValue(station['longitude'])
            self.status.setText(f'{missing} found in the local Hypatia catalog. Confirm to create a Custom Dataset.')
        else:
            self.custom_latitude.setValue(0); self.custom_longitude.setValue(0)
            self.status.setText(f'No local coordinates found for {missing}. Enter latitude and longitude; coordinates will not be guessed.')
        self.custom_station_form.setVisible(True)

    def _create_and_generate_custom_dataset(self) -> None:
        if not self._selected_dataset_id: return
        try:
            custom = self.lab.hypatia_visualization.create_custom_ground_station_set(
                self._selected_dataset_id, [{'name': self.custom_name.text().strip(),
                'latitude': self.custom_latitude.value(), 'longitude': self.custom_longitude.value()}])
            config = custom['generation_config']
            job = self.lab.hypatia_visualization.generate_network_dataset(**config, threads=self._dataset_thread_value())
        except ValueError as error:
            self.status.setText('Custom Ground Station unavailable: ' + str(error)); return
        self._job_id = job['job_id']; self.custom_station_form.setVisible(False); self.add_station.setVisible(False)
        self._last_failed_job = None; self._last_hypatia_job = dict(job)
        self._continue_after_custom = True
        self._apply_readiness()

    def _analyse(self) -> None:
        readiness = self._apply_readiness()
        if not readiness['can_analyze']:
            return
        try:
            if self.lab.hypatia_visualization.renderer_mode == 'native':
                if self._selected_dataset_id is None:
                    raise ValueError('A compatible installed dataset is required')
                job = self.lab.hypatia_visualization.analyze_existing_dataset(
                    dataset_id=self._selected_dataset_id, source=self.source.text(), destination=self.destination.text())
            else:
                job = self.lab.hypatia_visualization.generate_satellite_timeline(
                    network=self.constellation.currentText(), source=self.source.text(), destination=self.destination.text(),
                    duration_sec=10, step_ms=1000)
        except ValueError as error:
            self.status.setText('Hypatia generation unavailable: ' + str(error)); return
        self._last_failed_job = None; self._job_id = job['job_id']; self._last_hypatia_job = dict(job)
        self._apply_readiness()

    def _cancel(self) -> None:
        if self._job_id:
            self.lab.hypatia_visualization.cancel(self._job_id)

    def _generate_dataset(self) -> None:
        if self.dataset_gs.currentText() == 'Custom':
            self._prepare_custom_station()
            return
        try:
            job = self.lab.hypatia_visualization.generate_network_dataset(
                network=self.constellation.currentText(), duration_sec=self.dataset_duration.value(),
                step_ms=self.dataset_step.value(), isl_mode=self.dataset_isl.currentText(),
                ground_station_set=self.dataset_gs.currentText(), routing_algorithm=self.dataset_routing.currentText(),
                threads=self._dataset_thread_value())
        except ValueError as error:
            self.status.setText('Dataset generation unavailable: ' + str(error)); return
        self._last_failed_job = None; self._job_id = job['job_id']; self._last_hypatia_job = dict(job)
        self.generate_dataset.setEnabled(False); self.start_dataset_generation.setEnabled(False)
        self._apply_readiness()

    def _dataset_thread_value(self) -> str | int:
        value = self.dataset_threads.currentText().strip()
        if value.casefold() == 'auto': return 'auto'
        try: return int(value)
        except ValueError as error: raise ValueError('Threads must be Auto or a positive integer') from error

    def _open_native_in_browser(self) -> None:
        if not self.canvas.open_native_in_browser():
            self.status.setText('Native SatViz has not been loaded yet.')

    def _map_console_message(self, message: str, is_error: bool) -> None:
        """Keep developer payloads in the console, not in the status label."""
        if not is_error:
            return
        if 'LEO_TIMELINE_INVALID_GEODETIC' in message:
            if 'source_ground_station' in message:
                self.status.setText('Map error: Invalid source ground-station coordinates: ' + self.source.text().strip())
                return
            if 'destination_ground_station' in message:
                self.status.setText('Map error: Invalid destination ground-station coordinates: ' + self.destination.text().strip())
                return
        self.status.setText('Map error: ' + message[-160:])

    def _poll(self) -> None:
        if self._job_id:
            job = self.lab.hypatia_visualization.status(self._job_id)
            self._last_hypatia_job = dict(job)
            status = job['status']
            if status in {'QUEUED', 'RUNNING'}:
                self._apply_readiness()
            elif status == 'COMPLETED':
                if job.get('dataset_generation'):
                    self._preferred_dataset_id = job.get('dataset_id')
                    dataset = job.get('dataset')
                    if isinstance(dataset, dict):
                        self.lab.hypatia_wizard.set_dataset(dataset)
                    self._job_id = None; self.cancel.setVisible(False); self._last_failed_job = None; self._load_runtime()
                    self.generate_dataset.setEnabled(True); self.start_dataset_generation.setEnabled(True)
                    if self._continue_after_hypatia_wizard and self._hypatia_wizard_callback:
                        # Return to the assistant orchestration path so a
                        # pending verified endpoint can be added to the newly
                        # generated base dataset before Path / RTT starts.
                        self._continue_after_hypatia_wizard = False
                        callback = self._hypatia_wizard_callback
                        self._hypatia_wizard_callback = None
                        QTimer.singleShot(0, callback)
                    elif self._continue_after_custom and self.analyse.isEnabled():
                        self._continue_after_custom = False
                        QTimer.singleShot(0, self._analyse)
                else:
                    self._load_completed(job)
            elif status in {'FAILED', 'CANCELLED'}:
                self._last_failed_job = dict(job) if status == 'FAILED' else None
                self._job_id = None; self.cancel.setVisible(False)
                self._continue_after_hypatia_wizard = False
                self._hypatia_wizard_callback = None
                if self._default_native_view:
                    self.analyse.setEnabled(True)
                else:
                    self._refresh_dataset_selection()
                self.generate_dataset.setEnabled(True)
                self.start_dataset_generation.setEnabled(True)
                self._apply_readiness()
        for command in self.lab.hypatia_visualization.drain_view_commands():
            if 'timeline_command' in command: self.canvas.timeline_command(command)
            else: self.canvas.camera(command)

    def _load_completed(self, job: dict[str, Any]) -> None:
        html_file = job.get('html')
        observation = job.get('observation', {}); timeline = job.get('timeline')
        if isinstance(timeline, dict):
            if isinstance(html_file, str) and (self.lab.hypatia_visualization.renderer_mode == 'native' or self._loaded_html is None):
                self.canvas.load_visualization(html_file, timeline); self._loaded_html = html_file
            else: self.canvas.load_timeline(timeline)
            self._job_id = None; self.cancel.setVisible(False); self._apply_readiness(); return
        if isinstance(html_file, str) and (self.lab.hypatia_visualization.renderer_mode == 'native' or self._loaded_html is None):
            self.canvas.load_visualization(html_file); self._loaded_html = html_file
        elif isinstance(observation, dict) and self._loaded_html is not None:
            # Keep one Cesium viewer alive: a new GEN_TIME updates only route
            # entities in the browser and never reloads HTML or reinitializes
            # imagery.  The route job itself remains background-only.
            self.canvas.update_route(observation)
        rtt = observation.get('rtt_ms')
        hops = observation.get('hop_count')
        prefix = 'Hypatia: Loaded from cache' if job.get('cache_hit') else 'Hypatia: Completed'
        self._job_id = None; self.cancel.setVisible(False); self._apply_readiness()

    def _visualization_status(self, state: dict[str, Any]) -> None:
        map_state = state.get('map', 'LOADING')
        if map_state == 'FAILED':
            self.status.setText('Cesium ✓ · Base map unavailable · Hypatia route remains available.')
        elif map_state == 'READY':
            self._apply_readiness()


class SatelliteScenarioPopover(QDialog):
    def __init__(self, parent: QWidget, store: SatelliteExperimentStore) -> None:
        super().__init__(parent, Qt.WindowType.Popup)
        self.store = store; self.setWindowTitle('Satellite Scenario'); self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.addWidget(label('Satellite Scenario'))
        self.scenario = QComboBox()
        for item in store.scenarios:
            self.scenario.addItem(item['name'], item['id'])
            description = (f"Orbit type: {item.get('orbitType', 'unknown')}\n"
                           f"Satellite count: {item.get('satelliteCount', 'Data unavailable')}\n"
                           f"Beam count: {len(item.get('beams', []))}\n"
                           f"{item.get('description', 'No registry description.')}\n"
                           f"Suitable for: {', '.join(item.get('suitableFor', [])) or 'Data unavailable'}")
            self.scenario.setItemData(self.scenario.count() - 1, description, Qt.ItemDataRole.ToolTipRole)
        self.experiment = QComboBox()
        self.experiment.addItem('Fixed-Rate Traffic', 'fixed_rate_cbr'); self.experiment.addItem('Random Access', 'random_access'); self.experiment.addItem('Multi-user + ACM', 'acm_training')
        self.active = QLineEdit(); self.uts = QSpinBox(); self.uts.setRange(1, 500); self.uts.setValue(store.utsPerBeam)
        self._add(layout, 'Scenario', self.scenario, 'Installed SNS-3 scenarios, discovered by the backend. Hover or select an item for its registry description.')
        self.description = label('', muted=True); layout.addWidget(self.description)
        self._add(layout, 'Experiment', self.experiment, 'Selects the official SNS-3 example family. It does not emulate an experiment.')
        self._add(layout, 'Active Beams', self.active, 'Spot beams used in this run. A scenario can contain more beams than this experiment activates.')
        self._add(layout, 'UTs per Beam', self.uts, 'Each enabled beam receives this many UTs. Active Beams = 16 and UTs per Beam = 3 gives Total UT = 48.')
        locked = QLineEdit(); locked.setText('Derived from installed fwdConf.txt'); locked.setEnabled(False); locked.setToolTip('Beam count depends on antenna patterns, frequency plan, and GW mapping, so it cannot be edited alone.')
        self._add(layout, 'Beam Count', locked, locked.toolTip())
        advanced = QPushButton('Advanced Settings'); advanced.clicked.connect(self._custom); layout.addWidget(advanced)
        apply = QPushButton('Apply scenario'); apply.clicked.connect(self._apply); layout.addWidget(apply)
        self.scenario.currentIndexChanged.connect(self._scenario_changed)
        self.scenario.setCurrentIndex(self.scenario.findData(store.selectedScenarioId)); self._scenario_changed()

    def _add(self, layout: QVBoxLayout, text: str, widget: QWidget, help_text: str) -> None:
        row = QHBoxLayout(); row.addWidget(InfoLabel(text, help_text)); row.addWidget(widget, 1); layout.addLayout(row)

    def _scenario_changed(self, _index: int = 0) -> None:
        item = next((s for s in self.store.scenarios if s['id'] == self.scenario.currentData()), {})
        suitable = ', '.join(item.get('suitableFor', [])) or 'Data unavailable'
        self.description.setText(f"Orbit: {item.get('orbitType', 'unknown')}  ·  Beams: {len(item.get('beams', []))}\n{item.get('description', 'No registry description.')}\nSuitable for: {suitable}")
        self.active.setText(', '.join(str(b['beamId']) for b in item.get('beams', [])[:1]))

    def _apply(self) -> None:
        try: beams = [int(part.strip()) for part in self.active.text().split(',') if part.strip()]
        except ValueError: QMessageBox.warning(self, 'Invalid beams', 'Use comma-separated numeric beam IDs.'); return
        self.store.set_scenario(str(self.scenario.currentData())); self.store.experimentType = str(self.experiment.currentData())
        self.store.set_uts_per_beam(self.uts.value()); self.store.set_active_beams(beams); self.accept()

    def _custom(self) -> None:
        dialog = CustomScenarioDialog(self, self.store)
        dialog.exec()


class CustomScenarioDialog(QDialog):
    def __init__(self, parent: QWidget, store: SatelliteExperimentStore) -> None:
        super().__init__(parent); self.store = store; self.setWindowTitle('Custom Scenario'); self.setMinimumWidth(440)
        layout = QFormLayout(self); self.base = QComboBox()
        for item in store.scenarios: self.base.addItem(item['name'], item['id'])
        self.time = QSpinBox(); self.time.setRange(1, 3600); self.time.setValue(store.simulationTime); self.time.setSuffix(' seconds')
        self.beams = QLineEdit(', '.join(map(str, store.activeBeams))); self.uts = QSpinBox(); self.uts.setRange(1, 500); self.uts.setValue(store.utsPerBeam)
        self.users = QSpinBox(); self.users.setRange(1, 100); self.users.setValue(store.endUsersPerUt)
        self.direction = QComboBox(); self.direction.addItems(['Return Link', 'Forward Link'])
        self.rate = QSpinBox(); self.rate.setRange(1, 10_000_000); self.rate.setValue(store.trafficRateKbps); self.rate.setSuffix(' kbps')
        self.protocol = QComboBox(); self.protocol.addItems(['UDP', 'TCP'])
        for title, field, tip in (("Base Scenario", self.base, 'Installed scenario that supplies locked antenna, waveform, frequency, and GW mapping.'), ("Simulation Time", self.time, 'SNS-3 simulation execution time in seconds.'), ("Active Beams", self.beams, 'Comma-separated installed beam IDs to enable.'), ("UTs per Beam", self.uts, 'UT count in each active beam.'), ("End Users per UT", self.users, 'Application users installed for each UT.'), ("Traffic Direction", self.direction, 'Return Link is UT user to GW user; Forward Link is GW user to UT user.'), ("Traffic Rate", self.rate, 'Fixed traffic rate for each UT.'), ("Protocol", self.protocol, 'Application transport protocol.')):
            layout.addRow(InfoLabel(title, tip), field)
        button = QPushButton('Apply Custom Scenario'); button.clicked.connect(self._apply); layout.addRow(button)

    def _apply(self) -> None:
        try: beams = [int(v.strip()) for v in self.beams.text().split(',') if v.strip()]
        except ValueError: QMessageBox.warning(self, 'Invalid beams', 'Use comma-separated numeric beam IDs.'); return
        self.store.set_scenario(str(self.base.currentData())); self.store.simulationTime = self.time.value(); self.store.endUsersPerUt = self.users.value(); self.store.trafficDirection = self.direction.currentText(); self.store.trafficRateKbps = self.rate.value(); self.store.protocol = self.protocol.currentText(); self.store.set_uts_per_beam(self.uts.value()); self.store.set_active_beams(beams); self.accept()


class SatelliteScenarioDialog(QDialog):
    """Two-step Satellite System creation, separate from generic node creation."""
    def __init__(self, parent: QWidget, store: SatelliteExperimentStore) -> None:
        super().__init__(parent); self.store = store; self.setWindowTitle('建立低軌衛星系統'); self.setMinimumWidth(620)
        layout = QVBoxLayout(self); self.pages = QStackedWidget(); layout.addWidget(self.pages)
        picker = QWidget(); picker_layout = QVBoxLayout(picker); picker_layout.addWidget(label('選擇 SNS-3 Scenario'))
        filters = QHBoxLayout(); self.filter = QComboBox(); self.filter.addItems(['LEO', 'GEO', 'All']); filters.addWidget(self.filter); picker_layout.addLayout(filters)
        body = QHBoxLayout(); self.scenarios = QListWidget(); body.addWidget(self.scenarios, 1); self.metadata = label('', muted=True); self.metadata.setMinimumWidth(230); body.addWidget(self.metadata); picker_layout.addLayout(body)
        nav = QHBoxLayout(); cancel = QPushButton('取消'); cancel.clicked.connect(self.reject); custom = QPushButton('Custom Scenario'); custom.clicked.connect(self._custom); next_button = QPushButton('下一步'); next_button.clicked.connect(lambda: self.pages.setCurrentIndex(1)); nav.addWidget(cancel); nav.addWidget(custom); nav.addStretch(); nav.addWidget(next_button); picker_layout.addLayout(nav); self.pages.addWidget(picker)
        config = QWidget(); form = QFormLayout(config); self.experiment = QComboBox(); self.experiment.addItem('Fixed-Rate Traffic', 'fixed_rate_cbr'); self.experiment.addItem('Random Access', 'random_access'); self.experiment.addItem('Multi-user + ACM', 'acm_training')
        self.active = QLineEdit(); self.uts = QSpinBox(); self.uts.setRange(1, 500); self.uts.setValue(store.utsPerBeam); self.duration = QSpinBox(); self.duration.setRange(1, 3600); self.duration.setValue(store.simulationTime); self.duration.setSuffix(' seconds')
        self._row(form, 'Scenario', label('Pending selection'), 'Scenario data remains locked to the installed SNS-3 folder.')
        self.config_scenario = form.itemAt(form.rowCount() - 1, QFormLayout.ItemRole.FieldRole).widget()
        self._row(form, 'Experiment', self.experiment, 'Uses an official SNS-3 example family.')
        self._row(form, 'Active Beams', self.active, 'Installed beam IDs enabled in this experiment.')
        self._row(form, 'UTs per Beam', self.uts, 'Number of UTs in each active beam. 16 beams × 3 UTs = 48 UTs.')
        self._row(form, 'Simulation Time', self.duration, 'SNS-3 execution time in seconds.')
        advanced = QPushButton('進階設定 / Custom Scenario'); advanced.clicked.connect(lambda: CustomScenarioDialog(self, store).exec()); form.addRow(advanced)
        buttons = QHBoxLayout(); back = QPushButton('返回'); back.clicked.connect(lambda: self.pages.setCurrentIndex(0)); create = QPushButton('建立'); create.clicked.connect(self._create); buttons.addWidget(back); buttons.addStretch(); buttons.addWidget(create); form.addRow(buttons); self.pages.addWidget(config)
        self.filter.currentTextChanged.connect(self._populate); self.scenarios.currentRowChanged.connect(self._selected); self._populate('LEO')

    def _row(self, form: QFormLayout, title: str, field: QWidget, tip: str) -> None:
        form.addRow(InfoLabel(title, tip), field)

    def _populate(self, filter_value: str) -> None:
        self.scenarios.clear()
        for item in self.store.scenarios:
            if filter_value != 'All' and item.get('orbitType') != filter_value: continue
            display = item.get('name', item['id'])
            self.scenarios.addItem(display)
            self.scenarios.item(self.scenarios.count() - 1).setData(Qt.ItemDataRole.UserRole, item['id'])
        if self.scenarios.count(): self.scenarios.setCurrentRow(0)

    def _selected(self, _row: int) -> None:
        current = self.scenarios.currentItem()
        scenario_id = current.data(Qt.ItemDataRole.UserRole) if current else None
        item = next((value for value in self.store.scenarios if value['id'] == scenario_id), {})
        self.metadata.setText(f"{item.get('name', '')}\n\nScenario\n{scenario_id or ''}\n\nOrbit Type: {item.get('orbitType', '')}\nSatellites: {item.get('satelliteCount', 'Data unavailable')}\n\n{item.get('description', '')}\n\nSuitable for\n" + '\n'.join('• ' + value for value in item.get('suitableFor', [])) + '\n\nSource\nSNS-3 scenario data')
        self.config_scenario.setText(item.get('name', 'Pending selection'))
        self.active.setText(', '.join(str(b['beamId']) for b in item.get('beams', [])[:1]))

    def _create(self) -> None:
        current = self.scenarios.currentItem()
        if current is None: return
        try: beams = [int(value.strip()) for value in self.active.text().split(',') if value.strip()]
        except ValueError: QMessageBox.warning(self, 'Invalid beams', 'Use comma-separated numeric beam IDs.'); return
        self.store.set_scenario(str(current.data(Qt.ItemDataRole.UserRole))); self.store.experimentType = str(self.experiment.currentData()); self.store.simulationTime = self.duration.value(); self.store.set_uts_per_beam(self.uts.value()); self.store.set_active_beams(beams); self.store.enter_satellite_placement(); self.accept()

    def _custom(self) -> None:
        dialog = CustomScenarioDialog(self, self.store)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.store.enter_satellite_placement()
            self.accept()

class LabWindow(QMainWindow):
    """One ordinary-user window owns one initially empty, unprovisioned Lab."""
    def __init__(self) -> None:
        super().__init__()
        self.lab = Lab(current_mode='SNS3_SATELLITE')
        self.tool_registry = LeoToolRegistry(self.lab)
        self.agent: Optional[LeoToolCallAgent] = None
        self.worker_signals = WorkerSignals(self)
        self.worker_signals.completed.connect(self._on_worker_completed)
        self._worker_kind = ''
        self.config_manager = ConfigManager(self.lab, self)
        self.satellite_store = SatelliteExperimentStore()
        self._link_mode = False
        self._link_source: Optional[NodeItem] = None
        self.link_items: dict[str, LinkItem] = {}
        self.setObjectName('lab-window')
        self.setWindowTitle('LEO Network Lab — 原生桌面工作台')
        self.resize(1440, 940)
        self.setMinimumSize(1000, 720)
        self.setDockNestingEnabled(True)
        self.setStatusBar(RuntimeFooterStatusBar(self))
        self.setStyleSheet(self._theme())
        self._menus_and_toolbar()
        self._workspace()
        self._navigation()
        self._navigate_satellite(0)
        self._inspectors()
        self._console()
        self._install_runtime_footer()
        self.satellite_store.changed.connect(self._sync_satellite_controls)
        self._load_satellite_scenarios()
        # Node placement state
        self._placement_mode: Optional[str] = None  # role being placed, or None
        self._placement_temp_item: Optional[NodeItem] = None  # temporary item showing preview
        self.statusBar().clearMessage()
        self.runtime_timer = QTimer(self)
        self.runtime_timer.timeout.connect(self._refresh_runtime_capsules)
        self.runtime_timer.start(1000)
        self._refresh_runtime_capsules()
        self.resizeDocks([self.navigation_dock, self.map_dock], [210, 300], Qt.Orientation.Horizontal)
        QTimer.singleShot(0, lambda: self.set_ai_dock_expanded(False))

    def _install_runtime_footer(self) -> None:
        """Put compact runtime controls in the one, actual application footer."""
        self.container_footer_status = RuntimeStatusCapsule('Container', self)
        self.container_footer_status.set_status('IDLE', 'None')
        self.container_footer_status.setEnabled(False)
        footer = self.statusBar()
        if isinstance(footer, RuntimeFooterStatusBar):
            footer.set_runtime_widgets([
                self.sns3_status, self.hypatia_toolbar_status,
                self.container_footer_status, self.ai_toolbar_status,
            ])

    @staticmethod
    def _theme() -> str:
        c = COLORS
        return f"""\nQWidget {{ background: {c['surface']}; color: {c['text']}; }}\nQMainWindow, QGraphicsView {{ background: {c['background']}; }}\nQMenuBar, QToolBar, QStatusBar {{ padding: 6px; }}\nQToolBar {{ spacing: 8px; border-bottom: 1px solid {c['border']}; }}\nQDockWidget::title {{ padding: 10px; background: {c['raised']}; }}\nQLabel[heading=true] {{ font-size: 18px; font-weight: bold; }}\nQLabel[tone=muted] {{ color: {c['muted']}; }}\nQPushButton {{ padding: 9px 12px; border: 1px solid {c['border']}; border-radius: 4px; }}\nQPushButton:hover:enabled {{ background: {c['raised']}; }}\nQPushButton:disabled, QLineEdit:disabled {{ color: {c['muted']}; background: {c['background']}; }}\nQPushButton:focus, QListWidget:focus, QTabBar::tab:focus {{ border: 2px solid {c['accent']}; }}\nQListWidget {{ border: none; padding: 8px; }}\nQListWidget::item {{ padding: 13px 10px; }}\nQListWidget::item:selected {{ color: {c['accent']}; background: {c['raised']}; }}\nQTabBar::tab {{ padding: 9px 15px; }}\nQTabBar::tab:selected {{ color: {c['accent']}; border-bottom: 2px solid {c['accent']}; }}\nQTextEdit, QLineEdit {{ background: {c['background']}; border: 1px solid {c['border']}; padding: 8px; }}\nQSplitter::handle {{ background: {c['border']}; }}\n"""

    def keyPressEvent(self, event: Any) -> None:
        if event.key() == Qt.Key.Key_Escape and self.satellite_store.uePlacementMode:
            self.satellite_store.uePlacementMode = False
            self.satellite_store.changed.emit()
            self.statusBar().showMessage('UE placement mode finished.')
            event.accept(); return
        if event.key() == Qt.Key.Key_Delete and self.satellite_store.selectedUtId:
            selected = self.satellite_store.selectedUtId
            for beam, uts in self.satellite_store._positions.items():
                self.satellite_store._positions[beam] = [ut for ut in uts if ut['ueId'] != selected]
            self.satellite_store.selectedUtId = None; self.satellite_store.changed.emit()
            event.accept(); return
        super().keyPressEvent(event)

    def _menus_and_toolbar(self) -> None:
        file_menu = self.menuBar().addMenu('檔案')
        for title in ('匯出草稿（未接入）', '讀取草稿（未接入）'):
            action = file_menu.addAction(title)
            action.setEnabled(False)
        quit_action = file_menu.addAction('結束')
        quit_action.setShortcut('Ctrl+Q')
        quit_action.triggered.connect(self.close)
        self.view_menu = self.menuBar().addMenu('檢視')
        help_menu = self.menuBar().addMenu('說明')
        about = help_menu.addAction('目前功能範圍')
        about.triggered.connect(self.show_scope)
        toolbar = QToolBar('實驗操作', self)
        toolbar.setObjectName('experiment-toolbar')
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        toolbar.addWidget(label('ChatMiniNet / 實驗工作台'))
        self.btn_sns3 = QPushButton('SNS-3 Satellite Mode')
        self.btn_sns3.clicked.connect(self.open_sns3_experiment)
        toolbar.addWidget(self.btn_sns3)
        # A compact duplicate selector remains available for keyboard users; the
        # primary palette lives in the left dock.
        self.role_selector = QComboBox()
        self.role_selector.addItems(['請選擇角色'] + sorted(list(ROLES)))
        self.role_selector.setToolTip('選擇要放置的節點角色')
        self.role_selector.currentIndexChanged.connect(self.on_role_selector_changed)
        toolbar.addWidget(self.role_selector)
        self.provider_selector = QComboBox()
        self.provider_selector.addItems(['openrouter', 'gemini'])
        self.provider_selector.currentTextChanged.connect(self.configure_agent)
        toolbar.addWidget(self.provider_selector)
        self.provider_status = label('LLM 未設定', muted=True)
        toolbar.addWidget(self.provider_status)
        toolbar.addSeparator()
        self.btn_add_node = QPushButton('新增節點')
        self.btn_add_node.setVisible(False)  # Hidden, replaced by role selector
        self.btn_add_node.clicked.connect(self.on_add_node)
        toolbar.addWidget(self.btn_add_node)
        self.btn_create_link = QPushButton('建立鏈路')
        self.btn_create_link.setEnabled(False)
        self.btn_create_link.clicked.connect(self.on_create_link)
        toolbar.addWidget(self.btn_create_link)
        self.btn_open_terminal = QPushButton('開啟終端')
        self.btn_open_terminal.setEnabled(False)
        self.btn_open_terminal.clicked.connect(self.on_open_terminal)
        toolbar.addWidget(self.btn_open_terminal)
        self.btn_view_changes = QPushButton('檢視變更')
        self.btn_view_changes.clicked.connect(self.on_view_changes)
        toolbar.addWidget(self.btn_view_changes)
        self.btn_execute = QPushButton('確認執行')
        self.btn_execute.setEnabled(False)
        self.btn_execute.clicked.connect(self.on_execute_confirmed)
        toolbar.addWidget(self.btn_execute)
        self.btn_stop = QPushButton('停止實驗')
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.on_stop)
        toolbar.addWidget(self.btn_stop)
        self.btn_create_flow = QPushButton('建立流量')
        self.btn_create_flow.setEnabled(False)
        self.btn_create_flow.clicked.connect(self.on_create_flow)
        toolbar.addWidget(self.btn_create_flow)
        self.btn_flow_status = QPushButton('流量狀態')
        self.btn_flow_status.setEnabled(False)
        self.btn_flow_status.clicked.connect(self.on_flow_status)
        toolbar.addWidget(self.btn_flow_status)
        self.btn_stop_flow = QPushButton('停止流量')
        self.btn_stop_flow.setEnabled(False)
        self.btn_stop_flow.clicked.connect(self.on_stop_flow)
        toolbar.addWidget(self.btn_stop_flow)
        toolbar.addWidget(label('  原生 Qt · Containernet 與 SNS-3 分離', muted=True))
        self.btn_execute_links = QPushButton('執行鏈路')
        self.btn_execute_links.setEnabled(False)
        self.btn_execute_links.clicked.connect(self.on_execute_links)
        toolbar.addWidget(self.btn_execute_links)
        # Satellite Mode is the primary product surface.  Legacy Mininet
        # controls remain implemented for compatibility, but are not global UI.
        self._legacy_toolbar_widgets = [self.btn_sns3, self.role_selector, self.provider_selector,
            self.provider_status, self.btn_add_node, self.btn_create_link, self.btn_open_terminal,
            self.btn_view_changes, self.btn_execute, self.btn_stop, self.btn_create_flow,
            self.btn_flow_status, self.btn_stop_flow, self.btn_execute_links]
        for widget in self._legacy_toolbar_widgets: widget.setVisible(False)
        for action in toolbar.actions():
            action.setVisible(False)
        toolbar.addSeparator()
        toolbar.addWidget(label('Scenario'))
        self.satellite_scenario_selector = QComboBox(); self.satellite_scenario_selector.setMinimumWidth(160)
        self.satellite_scenario_selector.currentIndexChanged.connect(self._toolbar_scenario_changed); toolbar.addWidget(self.satellite_scenario_selector)
        toolbar.addWidget(label('Experiment'))
        self.satellite_experiment_selector = QComboBox()
        self.satellite_experiment_selector.addItem('Fixed Rate', 'fixed_rate_cbr'); self.satellite_experiment_selector.addItem('Random Access', 'random_access'); self.satellite_experiment_selector.addItem('Multi User + ACM', 'acm_training')
        self.satellite_experiment_selector.currentIndexChanged.connect(self._toolbar_experiment_changed); toolbar.addWidget(self.satellite_experiment_selector)
        self.sns3_status = RuntimeStatusCapsule('SNS-3', self)
        self.hypatia_toolbar_status = RuntimeStatusCapsule('Hypatia', self)
        self.ai_toolbar_status = RuntimeStatusCapsule('AI', self)
        capsule_style = (f"QToolButton{{min-height:30px;padding:2px 10px;border:1px solid {COLORS['border']};"
                         f"border-radius:15px;background:{COLORS['background']};color:{COLORS['muted']};}}"
                         f"QToolButton:hover{{background:{COLORS['raised']};}}"
                         f"QToolButton:focus{{border:2px solid {COLORS['accent']};}}"
                         f"QToolButton[runtimeState=\"ready\"],QToolButton[runtimeState=\"complete\"]{{color:{COLORS['success']};}}"
                         f"QToolButton[runtimeState=\"running\"],QToolButton[runtimeState=\"warning\"]{{color:{COLORS['warning']};}}"
                         f"QToolButton[runtimeState=\"failed\"]{{color:{COLORS['error']};}}"
                         f"QToolButton[runtimeState=\"idle\"],QToolButton[runtimeState=\"cancelled\"]{{color:{COLORS['idle']};}}")
        for capsule in (self.sns3_status, self.hypatia_toolbar_status, self.ai_toolbar_status):
            capsule.setStyleSheet(capsule_style)
        self.sns3_status.clicked.connect(lambda: self._open_runtime_status('SNS3'))
        self.hypatia_toolbar_status.clicked.connect(lambda: self._open_runtime_status('HYPATIA'))
        self.ai_toolbar_status.clicked.connect(lambda: self._open_runtime_status('AI'))
        # Runtime controls live below the AI composer, not amid global actions.
        self.btn_sat_run = QPushButton('▶ Run'); self.btn_sat_run.setToolTip('Preview and confirm a validated official SNS-3 experiment.')
        self.btn_sat_run.clicked.connect(self._run_current_experiment)
        self.btn_sat_stop = QPushButton('■ Stop'); self.btn_sat_stop.setEnabled(False)
        self.btn_sat_results = QPushButton('Results'); self.btn_sat_results.clicked.connect(lambda: self.navigation.setCurrentRow(1))
        self.btn_sat_settings = QPushButton('Settings'); self.btn_sat_settings.clicked.connect(lambda: self.configure_agent(self.provider_selector.currentText()))
        for widget in (self.btn_sat_run, self.btn_sat_stop, self.btn_sat_results, self.btn_sat_settings): toolbar.addWidget(widget)

    def on_role_selector_changed(self, index: int) -> None:
        self._link_mode = False
        self._link_source = None
        if index == 0:
            self._placement_mode = None
            self._clear_placement_preview()
            self.canvas.setCursor(Qt.CursorShape.ArrowCursor)
            self.statusBar().showMessage('節點元件庫未選取；可直接拖曳畫布中的既有節點。')
        else:
            role = self.role_selector.itemText(index)
            if role == 'satellite':
                self.role_selector.blockSignals(True)
                self.role_selector.setCurrentIndex(0)
                self.role_selector.blockSignals(False)
                self.beginSatelliteCreation()
                return
            self._placement_mode = role
            self._update_placement_preview()
            self.canvas.setCursor(Qt.CursorShape.CrossCursor)

    def _toolbar_scenario_changed(self, _index: int) -> None:
        scenario_id = self.satellite_scenario_selector.currentData()
        if scenario_id and scenario_id != self.satellite_store.selectedScenarioId:
            self.satellite_store.set_scenario(str(scenario_id))

    def _toolbar_experiment_changed(self, _index: int) -> None:
        value = self.satellite_experiment_selector.currentData()
        if value: self.satellite_store.experimentType = str(value); self.satellite_store.changed.emit()

    def _update_placement_preview(self) -> None:
        if self._placement_mode is None:
            self._clear_placement_preview()
            return
        # Generate a temporary name for preview
        base_name = {'satellite': 'sat', 'ground_station': 'ue', 'gateway': 'gw', 'host': 'srv'}.get(self._placement_mode, 'node')
        # Find next available number
        used_names = set()
        for draft in self.lab.drafts.values():
            used_names.add(draft['name'])
        for node in self.lab.nodes.values():
            used_names.add(node['spec']['name'])
        i = 1
        while f"{base_name}{i}" in used_names:
            i += 1
        preview_name = f"{base_name}{i}"
        if self._placement_temp_item is None:
            self._placement_temp_item = NodeItem('-preview', preview_name, self._placement_mode)
            self._placement_temp_item.setBrush(QBrush(QColor(COLORS['surface'])))
            self._placement_temp_item.setPen(QPen(QColor(COLORS['border']), 1, Qt.DashLine))
            self._placement_temp_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self._placement_temp_item.setOpacity(0.6)
            self.scene.addItem(self._placement_temp_item)
        else:
            self._placement_temp_item.name = preview_name
            self._placement_temp_item.set_role_visual(self._placement_mode)
            self._placement_temp_item.text_item.setPlainText(self._placement_temp_item._format_text())
            self._placement_temp_item._position_label()
        self._placement_temp_item.setPos(0, 0)  # Will be updated on mouse move

    def _clear_placement_preview(self) -> None:
        if self._placement_temp_item is not None:
            self.scene.removeItem(self._placement_temp_item)
            self._placement_temp_item = None

    def _workspace(self) -> None:
        self.pages = QStackedWidget()
        topology = QWidget()
        layout = QVBoxLayout(topology)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.addWidget(label('網路拓樸 / 換手工作區'))
        layout.addWidget(label('尚無節點；點擊角色選擇器後在畫布上點擊以放置節點。', muted=True))
        self.scene = QGraphicsScene(self)
        self.canvas = QGraphicsView(self.scene)
        self.canvas.setObjectName('topology-canvas')
        self.canvas.setAccessibleName('空白網路拓樸畫布')
        self.canvas.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Some Linux raster backends do not invalidate every old item tile while
        # dragging custom-painted graphics. Repaint the viewport as one frame so
        # old node positions cannot remain as trails.
        self.canvas.setViewportUpdateMode(
            QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.canvas.setBackgroundBrush(QBrush(QColor(COLORS['background'])))
        # Enable mouse tracking for placement preview
        self.canvas.setMouseTracking(True)
        self.canvas.viewport().installEventFilter(self)
        self.scene.selectionChanged.connect(self._update_selection_actions)
        layout.addWidget(self.canvas, 1)
        layout.addWidget(label('模擬時間：未啟動　｜　實際量測：無數據　｜　鏈路／路由：未建立', muted=True))
        self.pages.addWidget(topology)
        satellite = QWidget()
        satellite_layout = QVBoxLayout(satellite)
        satellite_layout.setContentsMargins(16, 16, 16, 16)
        satellite_layout.addWidget(label('Selected Beam Coverage Editor'))
        satellite_layout.addWidget(label(
            'Place UEs in the selected Beam. Energy zones are placement guidance; SNS-3 provides real gain and link results after a run.', muted=True))
        self.satellite_canvas = SatelliteExperimentCanvas(self.satellite_store)
        self.satellite_canvas.satelliteClicked.connect(self.open_satellite_scenario_popover)
        self.satellite_canvas.setAccessibleName('Satellite experiment canvas; select Satellite System to configure a scenario')
        satellite_layout.addWidget(self.satellite_canvas, 1)
        self.pages.addWidget(satellite)
        self.pages.addWidget(panel('安全驗證', '平台尚未接入。未執行政策、身分、mTLS、配置／原始碼／機密掃描或合規驗證；沒有安全結論。'))
        results = QWidget()
        results_layout = QVBoxLayout(results)
        results_layout.setContentsMargins(16, 16, 16, 16)
        results_layout.addWidget(label('Satellite Results / Evidence'))
        results_layout.addWidget(label('Only parsed SNS-3 evidence is displayed. Empty cards mean Data unavailable.', muted=True))
        self.sns3_results = QTextBrowser()
        self.sns3_results.setOpenLinks(False)
        self.sns3_results.setPlainText('Data unavailable\nRun an official SNS-3 experiment from the toolbar.')
        results_layout.addWidget(self.sns3_results, 1)
        self.pages.addWidget(results)
        self.setCentralWidget(self.pages)

    def eventFilter(self, obj, event) -> bool:
        if obj is self.canvas.viewport():
            if event.type() == QEvent.Type.MouseMove:
                if self._placement_mode is not None and self._placement_temp_item is not None:
                    # Map view coordinates to scene coordinates
                    pos = self.canvas.mapToScene(event.position().toPoint())
                    self._placement_temp_item.setPos(pos)
                    return True
            elif event.type() == QEvent.Type.MouseButtonPress:
                if event.button() == Qt.LeftButton and self._link_mode:
                    item = self.canvas.itemAt(event.position().toPoint())
                    while item is not None and not isinstance(item, NodeItem):
                        item = item.parentItem()
                    if isinstance(item, NodeItem) and item.node_id != '-preview':
                        if self._link_source is None:
                            self._link_source = item
                            self.statusBar().showMessage(f'來源 {item.name}：請點擊目標節點')
                        elif item is not self._link_source:
                            self._create_link(self._link_source, item)
                            self._link_mode = False
                            self._link_source = None
                    return True
                if event.button() == Qt.LeftButton and self._placement_mode is not None:
                    # Left-click to place node
                    scene_pos = self.canvas.mapToScene(event.position().toPoint())
                    self._place_node_at(scene_pos)
                    return True
        return super().eventFilter(obj, event)

    def _place_node_at(self, scene_pos: QPointF) -> None:
        if self._placement_mode is None:
            return
        base_name = {'satellite': 'sat', 'ground_station': 'ue', 'gateway': 'gw', 'host': 'srv'}.get(self._placement_mode, 'node')
        used_names = set()
        for draft in self.lab.drafts.values():
            used_names.add(draft['name'])
        for node in self.lab.nodes.values():
            used_names.add(node['spec']['name'])
        i = 1
        while f"{base_name}{i}" in used_names:
            i += 1
        name = f"{base_name}{i}"
        used_ips = len(self.lab.drafts) + len(self.lab.nodes) + 1
        data = {
            'name': name,
            'role': self._placement_mode,
            'ip': f'10.80.0.{used_ips}/32',
            'image': ROLE_IMAGE_MAP[self._placement_mode],
        }
        res = self.lab.call('draft_node', data)
        if res['status'] == 'draft':
            node_data = res['data']['node']
            node_id = res['data']['draft_id']
            item = NodeItem(node_id, node_data['name'], node_data['role'])
            item.setPos(scene_pos)
            self.scene.addItem(item)
            self.statusBar().showMessage(f"已新增草稿節點 {node_data['name']}")
            # Reset placement mode after placing a node
            self._placement_mode = None
            self.role_selector.setCurrentIndex(0)
            self._clear_placement_preview()
            self.canvas.setCursor(Qt.CursorShape.ArrowCursor)
            self.statusBar().showMessage(f'已新增 {node_data["name"]}；未選取元件時仍可拖曳既有節點。')
            self._update_selection_actions()
        else:
            QMessageBox.critical(self, '錯誤', res.get('error', '未知錯誤'))

    def _dock(self, title: str, name: str, content: QWidget,
              area: Qt.DockWidgetArea) -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(name)
        dock.setWidget(content)
        self.addDockWidget(area, dock)
        self.view_menu.addAction(dock.toggleViewAction())
        return dock

    def _navigation(self) -> None:
        content = QWidget()
        layout = QVBoxLayout(content)
        self.navigation = QListWidget()
        self.navigation.setAccessibleName('工作區導覽')
        self.navigation.addItems(['Satellite Experiment', 'Results', 'Evidence'])
        self.navigation.setCurrentRow(0)
        self.navigation.currentRowChanged.connect(self._navigate_satellite)
        layout.addWidget(self.navigation)
        layout.addWidget(label('節點元件庫', muted=True))
        for title, role in (
            ('🛰 Satellite System', 'satellite'),
            ('● UE / UT', 'ground_station'),
            ('▣ Application Server', 'host'),
            ('→ Traffic Flow', 'traffic_flow'),
        ):
            button = QPushButton(title)
            button.setProperty('nodeRole', role)
            if role == 'satellite':
                button.clicked.connect(self.beginSatelliteCreation)
            elif role == 'ground_station':
                self.ue_button = button; button.clicked.connect(self.begin_ue_placement)
            elif role == 'traffic_flow':
                button.clicked.connect(lambda: self.append_ai_text('Select a UE then an Application Server to create an application traffic flow.'))
            else:
                button.clicked.connect(lambda _checked=False, value=role: self.set_placement_role(value))
            layout.addWidget(button)
        self.navigation_dock = self._dock('實驗專案', 'navigation-dock', content, Qt.DockWidgetArea.LeftDockWidgetArea)

    def _navigate_satellite(self, row: int) -> None:
        # Existing pages are retained, but the product navigation no longer
        # exposes the old topology/security-first workflow.
        self.pages.setCurrentIndex({0: 1, 1: 3, 2: 3}.get(row, 1))

    def _inspectors(self) -> None:
        # One explicit vertical splitter owns the right sidebar. Two sibling
        # QDockWidgets previously let the native dock layout collapse their
        # child wrappers instead of resizing the panels themselves.
        self.right_sidebar_splitter = QSplitter(Qt.Orientation.Vertical)
        self.right_sidebar_splitter.setObjectName('right-sidebar-splitter')
        self.right_sidebar_splitter.setChildrenCollapsible(False)
        self.hypatia_panel = HypatiaMapPanel(self.lab)
        self.beam_panel = BeamCoveragePanel(self.satellite_store)
        self.right_sidebar_splitter.addWidget(self._right_panel('Hypatia', self.hypatia_panel))
        self.beam_wrapper = self._right_panel('Beam Overview', self.beam_panel)
        self.right_sidebar_splitter.addWidget(self.beam_wrapper)
        self.right_sidebar_splitter.setStretchFactor(0, 1)
        self.right_sidebar_splitter.setStretchFactor(1, 1)
        self.rightSidebarSplitRatio = 0.5
        self.right_sidebar_splitter.splitterMoved.connect(self._right_sidebar_resized)
        self.map_dock = self._dock('Hypatia / Beam Overview', 'map-dock',
            self.right_sidebar_splitter, Qt.DockWidgetArea.RightDockWidgetArea)
        self.map_dock.setMinimumWidth(320)
        # Compatibility alias for callers that referenced the old second dock.
        self.inspector_dock = self.map_dock
        self.beam_float_dock: Optional[QDockWidget] = None
        self.beam_panel_state = 'DOCKED'
        QTimer.singleShot(0, self._reset_right_sidebar_split)

    def _reset_right_sidebar_split(self) -> None:
        if not hasattr(self, 'right_sidebar_splitter'):
            return
        total = max(1, self.right_sidebar_splitter.height())
        self.right_sidebar_splitter.setSizes([
            max(220, int(total * self.rightSidebarSplitRatio)),
            max(240, int(total * (1 - self.rightSidebarSplitRatio))),
        ])

    def _right_sidebar_resized(self, _position: int, _index: int) -> None:
        sizes = self.right_sidebar_splitter.sizes()
        total = sum(sizes)
        if total:
            self.rightSidebarSplitRatio = sizes[0] / total

    def _toggle_beam_float(self) -> None:
        """Detach Beam Overview without losing its last dock ratio."""
        if self.beam_panel_state == 'FLOATING':
            self._dock_beam_overview()
            return
        sizes = self.right_sidebar_splitter.sizes()
        total = sum(sizes)
        if total:
            self.rightSidebarSplitRatio = sizes[0] / total
        self.beam_wrapper.setParent(None)
        self.beam_float_dock = QDockWidget('Beam Overview', self)
        self.beam_float_dock.setObjectName('beam-overview-floating-dock')
        self.beam_float_dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        self.beam_float_dock.setWidget(self.beam_wrapper)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.beam_float_dock)
        self.beam_float_dock.setFloating(True)
        self.beam_float_dock.resize(420, 520)
        self.beam_float_dock.show()
        self.beam_panel_state = 'FLOATING'

    def _dock_beam_overview(self) -> None:
        if self.beam_float_dock is not None:
            self.beam_float_dock.setWidget(None)
            self.beam_float_dock.hide()
            self.beam_float_dock.deleteLater()
            self.beam_float_dock = None
        self.right_sidebar_splitter.insertWidget(1, self.beam_wrapper)
        self.beam_wrapper.show()
        self.beam_panel_state = 'DOCKED'
        QTimer.singleShot(50, self._reset_right_sidebar_split)

    def _right_panel(self, title: str, content: QWidget) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(16, 16, 16, 16)
        header = QHBoxLayout()
        header.addWidget(label(title), 1)
        if title == 'Beam Overview':
            undock = QToolButton(); undock.setText('↗'); undock.setToolTip('Dock / Undock Beam Overview')
            undock.clicked.connect(self._toggle_beam_float)
            maximize = QToolButton(); maximize.setText('□'); maximize.setToolTip('Maximize Beam Overview')
            maximize.clicked.connect(self._maximize_beam_overview)
            collapse = QToolButton(); collapse.setText('×'); collapse.setToolTip('Collapse Beam Overview')
            collapse.clicked.connect(self._collapse_beam_overview)
            header.addWidget(undock); header.addWidget(maximize); header.addWidget(collapse)
            self.beam_dock_controls = (undock, maximize, collapse)
        layout.addLayout(header)
        layout.addWidget(content, 1)
        widget.setMinimumHeight(0)
        widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        return widget

    def _maximize_beam_overview(self) -> None:
        if self.beam_panel_state == 'MAXIMIZED':
            self.beam_panel_state = 'DOCKED'
            self.right_sidebar_splitter.setCollapsible(0, False)
            self._reset_right_sidebar_split()
            return
        if self.beam_panel_state == 'FLOATING' and self.beam_float_dock is not None:
            self.beam_float_dock.setWindowState(Qt.WindowState.WindowMaximized)
            self.beam_panel_state = 'MAXIMIZED'
            return
        self.beam_panel_state = 'MAXIMIZED'
        self.right_sidebar_splitter.setCollapsible(0, True)
        self.right_sidebar_splitter.setSizes([0, max(240, self.right_sidebar_splitter.height())])

    def _collapse_beam_overview(self) -> None:
        if self.beam_panel_state == 'FLOATING' and self.beam_float_dock is not None:
            self.beam_float_dock.hide()
            return
        self.beam_wrapper.setVisible(False)
        self.right_sidebar_splitter.setSizes([max(220, self.right_sidebar_splitter.height()), 0])

    def _load_satellite_scenarios(self) -> None:
        result = self.lab.call('sns3_scenarios', {})
        scenarios = result.get('data', {}).get('scenarios', [])
        self.satellite_store.load_scenarios(scenarios)
        self.satellite_scenario_selector.blockSignals(True)
        self.satellite_scenario_selector.clear()
        for scenario in scenarios:
            self.satellite_scenario_selector.addItem(scenario['name'], scenario['id'])
        self.satellite_scenario_selector.setCurrentIndex(
            self.satellite_scenario_selector.findData(self.satellite_store.selectedScenarioId))
        self.satellite_scenario_selector.blockSignals(False)
        if not scenarios:
            self.statusBar().showMessage(result.get('error', 'SNS-3 scenario data unavailable'))

    def _sync_satellite_controls(self) -> None:
        if hasattr(self, 'ue_button'):
            ready = self.satellite_store.satellitePlaced and self.satellite_store.selectedBeamId is not None
            self.ue_button.setEnabled(ready or self.satellite_store.uePlacementMode)
            if self.satellite_store.uePlacementMode:
                self.ue_button.setText('● UE / UT · Cancel')
                self.ue_button.setToolTip('UE placement mode is active. Click again to cancel placement mode.')
                self.satellite_canvas.setCursor(Qt.CursorShape.CrossCursor)
            else:
                self.ue_button.setText('● UE / UT')
                self.ue_button.setToolTip('Place a UE in the selected Beam Coverage.' if ready else '請先建立 Satellite System 並選擇 Beam。')
                self.satellite_canvas.setCursor(Qt.CursorShape.ArrowCursor)

    def open_satellite_scenario_popover(self) -> None:
        if not self.satellite_store.scenarios:
            self._load_satellite_scenarios()
        if self.satellite_store.satellitePlaced:
            dialog = SatelliteScenarioPopover(self, self.satellite_store)
            dialog.setWindowTitle('Satellite System Inspector')
            dialog.exec()
            return
        self.beginSatelliteCreation()

    def beginSatelliteCreation(self) -> None:
        """Special SNS-3 scenario flow, never the generic NodeItem workflow."""
        self.navigation.setCurrentRow(0)
        if not self.satellite_store.scenarios: self._load_satellite_scenarios()
        dialog = SatelliteScenarioDialog(self, self.satellite_store)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.statusBar().showMessage('請在 Satellite Experiment Canvas 點擊以放置衛星系統。')

    def begin_ue_placement(self) -> None:
        if self.satellite_store.uePlacementMode:
            self.satellite_store.uePlacementMode = False
            self.satellite_store.changed.emit()
            self.statusBar().showMessage('UE placement mode cancelled.')
            return
        if not self.satellite_store.enter_ue_placement():
            self.ue_button.setEnabled(False)
            self.ue_button.setToolTip('請先建立 Satellite System 並選擇 Beam。')
            self.statusBar().showMessage('請先建立 Satellite System 並選擇 Beam。')
            return
        self.ue_button.setText('● Click Coverage to Place UE')
        self.satellite_canvas.setCursor(Qt.CursorShape.CrossCursor)
        self.statusBar().showMessage('點擊 Beam Coverage 內的位置放置 UE。')

    def _console(self) -> None:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 8, 12, 8); layout.setSpacing(8)
        self.ai_header = QWidget(); header = QHBoxLayout(self.ai_header); header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(label('✦ AI 助手'))
        self.btn_ai_collapse = QPushButton('⌄'); self.btn_ai_collapse.setToolTip('Collapse AI Assistant'); self.btn_ai_collapse.clicked.connect(self.toggle_ai_dock)
        header.addStretch(); header.addWidget(self.btn_ai_collapse); layout.addWidget(self.ai_header)
        self.console_tabs = QTabWidget()
        self.console_tabs.setAccessibleName('AI Assistant tabs')
        self._result_details: dict[str, str] = {}  # Immutable JSON snapshots.
        for index, (title, message) in enumerate((
            ('對話', '✦ AI 實驗助理\n\n你可以輸入：\n「start」\n「把每個 Beam 改成 5 個 UE」\n「目前 throughput 是多少？」\n「分析台北到東京的 Starlink 路徑」'),
            ('_terminal_internal', 'Terminal output is available only from a Containernet-enabled UE context menu.'),
            ('執行紀錄', '尚未執行 SNS-3 或 Hypatia 實驗。'),
            ('Evidence', '尚無 Evidence。執行後會顯示 Evidence ID、Metric、Value 與 Source。'),
            ('Decisions', '尚未建立 LLM 決策紀錄。觀察、Finding、Proposal 與 Outcome 將顯示在此。'),
        )):
            output = QTextBrowser() if index == 0 else QTextEdit()
            output.setReadOnly(True)
            if isinstance(output, QTextBrowser):
                output.setOpenLinks(False)
                output.anchorClicked.connect(self._show_result_details)
            output.setAccessibleName(title)
            output.setPlainText(message)
            self.console_tabs.addTab(output, title)
        self.console_tabs.tabBar().setTabVisible(1, False)
        layout.addWidget(self.console_tabs)
        entry_row = QHBoxLayout()
        self.ai_compact_title = label('✦ AI 助手')
        entry_row.addWidget(self.ai_compact_title)
        self.console_input = QLineEdit()
        self.console_input.setAccessibleName('對話與配置輸入')
        self.console_input.setPlaceholderText('輸入 start、調整實驗參數，或詢問目前結果…')
        self.console_input.returnPressed.connect(self.on_console_submit)
        entry_row.addWidget(self.console_input)
        self.btn_console_submit = QPushButton('送出')
        self.btn_console_submit.clicked.connect(self.on_console_submit)
        entry_row.addWidget(self.btn_console_submit)
        self.btn_ai_expand = QPushButton('⌃'); self.btn_ai_expand.setToolTip('Expand AI Assistant'); self.btn_ai_expand.clicked.connect(self.toggle_ai_dock)
        entry_row.addWidget(self.btn_ai_expand)
        layout.addLayout(entry_row)
        # A single explicit vertical splitter gives the workspace and AI dock
        # one owner for their resize handle. The previous bottom QDockWidget
        # introduced a second native splitter and could clip the dock body.
        pages = self.takeCentralWidget()
        self.main_area_splitter = QSplitter(Qt.Orientation.Vertical)
        self.main_area_splitter.setObjectName('main-area-splitter')
        self.main_area_splitter.setChildrenCollapsible(False)
        self.main_area_splitter.addWidget(pages)
        self.main_area_splitter.addWidget(content)
        self.main_area_splitter.setStretchFactor(0, 1)
        self.main_area_splitter.setStretchFactor(1, 0)
        self.setCentralWidget(self.main_area_splitter)
        self.console_dock = content
        self.console_dock.setObjectName('ai-dock')
        self.console_dock.setMinimumHeight(56)
        self.ai_dock_expanded = True

    def toggle_ai_dock(self) -> None:
        self.set_ai_dock_expanded(not self.ai_dock_expanded)

    def set_ai_dock_expanded(self, expanded: bool) -> None:
        """Two real layouts, never a clipped hidden-expanded hybrid."""
        self.ai_dock_expanded = expanded
        self.ai_header.setVisible(expanded)
        self.console_tabs.setVisible(expanded)
        self.ai_compact_title.setVisible(not expanded)
        self.btn_ai_expand.setVisible(not expanded)
        self.btn_ai_collapse.setText('⌄' if expanded else '⌃')
        self.btn_ai_collapse.setToolTip('Collapse AI Assistant' if expanded else 'Expand AI Assistant')
        if not hasattr(self, 'main_area_splitter'):
            return
        total = max(1, self.main_area_splitter.height())
        if expanded:
            self.console_dock.setMinimumHeight(240)
            self.console_dock.setMaximumHeight(min(420, max(240, int(total * 0.45))))
            dock_height = min(360, max(260, int(total * 0.32)))
            self.main_area_splitter.setSizes([max(0, total - dock_height), dock_height])
        else:
            self.console_dock.setMinimumHeight(56)
            self.console_dock.setMaximumHeight(64)
            self.main_area_splitter.setSizes([max(0, total - 60), 60])

    def append_ai_text(self, text: str) -> None:
        editor = self.console_tabs.widget(0)
        if isinstance(editor, QTextEdit):
            cursor = editor.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            if not editor.document().isEmpty():
                cursor.insertBlock()
            cursor.insertText(text)
            editor.setTextCursor(cursor)

    def append_ai_result(self, summary: str, details: list[str],
                         detail_links: list[dict[str, Any]]) -> None:
        editor = self.console_tabs.widget(0)
        if not isinstance(editor, QTextBrowser):
            self.append_ai_text(summary)
            return
        fragments = [html.escape(summary).replace('\n', '<br>')]
        fragments.extend('<br>' + html.escape(str(line)) for line in details)
        for link in detail_links:
            if link.get('kind') not in {'interface', 'ping', 'operation', 'sns3_metric',
                                        'sns3_artifacts', 'sns3_evidence',
                                        'experiment_action'}:
                continue
            detail_id = uuid.uuid4().hex
            self._result_details[detail_id] = json.dumps(link, ensure_ascii=False)
            fragments.append(f'<br><a href="detail:{detail_id}" '
                             f'style="color:{COLORS["accent"]};text-decoration:none;">'
                             f'▸ {html.escape(link.get("label", "觀測資料"))}</a>')
        # Each append is parsed independently by Qt; insert complete HTML once.
        editor.append('<p style="margin:8px 0;">' + ''.join(fragments) + '</p>')

    def refresh_decisions_view(self) -> None:
        """Render backend decision facts without exposing any model reasoning."""
        output = self.console_tabs.widget(4)
        if not isinstance(output, QTextEdit):
            return
        result = self.lab.call('list_decisions', {})
        decisions = result.get('data', {}).get('decisions', [])
        if not decisions:
            output.setPlainText('尚未建立 LLM 決策紀錄。')
            return
        rows: list[str] = []
        for decision in decisions:
            proposal = decision.get('proposal', {})
            rows.extend((
                f"{decision.get('timestamp', '')} · {str(decision.get('status', '')).upper()}",
                f"目標：{decision.get('user_goal', '')}",
                f"Finding：{', '.join(decision.get('findings', [])) or '—'}",
                f"Proposal：{proposal.get('category', '—')} · {len(proposal.get('changes', []))} 項變更",
                '',
            ))
        output.setPlainText('\n'.join(rows).rstrip())

    def _show_result_details(self, url: QUrl) -> None:
        if url.scheme() != 'detail':
            return
        snapshot = self._result_details.get(url.path())
        if snapshot is None:
            return
        detail = json.loads(snapshot)
        if detail.get('kind') == 'experiment_action' and detail.get('action') == 'random-access':
            index = self.satellite_experiment_selector.findData('random_access')
            if index >= 0:
                self.satellite_experiment_selector.setCurrentIndex(index)
            self._start_satellite_wizard('start')
            return
        self._result_dialog(detail).exec()

    def _result_dialog(self, detail: dict[str, Any]) -> QDialog:
        if detail.get('kind') == 'sns3_metric' and detail.get('metric_detail'):
            return self._metric_result_dialog(detail)
        dialog = QDialog(self)
        dialog.setWindowTitle(detail.get('label', '觀測資料'))
        dialog.setAccessibleName('查詢詳細內容視窗')
        dialog.setStyleSheet(f'''
            QDialog, QLabel {{ background:{COLORS['surface']}; color:{COLORS['text']}; }}
            QTextEdit {{ background:{COLORS['background']}; color:{COLORS['text']};
                border:1px solid {COLORS['border']}; padding:12px; }}
            QPushButton {{ background:{COLORS['raised']}; color:{COLORS['accent']};
                border:1px solid {COLORS['border']}; padding:8px 16px; }}
        ''')
        layout = QVBoxLayout(dialog)
        title = QLabel(detail.get('label', '觀測資料'))
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setFont(QFont('sans-serif', 15, QFont.Weight.Bold))
        layout.addWidget(title)
        layout.addWidget(QLabel('觀測快照 · 不隨後續查詢覆寫'))
        properties = QFormLayout()
        for key, value in detail.get('properties', {}).items():
            field = QLabel(str(value))
            field.setTextFormat(Qt.TextFormat.PlainText)
            field.setWordWrap(True)
            field.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            properties.addRow(str(key), field)
        layout.addLayout(properties)
        layout.addWidget(QLabel('原始輸出 · 後端回傳內容'))
        content = QTextEdit()
        content.setReadOnly(True)
        content.setPlainText(detail.get('content') or '後端未回傳原始輸出')
        content.setFont(QFont('monospace'))
        layout.addWidget(content)
        close_button = QPushButton('關閉')
        close_button.clicked.connect(dialog.accept)
        layout.addWidget(close_button)
        dialog.resize(760, 560)
        return dialog

    def _metric_result_dialog(self, detail: dict[str, Any]) -> QDialog:
        data = detail['metric_detail']
        metric = data['metric']
        catalog = data.get('catalog', {})
        context = data.get('context', {})
        requested = context.get('requested_config', {})
        actual = context.get('actual_run_topology', {})
        dialog = QDialog(self)
        dialog.setWindowTitle(metric.get('name', 'Metric Detail'))
        dialog.setAccessibleName('SNS-3 metric explanation')
        dialog.setStyleSheet(f'''
            QDialog, QWidget, QLabel {{ background:{COLORS['surface']}; color:{COLORS['text']}; }}
            QTabWidget::pane {{ border:1px solid {COLORS['border']}; }}
            QTabBar::tab {{ padding:8px 14px; color:{COLORS['muted']}; }}
            QTabBar::tab:selected {{ color:{COLORS['accent']}; background:{COLORS['raised']}; }}
            QTextEdit {{ background:{COLORS['background']}; color:{COLORS['text']};
                border:1px solid {COLORS['border']}; padding:12px; }}
            QPushButton {{ min-height:40px; background:{COLORS['raised']}; color:{COLORS['text']};
                border:1px solid {COLORS['border']}; border-radius:4px; padding:6px 12px; }}
            QPushButton:focus {{ border:2px solid {COLORS['accent']}; }}
        ''')
        root = QVBoxLayout(dialog)
        title = QLabel(f"{metric['name']}  {metric['value']:.6g} {metric['unit']}")
        title.setFont(QFont('sans-serif', 16, QFont.Weight.Bold))
        root.addWidget(title)
        root.addWidget(QLabel(f"SNS-3 Evidence · {data.get('source_status', 'DIRECT')}"))
        tabs = QTabWidget()
        tabs.setAccessibleName('Metric detail layers')
        overview_scroll = QScrollArea()
        overview_scroll.setWidgetResizable(True)
        overview = QWidget()
        layout = QVBoxLayout(overview)
        layout.setSpacing(14)

        def section(heading: str, body: str) -> None:
            label_heading = QLabel(heading)
            label_heading.setFont(QFont('sans-serif', 12, QFont.Weight.Bold))
            label_body = QLabel(body)
            label_body.setWordWrap(True)
            label_body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(label_heading)
            layout.addWidget(label_body)

        section('A. What', catalog.get('description') or
                '此數值由本次 SNS-3 output 經受支援 parser 正規化。')
        topology = actual.get('ut_count_by_beam') or {}
        context_lines = [
            f"Scenario：{requested.get('scenario', actual.get('scenario', 'Data unavailable'))}",
            f"Profile：{requested.get('profile', actual.get('profile', 'Data unavailable'))}",
            f"Topology Source：{actual.get('topology_source', 'Data unavailable')}",
            f"Actual Beams：{', '.join(map(str, actual.get('active_beams', []))) or 'Data unavailable'}",
            f"Actual UT：{actual.get('total_ut', 'Data unavailable')}",
            f"UT Distribution：{', '.join(f'Beam {beam}: {count}' for beam, count in topology.items()) or 'Data unavailable'}",
            f"Traffic：{requested.get('traffic_rate_kbps', actual.get('traffic_rate_kbps', 'Data unavailable'))} kbps / flow",
            f"Simulation Time：{requested.get('simulation_time', actual.get('simulation_time', 'Data unavailable'))} s",
        ]
        section('B. Context', '\n'.join(context_lines))
        interpretation = data.get('interpretation', {})
        interpretation_lines = [
            'Evidence status：' + str(interpretation.get('evidence_status', 'Data unavailable')),
            'Causal inference：' + str(interpretation.get('causal_inference', 'NOT_ESTABLISHED')),
            *('• ' + item for item in interpretation.get('interpretation_hints', [])),
        ]
        section('C. Interpretation', '\n'.join(interpretation_lines))
        related = data.get('related_metrics', [])
        if related:
            related_title = QLabel('Related Metrics')
            related_title.setFont(QFont('sans-serif', 12, QFont.Weight.Bold))
            layout.addWidget(related_title)
            for related_metric in related:
                button = QPushButton(
                    f"{related_metric['name']}  {related_metric['value']:.6g} {related_metric['unit']}")
                button.setAccessibleName(f"Open {related_metric['name']} detail")
                button.clicked.connect(lambda _checked=False, metric_id=related_metric['metric_id']:
                    self._open_related_metric(data['run_id'], metric_id))
                layout.addWidget(button)
        comparison = data.get('comparison')
        if comparison:
            compare = QPushButton('與上一輪比較')
            compare.clicked.connect(lambda: QMessageBox.information(
                dialog, 'Evidence-backed comparison',
                f"Current：{comparison['current']:.6g} {comparison['unit']}\n"
                f"Previous：{comparison['previous']:.6g} {comparison['unit']}\n"
                f"Change：{comparison['change']:+.6g} {comparison['unit']}\n"
                f"Previous Run：{comparison['previous_run_id']}"))
            layout.addWidget(compare)
        layout.addStretch()
        overview_scroll.setWidget(overview)
        tabs.addTab(overview_scroll, 'Explanation')

        advanced = QTextEdit()
        advanced.setReadOnly(True)
        advanced.setAccessibleName('Advanced evidence and raw JSON')
        advanced.setPlainText(
            'D. Advanced Evidence\n\n'
            f"Mean：{metric.get('value')} {metric.get('unit')}\n"
            f"Min：{metric.get('min')}\nMax：{metric.get('max')}\n"
            f"Median：{metric.get('median')}\nP95：{metric.get('p95')}\n"
            f"Samples：{metric.get('count')}\n\n"
            'Evidence IDs\n' + '\n'.join(metric.get('evidence_ids', [])) + '\n\n'
            'Source Artifacts\n' + '\n'.join(
                f"{item.get('filename')} · {item.get('parser')} · {item.get('path')}"
                for item in data.get('artifacts', [])) + '\n\n'
            'Raw Evidence JSON\n' + json.dumps(data.get('evidence', []), ensure_ascii=False, indent=2))
        tabs.addTab(advanced, 'Advanced Evidence')
        root.addWidget(tabs)
        close_button = QPushButton('關閉')
        close_button.clicked.connect(dialog.accept)
        root.addWidget(close_button)
        dialog.resize(800, 650)
        return dialog

    def _open_related_metric(self, run_id: str, metric_id: str) -> None:
        result = self.lab.call('get_metric_details', {'run_id': run_id, 'metric_id': metric_id})
        if result.get('status') != 'ok':
            QMessageBox.warning(self, 'Metric unavailable', result.get('error', 'Data unavailable'))
            return
        self._metric_result_dialog({'kind': 'sns3_metric',
                                    'metric_detail': result['data']}).exec()

    def configure_agent(self, provider: str) -> None:
        try:
            settings = ProviderSettings.from_environment(provider)
            client = UnifiedLLMClient(settings)
            self.agent = LeoToolCallAgent(client, self.tool_registry)
            self.provider_status.setText(f'{settings.provider} · {settings.model}')
        except LLMConfigError as exc:
            self.agent = None
            self.provider_status.setText(f'{provider} · {exc}')

    def open_sns3_experiment(self) -> None:
        self.navigation.setCurrentRow(0)
        self.open_satellite_scenario_popover()

    def _refresh_runtime_capsules(self) -> None:
        # Refresh active Hypatia processes from the real Popen state before
        # reading the shared store. This is a read-only status check.
        for job_id, job in list(self.lab.hypatia_visualization.jobs.items()):
            if job.get('status') not in {'COMPLETED', 'FAILED', 'CANCELLED'}:
                try: self.lab.hypatia_visualization.status(job_id)
                except (KeyError, OSError): pass
        mapping = {
            'SNS3': self.sns3_status, 'HYPATIA': self.hypatia_toolbar_status,
            'AI': self.ai_toolbar_status,
        }
        for system, capsule in mapping.items():
            row = self.hypatia_panel.runtime_status_record() if system == 'HYPATIA' else self.lab.runtime_jobs.latest(system)
            if row is None:
                capsule.set_status('IDLE' if system == 'AI' else 'READY', 'Idle' if system == 'AI' else 'Ready')
            else:
                status = str(row.get('status', 'READY'))
                stage = str(row.get('stage') or 'Ready')
                if system == 'HYPATIA' and status in {'GENERATING', 'RUNNING', 'QUEUED'}:
                    generated, total = row.get('generated_states'), row.get('total_states')
                    if isinstance(generated, int) and isinstance(total, int) and total > 0:
                        capsule.set_status(status, f'{round(generated / total * 100)}%')
                        elapsed = float(row.get('elapsed_sec') or 0)
                        capsule.setToolTip(f'{stage} · {generated} / {total} states · {int(elapsed // 60):02d}:{int(elapsed % 60):02d}')
                        continue
                capsule.set_status(status, stage)

    def _open_runtime_status(self, system: str) -> None:
        job = self.hypatia_panel.runtime_status_record() if system == 'HYPATIA' else self.lab.runtime_jobs.latest(system)
        dialog = RuntimeStatusDialog(self, 'SNS-3' if system == 'SNS3' else system.title(),
                                     job)
        dialog.exec()

    def _run_worker(self, kind: str, operation: Any) -> None:
        if self._worker_kind:
            self.append_ai_text('⚠ 目前已有操作執行中。')
            return
        self._worker_kind = kind
        runtime_system = 'SNS3' if kind == 'sns3-run' else 'AI'
        runtime_stage = 'RUNNING_SIMULATION' if kind == 'sns3-run' else (
            'CONFIGURING' if kind == 'agent' else 'RUNNING')
        record = self.lab.runtime_jobs.create(system=runtime_system, job_type=kind,
            status='RUNNING', stage=runtime_stage,
            message='Waiting for assistant configuration.' if kind == 'agent' else 'Background operation is running.')
        self._runtime_worker_job_id = record['job_id']
        self._update_selection_actions()
        self.console_input.setEnabled(False)
        self.btn_console_submit.setEnabled(False)

        def run() -> None:
            try:
                result = operation()
            except Exception as exc:
                result = {'status': 'error', 'error': str(exc)}
            self.worker_signals.completed.emit(result)

        threading.Thread(target=run, daemon=True).start()

    def _on_worker_completed(self, result: dict[str, Any]) -> None:
        kind = self._worker_kind
        runtime_job_id = getattr(self, '_runtime_worker_job_id', None)
        if runtime_job_id:
            failed = result.get('status') == 'error'
            stage = ('FAILED' if failed else 'WAITING_FOR_CONFIRMATION'
                     if kind == 'agent' and any(call.get('name') == 'start_hypatia_wizard'
                                                for call in result.get('tool_calls', []))
                     else 'COMPLETED')
            self.lab.runtime_jobs.update(runtime_job_id,
                status='FAILED' if failed else ('CONFIGURING' if stage == 'WAITING_FOR_CONFIRMATION' else 'COMPLETED'), stage=stage,
                error=result.get('error') if failed else None,
                message=('Wizard is waiting for user input.' if stage == 'WAITING_FOR_CONFIRMATION'
                         else 'Operation completed.' if not failed else 'Operation failed.'))
        self._runtime_worker_job_id = None
        self._worker_kind = ''
        self.console_input.setEnabled(True)
        self.btn_console_submit.setEnabled(True)
        if kind == 'agent':
            self._render_agent_result(result)
        elif kind.startswith('command:'):
            self._render_command_result(kind.split(':', 1)[1], result)
        elif kind == 'runtime-operation':
            outcomes = result.get('results', [])
            ok = result.get('status') == 'ok'
            summary = ('✓ 設定已套用並完成驗證。' if ok
                       else '✖ 設定未完整套用；請展開詳細資料。')
            steps = [step for outcome in outcomes
                     for step in outcome.get('data', {}).get('steps', [])]
            labels = []
            for step in steps:
                command = step.get('command', [])
                if command[:3] == ['tc', 'qdisc', 'replace']:
                    labels.append('鏈路品質設定')
                elif command[:3] == ['tc', '-s', '-j']:
                    labels.append('套用後驗證')
                else:
                    labels.append('容器操作')
            if labels:
                summary += '\n' + '\n'.join(f'• {label}' for label in dict.fromkeys(labels))
            self.append_ai_result(summary, [], [{
                'kind': 'operation', 'label': '設定結果與驗證紀錄',
                'properties': {'狀態': '成功' if ok else '失敗',
                               '操作數': len(outcomes)},
                'content': json.dumps(result, ensure_ascii=False, indent=2),
            }])
            evidence = self.console_tabs.widget(3)
            if isinstance(evidence, QTextEdit):
                evidence.insertPlainText('\n' + json.dumps(result, ensure_ascii=False, indent=2))
        elif kind in {'flow-start', 'flow-stop'}:
            started = kind == 'flow-start'
            ok = result.get('status') in {'running', 'ok'}
            self.append_ai_result(
                ('✓ 實驗流量已啟動。' if started and ok else
                 '✓ 實驗流量已停止。' if ok else '✖ 實驗流量操作失敗。'), [], [{
                    'kind': 'flow', 'label': '流量操作結果',
                    'properties': {'操作': '啟動' if started else '停止',
                                   '狀態': result.get('status', 'error')},
                    'content': json.dumps(result, ensure_ascii=False, indent=2),
                }])
        elif kind == 'sns3-run':
            self._render_sns3_result(result)
        self._update_selection_actions()
        self.refresh_decisions_view()

    def _render_sns3_result(self, result: dict[str, Any]) -> None:
        self.sns3_status.set_status('READY', 'Ready')
        if result.get('status') != 'ok':
            data = result.get('data', {})
            error = result.get('error', 'Unknown SNS-3 error')
            output_path = str(data.get('output_path', 'unavailable'))
            exit_code = data.get('returncode', 'unavailable')
            self.sns3_results.setPlainText(
                'Simulation failed — Data unavailable\n\n' + error +
                '\n\nOutput: ' + output_path)
            self.append_ai_result(
                '✖ SNS-3 執行失敗，因此未顯示任何模擬數值或合成資料。', [], [{
                    'kind': 'operation', 'label': '查看 SNS-3 錯誤詳情 →',
                    'properties': {'Exit code': exit_code, 'Output': output_path},
                    'content': error,
                }])
            return
        data = result['data']
        metrics = data.get('metrics', [])
        summary = data.get('summary', {})
        topology = data.get('topology_configuration', {})
        lines = [f"✓ SNS-3 run: {data['run_id']}", f"Experiment: {data['experiment']}",
                 f"Output: {data['output_path']}", '']
        if topology:
            beam_set = topology.get('active_beam_set') or []
            distribution = topology.get('ut_count_by_beam') or {}
            lines.extend([
                'Topology source: ' + str(topology.get('source', 'SNS-3 configuration')),
                'Active Beams: ' + (', '.join(map(str, beam_set)) or 'Data unavailable'),
                f"Active Beam Count: {topology.get('active_beam_count', 'Data unavailable')}",
                ('UTs per Beam: ' + str(topology['uts_per_beam'])
                 if isinstance(topology.get('uts_per_beam'), int)
                 else 'UTs per Beam: non-uniform; see distribution'),
                ('UT distribution: ' + ', '.join(
                    f'Beam {beam}: {count}' for beam, count in distribution.items())
                 if distribution else 'UT distribution: Data unavailable'),
                f"Total UT Count: {topology.get('total_ut_count', 'Data unavailable')}", '',
            ])
            # Only configuration can update the editor preview. Gain and serving
            # satellite remain unavailable without a matching SNS-3 trace.
            if isinstance(topology.get('uts_per_beam'), int):
                self.satellite_store.set_uts_per_beam(topology['uts_per_beam'])
            if isinstance(topology.get('active_beam_set'), list):
                self.satellite_store.set_active_beams(topology['active_beam_set'])
        if summary.get('key_metrics'):
            lines.append('Parsed SNS-3 summary:')
            lines.extend(f"• {item['name']}: {item['value']:.6g} {item['unit']} · n={item['count']}"
                         for item in summary['key_metrics'])
        else:
            lines.append('Simulation completed, but no supported scalar metric was emitted (Data unavailable).')
        if summary.get('warnings'):
            lines.extend(['', f"Warnings: {len(summary['warnings'])}"])
        self.sns3_results.setPlainText('\n'.join(lines))
        links = []
        for item in summary.get('key_metrics', []):
            detail_result = self.lab.call('get_metric_details', {
                'run_id': data['run_id'], 'metric_id': item['metric_id']})
            metric_detail = detail_result.get('data', {}) if detail_result.get('status') == 'ok' else {}
            links.append({'kind': 'sns3_metric',
                'label': f"{item['name']} · {item['value']:.6g} {item['unit']} · 查看詳情 →",
                'properties': {'Run ID': data['run_id'], 'Metric': item['metric_id']},
                'metric_detail': metric_detail})
        detected = self.lab.call('list_detected_metrics', {'run_id': data['run_id']})
        links.append({'kind': 'sns3_artifacts', 'label': f"結果檔案 · {summary.get('artifact_count', 0)} 個 →",
                      'properties': {'Run ID': data['run_id'],
                                     'Parsed': summary.get('parsed_artifact_count', 0),
                                     'Total': summary.get('artifact_count', 0)},
                      'content': json.dumps(detected.get('data', {}), ensure_ascii=False, indent=2)})
        self.append_ai_result(
            f"實驗完成 ✓\n{summary.get('overview', {}).get('total_ut', 'Data unavailable')} UT · "
            f"{summary.get('overview', {}).get('simulation_time', 'Data unavailable')} s\n"
            '主要結果均來自 parser-backed Evidence。', [], links)
        evidence_view = self.console_tabs.widget(3)
        if isinstance(evidence_view, QTextEdit):
            rows = [f"{item['evidence_id']}  {item['metric']}  {item['value']} {item['unit']}  SNS-3"
                    for item in data.get('evidence', [])]
            evidence_view.setPlainText('\n'.join(rows) if rows else '尚無可解析 Evidence。')

    def _render_agent_result(self, result: dict[str, Any]) -> None:
        # A tool-driven assistant may change Hypatia state even when the user
        # did not touch the panel.  Apply that one canonical state here; this
        # does not load a page or start a job.
        hypatia_prompt = None
        for observation in reversed(result.get('results', [])):
            data = observation.get('data', {}) if isinstance(observation, dict) else {}
            candidate = data.get('wizard') if isinstance(data, dict) else None
            if isinstance(data, dict) and data.get('status') in {'prompt', 'confirmation'}:
                hypatia_prompt = data
            if not isinstance(candidate, dict) and isinstance(data, dict) and 'network' in data and 'gen_time_ms' in data:
                candidate = data
            if isinstance(candidate, dict):
                self.hypatia_panel.apply_hypatia_wizard_state(candidate)
                break
        if hypatia_prompt and any(call.get('name') == 'start_hypatia_wizard'
                                  for call in result.get('tool_calls', [])):
            self._render_hypatia_wizard(hypatia_prompt)
            return
        if result.get('status') == 'error':
            for observation in result.get('results', []):
                proposal = observation.get('data', {}).get('proposal')
                if proposal:
                    self.lab.call('discard_operation', {'proposal_id': proposal['proposal_id']})
            self.append_ai_text(f"✖ {result.get('error', '自然語言規劃失敗')}")
            return
        tool_details = [
            f"Function：{call['name']} {call['arguments']} → {observation.get('status')}"
            for call, observation in zip(result.get('tool_calls', []),
                                         result.get('results', []))
        ]
        evidence = self.console_tabs.widget(3)
        for line in tool_details:
            if isinstance(evidence, QTextEdit):
                evidence.insertPlainText('\n' + line)
        pending = [r.get('data', {}).get('proposal') for r in result.get('results', [])
                   if r.get('data', {}).get('proposal')]
        if pending:
            self.append_ai_result(result.get('assistant_message', ''),
                                  result.get('details', []), result.get('detail_links', []))
            decision = QMessageBox.question(
                self, '確認容器操作（不變更宿主）',
                json.dumps(pending, ensure_ascii=False, indent=2),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if decision == QMessageBox.StandardButton.Yes:
                def apply_operations() -> dict[str, Any]:
                    outcomes = []
                    failed = False
                    for proposal in pending:
                        if failed:
                            self.lab.call('discard_operation', {'proposal_id': proposal['proposal_id']})
                            continue
                        outcome = self.lab.call('apply_operation', {
                            'proposal_id': proposal['proposal_id'], 'confirm': True})
                        outcomes.append(outcome)
                        failed = outcome.get('status') != 'ok'
                    return {'status': 'error' if failed else 'ok', 'results': outcomes,
                            'notice': '失敗即停止後續操作；不自動回滾已執行步驟。'}
                self._run_worker('runtime-operation', apply_operations)
            else:
                for proposal in pending:
                    self.lab.call('discard_operation', {'proposal_id': proposal['proposal_id']})
                self.append_ai_text('已取消容器操作。')
            self.sync_canvas_from_workspace()
            return
        self.sync_canvas_from_workspace()
        if result.get('requires_confirmation'):
            if result.get('assistant_message'):
                self.append_ai_text(result['assistant_message'])
            self.config_manager.start_config(full=result.get('start_mode') == 'start-full')
        elif result.get('assistant_message'):
            self.append_ai_result(
                result['assistant_message'],
                result.get('details', []),
                result.get('detail_links', []))

    def sync_canvas_from_workspace(self) -> None:
        items = {item.node_id: item for item in self.scene.items()
                 if isinstance(item, NodeItem) and item.node_id != '-preview'}
        for draft_id, draft in self.lab.drafts.items():
            item = items.get(draft_id)
            if item is None:
                item = NodeItem(draft_id, draft['name'], draft['role'])
                item.setPos(80 + len(items) * 90, 100 + (len(items) % 3) * 100)
                self.scene.addItem(item)
                items[draft_id] = item
            else:
                item.name = draft['name']
                item.set_role_visual(draft['role'])
                item.text_item.setPlainText(item._format_text())
                item._position_label()
        existing_pairs = [{link.source.name, link.target.name}
                          for link in self.link_items.values()]
        name_items = {item.name: item for item in items.values()}
        for link_id, link in self.lab.link_drafts.items():
            pair = {link['source'], link['target']}
            if pair not in existing_pairs and pair <= set(name_items):
                visual = LinkItem(name_items[link['source']], name_items[link['target']])
                self.scene.addItem(visual)
                self.link_items[link_id] = visual
        self._update_selection_actions()

    def update_config_ui(self) -> None:
        state = self.config_manager.state
        enabled = state != ConfigState.CONFIRMED
        self.console_input.setEnabled(enabled)
        self.btn_console_submit.setEnabled(enabled)
        if state == ConfigState.SUMMARY:
            self.btn_console_submit.setText('確認')
        else:
            self.btn_console_submit.setText('送出')
        self._update_selection_actions()

    def show_summary(self) -> None:
        self.append_ai_text('\n--- 配置計畫摘要 ---')
        for d_id, data in self.lab.drafts.items():
            self.append_ai_text(f'節點: {data["name"]} [{data["role"]}] IP: {data["ip"]} Image: {data["image"]}')
        for l_id, data in self.lab.link_drafts.items():
            self.append_ai_text(f'鏈路: {data["source"]} <-> {data["target"]}')
        self.append_ai_text('--------------------')
        self.append_ai_text('可繼續以自然語言或畫布修改草稿；完成後按「確認執行」部署。')

    def _canvas_experiment_state(self) -> dict[str, Any]:
        store = self.satellite_store
        return {
            'selectedScenarioId': store.selectedScenarioId,
            'activeBeams': list(store.activeBeams),
            'selectedBeamId': store.selectedBeamId,
            'utsPerBeam': store.utsPerBeam,
            'endUsersPerUt': store.endUsersPerUt,
            'trafficDirection': store.trafficDirection,
            'protocol': store.protocol,
            'trafficRateKbps': store.trafficRateKbps,
            'simulationTime': store.simulationTime,
        }

    def _apply_experiment_snapshot(self, snapshot: dict[str, Any]) -> None:
        config = snapshot.get('config', snapshot)
        store = self.satellite_store
        if isinstance(config.get('uts_per_beam'), int):
            store.utsPerBeam = config['uts_per_beam']
        if isinstance(config.get('end_users_per_ut'), int):
            store.endUsersPerUt = config['end_users_per_ut']
        if isinstance(config.get('traffic_rate_kbps'), (int, float)):
            store.trafficRateKbps = config['traffic_rate_kbps']
        if isinstance(config.get('packet_size'), int):
            store.packetSize = config['packet_size']
        if isinstance(config.get('packet_interval'), (int, float)):
            store.packetInterval = config['packet_interval']
        if isinstance(config.get('simulation_time'), (int, float)):
            store.simulationTime = config['simulation_time']
        if isinstance(config.get('statistics'), list):
            store.statistics = list(config['statistics'])
        store.wizardState = dict(snapshot.get('wizardState') or snapshot.get('wizard') or {})
        store.dirtyParameters = list(snapshot.get('dirtyParameters') or [])
        store.changed.emit()

    def _start_satellite_wizard(self, mode: str) -> None:
        experiment_id = {'fixed_rate_cbr': 'fixed-rate', 'random_access': 'random-access',
                         'acm_training': 'multi-user-acm'}.get(
                             self.satellite_experiment_selector.currentData(), 'fixed-rate')
        self.lab.call('select_experiment', {'experiment_id': experiment_id})
        result = self.lab.call('start_experiment_wizard', {
            'mode': mode, 'canvas_state': self._canvas_experiment_state()})
        if result.get('status') != 'ok':
            self.append_ai_text('✖ ' + result.get('error', '無法啟動實驗設定精靈。'))
            return
        self.set_ai_dock_expanded(True)
        self._render_experiment_wizard(result['data'])

    def _start_hypatia_wizard(self, mode: str) -> None:
        result = self.lab.call('start_hypatia_wizard', {'mode': mode})
        if result.get('status') != 'ok':
            self.append_ai_text('✖ ' + result.get('error', '無法啟動 Hypatia 精靈。')); return
        self.set_ai_dock_expanded(True)
        self._render_hypatia_wizard(result['data'])

    def _render_hypatia_wizard(self, data: dict[str, Any]) -> None:
        state = data.get('wizard') if isinstance(data.get('wizard'), dict) else data.get('state')
        if isinstance(state, dict): self.hypatia_panel.apply_hypatia_wizard_state(state)
        if data.get('status') == 'prompt':
            parameter = data['parameter']; options = parameter.get('enum') or []
            ai_job = self.lab.runtime_jobs.latest('AI')
            if ai_job and ai_job.get('status') not in {'FAILED', 'CANCELLED'}:
                self.lab.runtime_jobs.update(ai_job['job_id'], status='CONFIGURING',
                    stage=str(parameter['id']).upper(),
                    message=f"Waiting for {parameter['display_name']}.")
            current = data.get('current_value')
            text = (f"Hypatia · Step {data['step']} / {data['total']} · {parameter['display_name']}\n\n"
                    f"{parameter.get('description', '')}\n\n目前：{current}")
            if parameter.get('id') == 'time_step_ms' and isinstance(state, dict):
                text += f"\n\nEstimated States: {state.get('estimated_states')}"
            if parameter.get('id') == 'threads' and isinstance(state, dict):
                text += (f"\n\nCPU Cores: {state.get('cpu_count')}\n"
                         f"Auto resolves to {state.get('resolved_threads')} threads.\n"
                         'Only forwarding-state generation uses this worker count.')
            if options: text += '\n可用值：' + ' / '.join(map(str, options))
            text += '\n\n按 Enter 保留目前值，或輸入新的值。'
            self.append_ai_text(text); return
        if data.get('status') == 'confirmation':
            ai_job = self.lab.runtime_jobs.latest('AI')
            if ai_job and ai_job.get('status') not in {'FAILED', 'CANCELLED'}:
                self.lab.runtime_jobs.update(ai_job['job_id'], status='CONFIGURING',
                    stage='WAITING_FOR_CONFIRMATION', message='Summary ready; waiting for Run, Modify, or Cancel.')
            summary = data.get('summary', {})
            dataset = summary.get('selected_dataset') or summary.get('compatible_dataset') or {}
            pending = self._resolve_hypatia_wizard_endpoints(summary, dataset)
            summary['pending_ground_stations'] = pending
            self.lab.hypatia_wizard.set_pending_ground_stations(pending)
            duration, cadence = summary.get('duration_s'), summary.get('time_step_ms')
            dataset_label = ('Quick' if (duration, cadence) == (20, 1000) else
                             'Demo' if (duration, cadence) == (60, 500) else
                             'Detailed' if (duration, cadence) == (200, 100) else 'Selected')
            source = summary.get('source') or 'Not set'; destination = summary.get('destination') or 'Not set'
            pending_names = {self.lab.hypatia_visualization.endpoint_resolver.normalized_name(item['requested_location']) for item in pending}
            normalize = self.lab.hypatia_visualization.endpoint_resolver.normalized_name
            stations = {normalize(name) for name in dataset.get('ground_stations', [])}
            source_available = normalize(source) in stations
            destination_available = normalize(destination) in stations
            lines = ['Hypatia Analysis Summary',
                     f"Network: {summary.get('network')}",
                     f"Dataset Policy: {summary.get('dataset_policy')}",
                     f'Dataset: Starlink {dataset_label} Dataset' if summary.get('network') == 'starlink_550' else f"Dataset: {dataset_label} Dataset",
                     f"Dataset ID: {dataset.get('dataset_id') or 'Will create new'}",
                     f'Duration: {duration} s', f'Time Step: {cadence} ms',
                     f"Estimated States: {summary.get('estimated_states')}",
                     f"ISL: {summary.get('isl_selection')}",
                     f"Ground Stations: {summary.get('ground_station_set')}",
                     f"Routing: {summary.get('routing_algorithm')}",
                     f"Threads: {summary.get('requested_threads')} → {summary.get('resolved_threads')} (forwarding state)",
                     f'Source: {source} · {"Available" if source_available else "Resolved; will add" if normalize(source) in pending_names else "Not resolved"}',
                     f'Destination: {destination} · {"Available" if destination_available else "Resolved; will add" if normalize(destination) in pending_names else "Not resolved"}',
                     'Pending Ground Stations: ' + (', '.join(item['station']['name'] for item in pending) or 'None'),
                     f"GEN_TIME: {summary.get('gen_time_ms')} ms",
                     f"Playback: {summary.get('playback_enabled')} · step {summary.get('playback_step_ms')} ms · speed {summary.get('playback_speed')}x",
                    ]
            lines.append('\n[執行]  [修改]  [取消]\n只有輸入「執行」/run/analyze 才會開始 Job。')
            self.append_ai_text('\n'.join(lines))

    def _resolve_hypatia_wizard_endpoints(self, summary: dict[str, Any],
                                          dataset: dict[str, Any]) -> list[dict[str, Any]]:
        pending: list[dict[str, Any]] = []
        dataset_id = dataset.get('dataset_id')
        for location in (summary.get('source'), summary.get('destination')):
            if not isinstance(location, str) or not location.strip():
                continue
            if dataset_id:
                check = self.lab.call('check_ground_station', {'dataset_id': dataset_id, 'location': location})
                if check.get('data', {}).get('status') == 'FOUND':
                    continue
            local = self.lab.call('search_local_ground_station', {'location': location}).get('data', {})
            resolved = local if local.get('status') == 'FOUND_IN_LOCAL_CATALOG' else \
                self.lab.call('resolve_location', {'location': location}).get('data', {})
            station = resolved.get('station')
            if isinstance(station, dict) and resolved.get('status') in {'FOUND_IN_LOCAL_CATALOG', 'FOUND_BY_GEOCODER'}:
                pending.append({'requested_location': location, 'station': station,
                                'source': resolved.get('source') or station.get('source')})
            elif resolved.get('status') == 'AMBIGUOUS':
                self.append_ai_text(f'⚠ {location} 有多個解析結果，請先指定地區後再執行。')
            else:
                self.append_ai_text(f'⚠ 無法可靠解析 {location}；執行前需要 latitude / longitude。')
        return pending

    def _analyze_hypatia_wizard(self) -> None:
        state = self.lab.call('get_hypatia_wizard_state', {}).get('data', {})
        try:
            state = self.lab.hypatia_wizard.confirm()
        except ValueError as error:
            self.append_ai_text('✖ ' + str(error)); return
        datasets = self.lab.call('list_hypatia_datasets', {}).get('data', {}).get('datasets', [])
        base_id = state.get('dataset_id') or (state.get('compatible_dataset') or {}).get('dataset_id')
        selected = next((item for item in datasets if item.get('dataset_id') == base_id and item.get('status') == 'READY'), None)
        policy = state.get('dataset_policy')
        if selected is None and policy == 'reuse':
            self.append_ai_text('✖ Dataset Policy=Reuse，但沒有完全相容的 READY Dataset。'); return
        if selected is None:
            generated = self.lab.call('generate_hypatia_dataset', {
                'network': state['network'], 'duration_sec': state['duration_s'],
                'step_ms': state['time_step_ms'], 'isl_mode': state['isl_selection'],
                'ground_station_set': state['ground_station_set'],
                'routing_algorithm': state['routing_algorithm'], 'threads': state['threads'],
                'force_new_dataset': policy == 'new'})
            if generated.get('status') != 'ok':
                self.append_ai_text('✖ ' + generated.get('error', 'Dataset generation could not start.')); return
            self.hypatia_panel._job_id = generated['data']['job_id']
            self.hypatia_panel._continue_after_custom = True
            self.hypatia_panel._continue_after_hypatia_wizard = True
            self.hypatia_panel._hypatia_wizard_callback = self._analyze_hypatia_wizard
            self.hypatia_panel.cancel.setVisible(True)
            self.hypatia_panel.status.setText('Generating Dataset; analysis will continue when READY…')
            self.append_ai_text('Hypatia Dataset 已以背景 Job 開始；完成後將使用原始 Source → Destination 繼續。')
            return
        state = self.lab.hypatia_wizard.set_dataset(selected)
        self.hypatia_panel.apply_hypatia_wizard_state(state)
        dataset_id, source, destination = state.get('dataset_id'), state.get('source'), state.get('destination')
        if not all(isinstance(value, str) and value for value in (dataset_id, source, destination)):
            self.append_ai_text('✖ 請先完成 Network、Source、Destination 與 Dataset。'); return
        job = self.lab.call('create_hypatia_analysis', {
            'dataset_id': dataset_id, 'source': source, 'destination': destination,
            'network': state['network'], 'gen_time_ms': int(state['gen_time_ms']),
            'force_new': True, 'force_new_dataset': policy == 'new',
            'threads': state.get('threads', 'auto')})
        if job.get('status') != 'ok':
            self.append_ai_text('✖ ' + job.get('error', 'Hypatia analysis could not start.')); return
        self.hypatia_panel._job_id = job['data']['job_id']
        self.hypatia_panel.cancel.setVisible(True)
        self.hypatia_panel.status.setText('Validating endpoints and preparing Path / RTT…')
        self.append_ai_text(f"Hypatia Job 已開始：{job['data']['job_id']}\n"
                            f"Dataset Policy: {policy} · GEN_TIME: {state['gen_time_ms']} ms")

    def _confirm_pending_hypatia_station(self) -> None:
        station = getattr(self, '_pending_hypatia_station', None)
        state = self.lab.call('get_hypatia_wizard_state', {}).get('data', {})
        selected_dataset = state.get('selected_dataset')
        if (not isinstance(station, dict) or not isinstance(selected_dataset, dict)
                or selected_dataset.get('status') != 'READY'
                or selected_dataset.get('dataset_id') != state.get('dataset_id')):
            self.append_ai_text('目前沒有待確認的 Custom Ground Station。'); return
        datasets = self.lab.call('list_hypatia_datasets', {}).get('data', {}).get('datasets', [])
        required_names = {self.lab.hypatia_visualization.endpoint_resolver.normalized_name(name)
                          for name in (state.get('source'), state.get('destination'), station.get('name'))
                          if isinstance(name, str)}
        cached_custom = next((item for item in datasets
                              if item.get('status') == 'READY' and item.get('custom')
                              and item.get('network') == selected_dataset.get('network')
                              and item.get('duration_s') == selected_dataset.get('duration_s')
                              and item.get('step_ms') == selected_dataset.get('step_ms')
                              and item.get('isl_mode') == selected_dataset.get('isl_mode')
                              and item.get('routing_algorithm') == selected_dataset.get('routing_algorithm')
                              and item.get('base_ground_station_set') == selected_dataset.get('ground_station_set')
                              and required_names <= {
                                  self.lab.hypatia_visualization.endpoint_resolver.normalized_name(name)
                                  for name in item.get('ground_stations', [])}), None)
        if cached_custom:
            self.hypatia_panel._preferred_dataset_id = cached_custom['dataset_id']
            self.hypatia_panel.apply_hypatia_wizard_state(
                self.lab.hypatia_wizard.set_dataset(cached_custom))
            self.append_ai_text('已找到相同設定且包含 Taipei / Tokyo 的 READY Custom Dataset，直接重用並自動繼續分析。')
            self._pending_hypatia_station = None
            self.hypatia_panel._analyse()
            return
        created = self.lab.call('create_custom_ground_station_set', {
            'base_dataset_id': selected_dataset['dataset_id'], 'stations': [station]})
        if created.get('status') != 'ok':
            self.append_ai_text('✖ ' + created.get('error', '無法建立 Custom Ground Station Set。')); return
        config = created['data']['generation_config']
        config['threads'] = state.get('threads', 1)
        generated = self.lab.call('generate_hypatia_dataset', config)
        if generated.get('status') == 'ok':
            self.append_ai_text('已建立 Custom Ground Station Set，Hypatia Dataset 正在背景生成；完成後會保留原本的 Source → Destination intent。')
            self.hypatia_panel._job_id = generated['data'].get('job_id')
            self.hypatia_panel._continue_after_custom = True
            self.hypatia_panel.cancel.setVisible(True)
            self.hypatia_panel.status.setText('Generating Custom Ground Station Dataset…')
            self._pending_hypatia_station = None
        else:
            self.append_ai_text('✖ ' + generated.get('error', 'Dataset generation 無法開始。'))

    def _show_experiment_catalog(self) -> None:
        result = self.lab.call('list_experiments', {})
        if result.get('status') != 'ok':
            self.append_ai_text('✖ Experiment Registry unavailable.'); return
        labels = {'fixed-rate': ('① 固定速率流量效能', 'UT 數量、Traffic Load、Throughput、Delay'),
                  'random-access': ('② Random Access', 'Offered Load、Collision、Packet Error、Throughput'),
                  'multi-user-acm': ('③ Multi-user + ACM', '多使用者、SINR、Waveform / MODCOD、Throughput')}
        lines = ['可進行的衛星實驗：', '']
        for item in result['data']['experiments']:
            title, purpose = labels.get(item['id'], (item['displayName'], item.get('description', '')))
            state = 'Available' if item['availability']['available'] else 'Unavailable'
            lines.extend([title, f'   研究：{purpose}', f'   {state}', ''])
        lines.extend(['進階實驗由 installed SNS-3 examples 決定：Slotted ALOHA、CRDSA、Dynamic Load Control、Constellation 等。',
                      '選擇實驗後才會顯示對應官方 example 與可調參數。'])
        self.append_ai_text('\n'.join(lines))

    def _render_experiment_wizard(self, data: dict[str, Any]) -> None:
        status = data.get('status')
        if status == 'prompt':
            parameter = data['parameter']
            current = data.get('currentValue')
            unit = parameter.get('unit', '')
            derived = data.get('derived', {})
            text = (f"Step {data['step']} / {data['total']} · {parameter['displayName']}\n\n"
                    f"{parameter.get('description', '')}\n\n目前：{current} {unit}".rstrip())
            if parameter['id'] == 'uts_per_beam':
                text += (f"\nActive Beam：{derived.get('active_beam_count', 0)}"
                         f"\nTotal UT：{derived.get('total_ut', 0)}")
            if parameter.get('enum'):
                text += '\n可用值：' + ' / '.join(map(str, parameter['enum']))
            text += '\n\n按 Enter 保留目前值，或輸入新的值。'
            self.append_ai_text(text)
            self.satellite_store.wizardState = dict(data.get('wizard', {}))
            return
        if status == 'confirmation':
            self._experiment_validated_id = data.get('validatedConfigId')
            self._apply_experiment_snapshot(data)
            config = data.get('config', {})
            errors = data.get('validation', {}).get('errors', [])
            summary = (
                '固定流量實驗準備完成\n\n'
                f"Scenario：{config.get('scenario')}\n"
                f"Profile：{config.get('scenario_profile')}\n"
                'Beam / UT topology：由官方 profile 建立\n'
                '實際 Active Beams 與 UT 分布：執行後由 CreationTrace 回填\n'
                f"Protocol：{config.get('protocol')}\n"
                f"Traffic：{config.get('traffic_rate_kbps')} kbps / flow\n"
                f"Packet Size：{config.get('packet_size')} B\n"
                f"Simulation：{config.get('simulation_time')} s\n\n")
            summary += ('輸入 run 執行，或繼續輸入參數修改。' if not errors
                        else '目前不可執行：' + '; '.join(errors))
            self.append_ai_text(summary)

    def _try_wizard_mutation(self, text: str) -> bool:
        import re
        matchers = (
            (r'(?:ut|uts)(?:\s*per\s*beam)?\D+(\d+)', 'uts_per_beam', int),
            (r'(\d+(?:\.\d+)?)\s*(mbps|kbps)', 'traffic_rate_kbps',
             lambda value, unit: float(value) * (1000 if unit.lower() == 'mbps' else 1)),
            (r'(?:模擬時間|simulation\s*time)\D+(\d+(?:\.\d+)?)', 'simulation_time', float),
        )
        lowered = text.lower()
        for pattern, parameter_id, converter in matchers:
            match = re.search(pattern, lowered, re.IGNORECASE)
            if not match: continue
            value = converter(*match.groups()) if len(match.groups()) > 1 else converter(match.group(1))
            result = self.lab.call('set_experiment_parameter', {
                'experiment_id': self.lab.experiment_session.experiment_id,
                'parameter_id': parameter_id, 'value': value})
            if result.get('status') != 'ok':
                self.append_ai_text('✖ ' + result.get('error', '參數修改失敗。')); return True
            self._apply_experiment_snapshot(result['data'])
            self._experiment_validated_id = None
            change = result['data']
            self.append_ai_text(f"{parameter_id}：{change['old']} → {change['value']}")
            next_result = self.lab.call('get_next_parameter', {})
            if next_result.get('status') == 'ok': self._render_experiment_wizard(next_result['data'])
            return True
        return False

    def _run_current_experiment(self) -> None:
        self.lab.call('sync_experiment_canvas', {'state': self._canvas_experiment_state()})
        validated_id = getattr(self, '_experiment_validated_id', None)
        if not validated_id:
            preview = self.lab.call('preview_experiment', {})
            if preview.get('status') != 'ok':
                self.append_ai_text('✖ ' + preview.get('error', 'Experiment validation failed.'))
                return
            validated_id = preview['data']['validatedConfigId']
            self._experiment_validated_id = validated_id
        preview = self.lab.experiment_session.preview()
        message = json.dumps({'experiment': preview['experimentId'],
                              'config': preview['config'], 'derived': preview['derived']},
                             ensure_ascii=False, indent=2)
        confirm = QMessageBox.question(self, '確認執行 SNS-3', message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if confirm != QMessageBox.StandardButton.Yes:
            self.append_ai_text('已取消 SNS-3 執行。'); return
        self.sns3_status.set_status('RUNNING', 'Running Simulation')
        self.append_ai_text('Running SNS-3… 完成後將自動掃描、解析並建立 Evidence。')
        self._run_worker('sns3-run', lambda: self.lab.call(
            'run_experiment', {'validated_config_id': validated_id, 'confirm': True}))

    def on_console_submit(self) -> None:
        text = self.console_input.text().strip()
        if not text:
            if self.lab.hypatia_wizard.state.get('current_parameter_id'):
                result = self.lab.call('answer_hypatia_wizard', {'value': ''})
                if result.get('status') == 'ok': self._render_hypatia_wizard(result['data'])
                else: self.append_ai_text('✖ ' + result.get('error', 'Hypatia parameter is invalid.'))
                return
            if self.lab.experiment_session.wizard.get('currentParameterId'):
                result = self.lab.call('answer_experiment_wizard', {'value': ''})
                if result.get('status') == 'ok': self._render_experiment_wizard(result['data'])
                return
            if self.config_manager.state in (ConfigState.PROMPTING_NODES, ConfigState.PROMPTING_LINKS):
                self.console_input.clear()
                self.config_manager.handle_input('')
            return
        self.console_input.clear()
        lowered = text.lower()
        if lowered in {'hypatia start', 'hypatia start-full'}:
            self.append_ai_text(f'> {text}')
            self._start_hypatia_wizard('start-full' if lowered.endswith('start-full') else 'start')
            return
        if (lowered in {'run', '執行', 'analyze', '分析', '分析路徑'}
                and self.lab.hypatia_wizard.state.get('mode')
                and self.lab.hypatia_wizard.state.get('current_parameter_id') is None):
            self.append_ai_text(f'> {text}')
            self._analyze_hypatia_wizard()
            return
        if (lowered in {'cancel', '取消'} and self.lab.hypatia_wizard.state.get('mode')):
            self.lab.hypatia_wizard.state['mode'] = None
            self.lab.hypatia_wizard.state['confirmed'] = False
            self.lab.hypatia_wizard._save()
            self.append_ai_text('Hypatia Wizard 已取消；沒有啟動 Job。')
            return
        if lowered in {'新增地面站', 'add ground station', 'add station'}:
            self.append_ai_text(f'> {text}')
            self._confirm_pending_hypatia_station()
            return
        if self.lab.hypatia_wizard.state.get('current_parameter_id') and lowered not in {'start', 'start-full'}:
            self.append_ai_text(f'> {text}')
            result = self.lab.call('answer_hypatia_wizard', {'value': text})
            if result.get('status') == 'ok': self._render_hypatia_wizard(result['data'])
            else: self.append_ai_text('✖ ' + result.get('error', 'Hypatia 參數不符合 capability schema。'))
            return
        if (lowered in {'start', 'start-full'}
                and not (self.lab.drafts and not self.satellite_store.satellitePlaced)):
            self.append_ai_text(f'> {text}')
            self._start_satellite_wizard(lowered)
            return
        if '有哪些實驗' in text or '可做什麼實驗' in text:
            self.append_ai_text(f'> {text}'); self._show_experiment_catalog(); return
        experiment_intent = None
        if '固定流量' in text: experiment_intent = ('fixed-rate', 'fixed_rate_cbr')
        elif 'random access' in lowered or '隨機存取' in text: experiment_intent = ('random-access', 'random_access')
        elif 'acm' in lowered: experiment_intent = ('multi-user-acm', 'acm_training')
        if experiment_intent and any(token in text for token in ('我要測', '開始', '實驗')):
            self.append_ai_text(f'> {text}')
            index = self.satellite_experiment_selector.findData(experiment_intent[1])
            if index >= 0: self.satellite_experiment_selector.setCurrentIndex(index)
            self._start_satellite_wizard('start'); return
        if lowered in {'run', '執行', '跑目前設定'} and getattr(self, '_experiment_validated_id', None):
            self.append_ai_text(f'> {text}')
            self._run_current_experiment(); return
        if self.lab.experiment_session.wizard.get('currentParameterId'):
            self.append_ai_text(f'> {text}')
            if self._try_wizard_mutation(text): return
            result = self.lab.call('answer_experiment_wizard', {'value': text})
            if result.get('status') == 'ok': self._render_experiment_wizard(result['data'])
            else: self.append_ai_text('✖ ' + result.get('error', '輸入不符合參數 schema。'))
            return
        if (self.config_manager.state not in {ConfigState.IDLE, ConfigState.CONFIRMED}
                and self.config_manager.handle_input(text)):
            return
        self.append_ai_text(f'> {text}')
        if self.agent is None:
            self.configure_agent(self.provider_selector.currentText())
        if self.agent is None:
            self.append_ai_text('✖ LLM 尚未設定；請配置所選 provider 的 API key。')
            return
        agent = self.agent
        self._run_worker(
            'agent', lambda: {**agent.run(text), 'start_mode': text.lower()})

    def on_add_node(self) -> None:
        # This method is kept for compatibility but is now hidden; we keep it for potential future use.
        from leo_lab import ROLES
        dlg = NodeDialog(self, ROLES)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            data = dlg.get_data()
            res = self.lab.call('draft_node', data)
            if res['status'] == 'draft':
                node_data = res['data']['node']
                node_id = res['data']['draft_id']
                item = NodeItem(node_id, node_data['name'], node_data['role'])
                self.scene.addItem(item)
                self.statusBar().showMessage(f"已新增草稿節點 {node_data['name']}")
            else:
                QMessageBox.critical(self, '錯誤', res.get('error', '未知錯誤'))

    def set_placement_role(self, role: str) -> None:
        if role == 'satellite':
            self.beginSatelliteCreation()
            return
        self.role_selector.setCurrentText(role)

    def _update_selection_actions(self) -> None:
        nodes = [item for item in self.scene.selectedItems()
                 if isinstance(item, NodeItem) and item.node_id != '-preview']
        busy = bool(self._worker_kind)
        self.btn_create_link.setEnabled(len(self.lab.drafts) + len(self.lab.nodes) >= 2 and not busy)
        self.btn_open_terminal.setEnabled(
            len(nodes) == 1 and nodes[0].name in self.lab.nodes and not busy)
        self.btn_execute.setEnabled(bool(self.lab.drafts or self.lab.link_drafts) and not busy)
        self.btn_execute_links.setEnabled(bool(self.lab.link_drafts) and not busy and all(
            link['source'] in self.lab.nodes and link['target'] in self.lab.nodes
            for link in self.lab.link_drafts.values()))
        self.btn_stop.setEnabled(self.lab.backend is not None and not busy)
        can_manage_flows = (self.lab.backend is not None and len(self.lab.nodes) >= 2
                            and bool(self.lab.active_links) and not busy)
        self.btn_create_flow.setEnabled(can_manage_flows)
        self.btn_flow_status.setEnabled(self.lab.backend is not None and not busy)
        self.btn_stop_flow.setEnabled(self.lab.backend is not None and not busy)
        self.btn_view_changes.setEnabled(not busy)

    def on_create_flow(self) -> None:
        selected_names = [item.name for item in self.scene.selectedItems()
                          if isinstance(item, NodeItem) and item.name in self.lab.nodes]
        dialog = FlowDialog(self, sorted(self.lab.nodes), selected_names)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        request = dialog.get_data()
        if request['source'] == request['destination']:
            QMessageBox.warning(self, '無法建立流量', '來源與目標必須是不同節點。')
            return
        proposed = self.lab.call('propose_flow', request)
        if proposed['status'] != 'draft':
            QMessageBox.warning(self, '無法建立流量', proposed.get('reason', proposed.get('error', '未知錯誤')))
            return
        proposal = proposed['data']['proposal']
        preview = proposal['preview']
        message = (f"{preview['source']} → {preview['destination']}\n"
                   f"{preview['protocol'].upper()} · {preview['rate_mbps']} Mbps · "
                   f"{preview['duration_seconds']} 秒\n\n"
                   '確認後才會在此工作階段啟動 iperf3。')
        if QMessageBox.question(
            self, '確認啟動實驗流量', message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._run_worker('flow-start', lambda: self.lab.call('apply_flow', {
            'proposal_id': proposal['proposal_id'], 'confirm': True}))

    def on_flow_status(self) -> None:
        result = self.lab.call('flow_status', {})
        flows = result.get('data', {}).get('flows', [])
        if not flows:
            self.append_ai_text('目前沒有此工作階段建立的實驗流量。')
            return
        self.append_ai_result('實驗流量狀態已更新。', [], [{
            'kind': 'flow', 'label': '流量狀態',
            'properties': {'flow 數量': len(flows)},
            'content': json.dumps(flows, ensure_ascii=False, indent=2),
        }])

    def on_stop_flow(self) -> None:
        names = sorted(self.lab.nodes)
        source, accepted = QInputDialog.getItem(self, '停止流量', '來源節點', names, 0, False)
        if not accepted:
            return
        proposal = self.lab.call('propose_stop_flow', {'source': source})
        if proposal['status'] != 'draft':
            QMessageBox.warning(self, '無法停止流量', proposal.get('error', '未知錯誤'))
            return
        if QMessageBox.question(
            self, '確認停止實驗流量', f'停止來源 {source} 的所有 session flow？',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._run_worker('flow-stop', lambda: self.lab.call('apply_stop_flow', {
            'proposal_id': proposal['data']['proposal']['proposal_id'], 'confirm': True}))

    def on_create_link(self) -> None:
        selected = self.scene.selectedItems()
        nodes = [i for i in selected if isinstance(i, NodeItem)]
        if len(nodes) != 2:
            self.role_selector.setCurrentIndex(0)
            self._link_mode = not self._link_mode
            self._link_source = None
            self.statusBar().showMessage('請依序點擊來源與目標節點' if self._link_mode else '已取消建立鏈路')
            return
        source, target = nodes
        self._create_link(source, target)

    def _create_link(self, source: NodeItem, target: NodeItem) -> None:
        res = self.lab.call('draft_link', {'source': source.name, 'target': target.name})
        if res['status'] == 'draft':
            link_id = res['data']['link_id']
            link_item = LinkItem(source, target)
            self.scene.addItem(link_item)
            self.link_items[link_id] = link_item
            self.statusBar().showMessage(f"已建立草稿鏈路 {source.name} ↔ {target.name}")
            self.config_manager.state = ConfigState.IDLE
            self.update_config_ui()
        else:
            QMessageBox.critical(self, '錯誤', res.get('error', '未知錯誤'))

    def on_execute_confirmed(self) -> None:
        if self._worker_kind or not (self.lab.drafts or self.lab.link_drafts):
            return
        if self.config_manager.state != ConfigState.CONFIRMED:
            if not self._confirm_changes():
                return
            self.config_manager.state = ConfigState.CONFIRMED
        self.btn_execute.setEnabled(False)
        self.append_ai_text('\n[System] 開始部署節點...')
        draft_ids = list(self.lab.drafts.keys())
        node_names = [self.lab.drafts[draft_id]['name'] for draft_id in draft_ids]
        link_count = len(self.lab.link_drafts)
        try:
            for d_id in draft_ids:
                node_name = self.lab.drafts[d_id]['name']
                self.append_ai_text(f'Deploying {node_name}...')
                res = self.lab.call('apply_draft', {'draft_id': d_id, 'confirm': True})
                if res['status'] in {'ok', 'partial'}:
                    self.append_ai_text(f'{node_name} ... OK')
                    for item in self.scene.items():
                        if isinstance(item, NodeItem) and item.name == node_name:
                            item.set_status('Running')
                    if res['status'] == 'partial':
                        self.append_ai_text(f"⚠ {res.get('error', '節點已建立，但讀回失敗')}")
                else:
                    error_msg = res.get('error', 'Unknown error')
                    self.append_ai_text(f'{node_name} ... FAILED: {error_msg}')
                    if 'Containernet apply requires root' in error_msg:
                        QMessageBox.critical(self, '權限錯誤',
                                            '後端部署需要 root 權限。請以 sudo 啟動或配置 sudoers。')
                    else:
                        QMessageBox.critical(self, '部署失敗', f'部署 {node_name} 時發生錯誤:\n{error_msg}')
                    raise RuntimeError(f'Deployment stopped at {node_name}: {error_msg}')
            self.append_ai_text('[System] 節點部署完成，開始建立鏈路。')
            self._apply_pending_links()
            names = '、'.join(node_names)
            self.append_ai_text(
                f'✓ 所有內容成功部署：{len(node_names)} 個 Docker 節點'
                f'（{names}）、{link_count} 條鏈路。')
            self.statusBar().showMessage('實驗環境已啟動 · 容器運行中 · 拓樸已生效')
        except Exception as exc:
            self.append_ai_text(f'[System] 部署中斷: {exc}')
            self.btn_execute.setEnabled(True)
            return
        self.config_manager.state = ConfigState.IDLE
        self.update_config_ui()

    def on_execute_links(self) -> None:
        if self._worker_kind or not self.lab.link_drafts:
            return
        if not self._confirm_changes(links_only=True):
            return
        self.btn_execute_links.setEnabled(False)
        self.append_ai_text('\n[System] 開始建立網路鏈路...')
        workspace = self.lab.call('get_workspace', {})['data']
        link_drafts = workspace['link_drafts']
        if not link_drafts:
            self.append_ai_text('[System] 沒有待執行的鏈路草稿。')
            self.btn_execute_links.setEnabled(True)
            return
        for l_id, link in link_drafts.items():
            self.append_ai_text(f'正在建立: {link["source"]} <-> {link["target"]}...')
            res = self.lab.call('apply_link', {'link_id': l_id, 'confirm': True})
            if res['status'] == 'ok':
                self.append_ai_text(f'✓ 鏈路 {link["source"]} <-> {link["target"]} 已建立')
                if l_id in self.link_items:
                    self.link_items[l_id].set_active(True)
            else:
                self.append_ai_text(f'✗ 鏈路 {link["source"]} <-> {link["target"]} 失敗: {res.get("error")}')
        self.append_ai_text('[System] 鏈路部署流程結束。')
        self._update_selection_actions()

    def _apply_pending_links(self) -> None:
        for link_id, link in list(self.lab.link_drafts.items()):
            result = self.lab.call('apply_link', {
                'link_id': link_id, 'confirm': True,
            })
            if result['status'] != 'ok':
                raise RuntimeError(result.get('error', '鏈路建立失敗'))
            if link_id in self.link_items:
                self.link_items[link_id].set_active(True)
            self.append_ai_text(f"✓ {link['source']} ↔ {link['target']}")

    def mousePressEvent(self, event: QMouseEvent) -> None:
        super().mousePressEvent(event)
        self._update_selection_actions()

    def on_open_terminal(self) -> None:
        selected = self.scene.selectedItems()
        nodes = [i for i in selected if isinstance(i, NodeItem)]
        if len(nodes) == 1:
            self.open_node_terminal(nodes[0].name)

    def open_node_terminal(self, node_name: str) -> None:
        if self._worker_kind:
            return
        result = self.lab.call('open_terminal', {'name': node_name})
        editor = self.console_tabs.widget(1)
        if isinstance(editor, QTextEdit):
            editor.append(f"{node_name}: xterm 已啟動" if result['status'] == 'ok'
                          else f"{node_name}: {result.get('error')}")
        if result['status'] != 'ok':
            QMessageBox.warning(self, '無法開啟終端', result.get('error', '未知錯誤'))
        self._log_to_evidence_dock('open_terminal', result['operation_id'], node_name,
                                   result['status'], command_output=result.get('data'),
                                   error=result.get('error'))

    def _changes_text(self, links_only: bool = False) -> str:
        lines = []
        if not links_only:
            lines.extend(f"節點 {node['name']} [{node['role']}] {node['ip']} · {node['image']}"
                         for node in self.lab.drafts.values())
        lines.extend(f"鏈路 {link['source']} ↔ {link['target']}"
                     for link in self.lab.link_drafts.values())
        return '\n'.join(lines) or '沒有待執行變更。'

    def on_view_changes(self) -> None:
        QMessageBox.information(self, '待執行變更', self._changes_text())

    def _confirm_changes(self, links_only: bool = False) -> bool:
        return QMessageBox.question(
            self, '確認執行', self._changes_text(links_only),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes

    def on_stop(self) -> None:
        if self._worker_kind or self.lab.backend is None:
            return
        if QMessageBox.question(
            self, '停止實驗', '關閉此工作階段的 xterm、容器與鏈路，保留拓樸草稿？',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        result = self.lab.call('stop', {'confirm': True})
        if result['status'] != 'ok':
            QMessageBox.critical(self, '停止失敗', result.get('error', '未知錯誤'))
            return
        self._link_mode = False
        self._link_source = None
        self.role_selector.setCurrentIndex(0)
        positions = {item.name: item.pos() for item in self.scene.items() if isinstance(item, NodeItem)}
        self.link_items.clear()
        self.scene.clear()
        self.sync_canvas_from_workspace()
        for item in self.scene.items():
            if isinstance(item, NodeItem) and item.name in positions:
                item.setPos(positions[item.name])
        self.config_manager.state = ConfigState.IDLE
        self.update_config_ui()
        self.statusBar().showMessage('實驗已停止 · 拓樸草稿保留')

    def _render_command_result(self, node_name: str,
                               result: dict[str, Any]) -> None:
        term_editor = self.console_tabs.widget(1)
        if isinstance(term_editor, QTextEdit):
            if result['status'] == 'ok':
                term_editor.append(result['data']['output'])
                suffix = f"[exit {result['data']['exit_code']}]"
                if result['data'].get('timed_out'):
                    suffix += ' [timeout]'
                if result['data'].get('truncated'):
                    suffix += ' [output truncated]'
                term_editor.append(suffix)
            else:
                term_editor.append(f"Error: {result.get('error', 'Unknown')}")
        self._perform_automatic_checks(node_name, result)

    def _perform_automatic_checks(self, node_name: str, command_result: dict[str, Any]) -> None:
        """After a run_command, automatically query node IP and ping neighbors, and log to evidence dock."""
        # Log the command result first
        self._log_to_evidence_dock('run_command', command_result.get('operation_id', 'unknown'),
                                   node_name, command_result.get('status', 'unknown'),
                                   command_output=command_result.get('data', {}).get('output'),
                                   exit_code=command_result.get('data', {}).get('exit_code'),
                                   error=command_result.get('error'))
        # Query node for IP/interfaces
        query_res = self.lab.call('query_node', {'name': node_name})
        self._log_to_evidence_dock('query_node', query_res.get('operation_id', 'unknown'),
                                   node_name, query_res.get('status', 'unknown'),
                                   command_output=query_res.get('data'))
        # If query successful, get neighbors from active links and ping them
        if query_res.get('status') == 'ok':
            workspace = self.lab.call('get_workspace', {})['data']
            active_links = workspace.get('links', [])
            for link in active_links:
                src = link['source']
                tgt = link['target']
                neighbor = None
                if src == node_name:
                    neighbor = tgt
                elif tgt == node_name:
                    neighbor = src
                if neighbor:
                    ping_res = self.lab.call('ping_test', {'source': node_name, 'target': neighbor})
                    self._log_to_evidence_dock('ping_test', ping_res.get('operation_id', 'unknown'),
                                               f"{node_name}->{neighbor}", ping_res.get('status', 'unknown'),
                                               command_output=ping_res.get('data'))

    def _log_to_evidence_dock(self, tool: str, operation_id: str, target: str, status: str,
                              command_output: Any = None, exit_code: Any = None, error: Any = None) -> None:
        evidence_editor = self.console_tabs.widget(3)  # Index 3 is 工具證據
        if not isinstance(evidence_editor, QTextEdit):
            return
        from datetime import datetime
        timestamp = datetime.now().strftime('%H:%M:%S')
        lines = []
        lines.append(f"[{timestamp}] {tool} op={operation_id} target={target} status={status}")
        if error:
            lines.append(f"  Error: {error}")
        if command_output is not None:
            if isinstance(command_output, dict):
                # Format dict nicely
                for key, val in command_output.items():
                    lines.append(f"  {key}: {val}")
            else:
                lines.append(f"  Output: {command_output}")
        if exit_code is not None:
            lines.append(f"  Exit code: {exit_code}")
        lines.append("")  # blank line
        evidence_editor.append('\n'.join(lines))
        # Auto-scroll to bottom
        cursor = evidence_editor.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        evidence_editor.setTextCursor(cursor)

    def show_scope(self) -> None:
        QMessageBox.information(
            self,
            '目前功能範圍',
            '已提供：節點與鏈路草稿、start / start-full 逐項確認，以及配置摘要。\n'
            '已接入：確認部署、xterm、原生 LLM function calls、介面與 ping 觀測。\n'
            '新增：確認式容器離線 apt 快取安裝與單向 netem；不建立管理網路。\n'
            'start-full 欄位流程不變；netem 另走執行中介面的確認提案。\n'
            '物理碰撞模擬、在線下載、完整參考工具集尚不支援。詳見 docs/reference-tool-mapping.md。',
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._worker_kind:
            QMessageBox.warning(self, '操作執行中', '請等待目前操作結束後再關閉。')
            event.ignore()
            return
        try:
            self.lab.close()
        except Exception as exc:
            QMessageBox.critical(self, '工作階段未能關閉', str(exc))
            event.ignore()
            return
        event.accept()

    def edit_node_parameters(self, item: NodeItem) -> None:
        """Open parameter dialog for editing a draft node."""
        if item.status != 'Draft':
            QMessageBox.warning(self, '提示', '只能編輯草稿節點的參數。')
            return
        # Get current draft data from lab
        draft_id = item.node_id
        draft = None
        for did, data in self.lab.drafts.items():
            if did == draft_id:
                draft = data
                break
        if draft is None:
            QMessageBox.critical(self, '錯誤', '找不到對應的草稿。')
            return
        dlg = NodeDialog(self, ROLES, draft)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            old_name = item.name
            result = self.lab.call('update_draft', {
                'draft_id': draft_id,
                **dlg.get_data(),
            })
            if result['status'] != 'draft':
                QMessageBox.critical(self, '參數無效', result.get('error', '未知錯誤'))
                return
            new_data = result['data']['node']
            item.name = new_data['name']
            item.set_role_visual(new_data['role'])
            item.text_item.setPlainText(item._format_text())
            self.statusBar().showMessage(f"已更新節點 {old_name} → {item.name}。")

def main() -> int:
    import hashlib
    import inspect
    import marshal
    import os
    from containernet_backend import ContainernetBackend

    backend_path = inspect.getfile(ContainernetBackend)
    fingerprint = hashlib.sha256(marshal.dumps(ContainernetBackend.create.__code__)).hexdigest()[:12]
    print(f'[LEO runtime] pid={os.getpid()} python={sys.executable} '
          f'backend={backend_path} create={fingerprint}', flush=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--screenshot', type=Path, help='Render the real Qt window to PNG and exit.')
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setFont(QFont('Noto Sans CJK TC', 10))
    window = LabWindow()
    window.show()
    failure = False
    def capture() -> None:
        nonlocal failure
        try:
            assert args.screenshot is not None
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            if not window.grab().save(str(args.screenshot), 'PNG'):
                failure = True
        except Exception as exc:
            print(f'Screenshot failed: {exc}', file=sys.stderr)
            failure = True
        finally:
            window.close()
    if args.screenshot:
        QTimer.singleShot(250, capture)
    result = app.exec()
    return 1 if failure else result

if __name__ == '__main__':
    raise SystemExit(main())
