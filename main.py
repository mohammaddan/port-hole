#!/usr/bin/env python3
"""
PortHole – Docker Port Forwarder
Modern UI with PySide6, full functionality (socat proxy + SSH tunnel)
"""

import os
import socket
import subprocess
import sys
import threading
import time

import docker
import paramiko
from PySide6.QtCore import Qt, QSize, QThread, Signal, QUrl
from PySide6.QtGui import QFont, QIcon, QPixmap, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QFrame,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QLineEdit,
    QComboBox,
    QGridLayout,
    QMessageBox,
    QTextEdit,
    QDialog,
    QCheckBox,
)

from container_load_thread import ContainerLoaderThread


# ------------------------------------------------------------
# Logs streaming thread (PySide6 version)
# ------------------------------------------------------------
class LogsThread(QThread):
    line_received = Signal(str)
    finished = Signal()

    def __init__(self, docker_client, container_id, follow=True):
        super().__init__()
        self.client = docker_client
        self.container_id = container_id
        self.follow = follow
        self._stop = False

    def run(self):
        container = self.client.containers.get(self.container_id)
        try:
            logs = container.logs(stream=self.follow, tail=100, follow=self.follow)
            if self.follow:
                for line in logs:
                    if self._stop:
                        break
                    self.line_received.emit(
                        line.decode("utf-8", errors="ignore").rstrip()
                    )
            else:
                for line in logs.splitlines():
                    self.line_received.emit(
                        line.decode("utf-8", errors="ignore").rstrip()
                    )
        except Exception as e:
            self.line_received.emit(f"Error reading logs: {e}")
        self.finished.emit()

    def stop(self):
        self._stop = True
        self.wait()


class LogViewerDialog(QDialog):
    def __init__(self, parent, docker_client, container_name, container_id):
        super().__init__(parent)
        self.setWindowTitle(f"Logs: {container_name}")
        self.resize(800, 600)

        layout = QVBoxLayout()
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        font = QFont("Monospace", 10)
        self.log_text.setFont(font)
        layout.addWidget(self.log_text)

        btn_layout = QHBoxLayout()
        self.follow_cb = QCheckBox("Follow (live)")
        self.follow_cb.setChecked(True)
        btn_layout.addWidget(self.follow_cb)
        self.refresh_btn = QPushButton("Refresh (stop/restart)")
        self.close_btn = QPushButton("Close")
        btn_layout.addWidget(self.refresh_btn)
        btn_layout.addWidget(self.close_btn)
        layout.addLayout(btn_layout)

        self.setLayout(layout)

        self.docker_client = docker_client
        self.container_id = container_id
        self.logs_thread = None

        self.refresh_btn.clicked.connect(self.refresh_logs)
        self.close_btn.clicked.connect(self.accept)
        self.refresh_logs()

    def refresh_logs(self):
        if self.logs_thread:
            self.logs_thread.stop()
            self.logs_thread = None
        self.log_text.clear()
        self.logs_thread = LogsThread(
            self.docker_client, self.container_id, follow=self.follow_cb.isChecked()
        )
        self.logs_thread.line_received.connect(self._append_log)
        self.logs_thread.start()

    def _append_log(self, line):
        self.log_text.append(line)

    def closeEvent(self, event):
        if self.logs_thread:
            self.logs_thread.stop()
        event.accept()


# ------------------------------------------------------------
# Main application
# ------------------------------------------------------------
class PortHoleWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        logo_path = os.path.join(os.path.dirname(__file__), "PortHole.png")
        if os.path.exists(logo_path):
            self.setWindowIcon(QIcon(logo_path))
        self.loader_thread = None
        self.all_containers = []
        self.loading = False
        self.setWindowTitle("PortHole – Docker Port Forwarder")
        self.resize(1400, 900)

        self.setStyleSheet(
            """
            QWidget {
                background-color: #0d0f12;
                color: #f2f2f2;
                font-family: Inter, Roboto, sans-serif;
                font-size: 14px;
            }
            QMainWindow {
                background-color: #0d0f12;
            }
            QLabel {
                background: transparent;
            }
            QFrame#sidebar {
                background-color: #111418;
                border-right: 1px solid #1d232a;
            }
            QFrame#topbar {
                background-color: #0f1317;
                border-bottom: 1px solid #1d232a;
            }
            QFrame#card {
                background-color: #12161b;
                border: 1px solid #1d232a;
                border-radius: 14px;
            }
            QFrame#activeCard {
                background-color: #101419;
                border: 1px solid #1e2a22;
                border-radius: 14px;
            }
            QPushButton {
                background-color: #1a1f25;
                border: 1px solid #2a3138;
                border-radius: 6px;
                padding: 4px 9px;
                color: #f5f5f5;
            }
            QPushButton:hover {
                border: 1px solid #4CAF50;
            }
            QPushButton#greenButton {
                background-color: #4CAF50;
                color: white;
                font-weight: 600;
                border: none;
            }
            QPushButton#greenButton:hover {
                background-color: #5dc761;
            }
            QPushButton#dangerButton {
                background-color: #2a1414;
                color: #ff6b6b;
                border: 1px solid #6d2323;
            }
            QPushButton#dangerButton:hover {
                background-color: #3a1919;
            }
            QLineEdit, QComboBox {
                background-color: #181d22;
                border: 1px solid #2b333b;
                border-radius: 6px;
                padding: 4px;
                color: #f2f2f2;
                min-height: 16px;
            }
            QListWidget {
                background: transparent;
                border: none;
                outline: none;
            }
            QListWidget::item {
                background-color: #12161b;
                border: 1px solid #1d232a;
                border-radius: 8px;
                margin-bottom: 5px;
                padding: 7px;
            }
            QListWidget::item:selected {
                border: 1px solid #4CAF50;
                background-color: #151d17;
            }
        """
        )

        # Data structures (same as main.py)
        self.clients = {}  # Docker clients per context
        self.active_forwards = {}  # local_port -> forward_info
        self.contexts = self.get_docker_contexts()
        self.current_container_id = None

        # Build UI
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.sidebar = self.build_sidebar()
        self.content = self.build_content()
        root_layout.addWidget(self.sidebar)
        root_layout.addWidget(self.content)

        # Initially populate containers
        self.refresh_containers()

    # ------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------
    def build_sidebar(self):
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(360)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(18)

        # ---------- Header with fixed height ----------
        header_widget = QWidget()
        header_widget.setFixedHeight(80)
        header_widget.setStyleSheet('background-color: transparent;')
        header_layout = QHBoxLayout(header_widget)
        header_layout.setContentsMargins(0, 0, 0, 0)

        logo_path = os.path.join(os.path.dirname(__file__), "PortHole.png")
        if os.path.exists(logo_path):
            self.setWindowIcon(QIcon(logo_path))
        # Logo + title
        logo_row = QHBoxLayout()

        if os.path.exists(logo_path):
            pixmap = QPixmap(logo_path)
            pixmap = pixmap.scaled(
                64,
                64,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            logo_label = QLabel()  # Create a QLabel
            logo_label.setPixmap(pixmap)  # Set the pixmap on the label
            header_layout.addWidget(logo_label)

        # logo_row.addWidget(logo)
        title_layout = QVBoxLayout()
        title = QLabel("PortHole")
        title.setStyleSheet("font-size:32px;font-weight:700;")
        subtitle = QLabel("Docker Port Forwarder")
        subtitle.setStyleSheet("color:#9aa4af;font-size:14px;")
        title_layout.setSpacing(10)
        title_layout.addWidget(title)
        title_layout.addWidget(subtitle)
        header_layout.addLayout(title_layout)
        header_layout.addStretch()
        layout.addWidget(header_widget)

        # Search field (filter containers)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search containers...")
        self.search_input.textChanged.connect(self.filter_container_list)
        layout.addWidget(self.search_input)

        # Container list
        self.container_list = QListWidget()
        self.container_list.itemSelectionChanged.connect(self.on_container_selected)
        self.container_list.setIconSize(QSize(48, 48))
        self.container_list.setMinimumHeight(200)
        layout.addWidget(self.container_list)

        self.loading_label = QLabel("Loading containers...")
        self.loading_label.setStyleSheet("color:#ddcc00;padding:20px;font-size:24px;font-weight:600;")
        self.loading_label.setVisible(False)
        layout.addWidget(self.loading_label)

        # Footer (context info)
        footer = QFrame()
        footer.setObjectName("card")
        footer_layout = QVBoxLayout(footer)
        self.connected_label = QLabel("● Connected")
        self.connected_label.setStyleSheet(
            "color:#4CAF50;font-weight:600;font-size:16px;"
        )
        self.context_label = QLabel("No context")
        self.context_label.setStyleSheet("color:#9aa4af;line-height:22px;")
        footer_layout.addWidget(self.connected_label)
        footer_layout.addWidget(self.context_label)
        layout.addWidget(footer)

        return sidebar

    def build_content(self):
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        # Topbar
        topbar = QFrame()
        topbar.setObjectName("topbar")
        topbar_layout = QHBoxLayout(topbar)
        topbar_layout.setContentsMargins(11, 9, 11, 9)

        self.context_combo = QComboBox()
        self.context_combo.setMinimumWidth(360)
        self.context_combo.currentIndexChanged.connect(self.on_context_changed)
        topbar_layout.addWidget(self.context_combo)

        self.status_label = QLabel("● Connected")
        self.status_label.setStyleSheet("color:#4CAF50;font-size:14px;font-weight:600;")
        topbar_layout.addSpacing(12)
        topbar_layout.addWidget(self.status_label)
        topbar_layout.addStretch()

        self.refresh_containers_btn = QPushButton("Refresh")
        self.refresh_containers_btn.clicked.connect(self.refresh_containers)
        topbar_layout.addWidget(self.refresh_containers_btn)
        settings_btn = QPushButton("⚙")
        settings_btn.setFixedWidth(52)
        topbar_layout.addWidget(settings_btn)

        layout.addWidget(topbar)

        # Summary card (container details)
        self.summary_card = QFrame()
        self.summary_card.setObjectName("card")
        self.summary_card_layout = QHBoxLayout(self.summary_card)
        self.summary_card_layout.setContentsMargins(14, 14, 14, 14)

        left = QVBoxLayout()
        left.setSpacing(10)
        self.summary_title = QLabel("Select a container")
        self.summary_title.setStyleSheet("font-size:24px;font-weight:700;")
        self.summary_id = QLabel("")
        self.summary_id.setStyleSheet("color:#93a0ab;font-size:16px;")
        self.summary_meta = QLabel("")
        self.summary_meta.setStyleSheet("color:#9aa4af;font-size:16px;")
        self.summary_ports = QLabel("")
        self.summary_ports.setStyleSheet(
            "color:#4CAF50;font-size:15px;font-weight:600;"
        )
        left.addWidget(self.summary_title)
        left.addWidget(self.summary_id)
        left.addSpacing(4)
        left.addWidget(self.summary_meta)
        left.addWidget(self.summary_ports)

        actions = QVBoxLayout()
        actions.setSpacing(4)
        logs_btn = QPushButton("View Logs")
        logs_btn.clicked.connect(self.view_logs)
        terminal_btn = QPushButton("Open Terminal")
        terminal_btn.clicked.connect(self.open_terminal)
        actions.addWidget(logs_btn)
        actions.addWidget(terminal_btn)
        actions.addStretch()

        self.summary_card_layout.addLayout(left)
        self.summary_card_layout.addStretch()
        self.summary_card_layout.addLayout(actions)
        layout.addWidget(self.summary_card)

        # Port forwarding card
        forward_card = QFrame()
        forward_card.setObjectName("card")
        forward_layout = QVBoxLayout(forward_card)
        forward_layout.setContentsMargins(14, 14, 14, 14)
        forward_layout.setSpacing(24)

        forward_title = QLabel("PORT FORWARDING (without publishing)")
        forward_title.setStyleSheet("font-size:18px;font-weight:700;")

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)

        internal_label = QLabel("Container internal port")
        local_label = QLabel("Local port on your machine")
        host_label = QLabel("Bind address")

        self.internal_port_combo = QComboBox()
        self.internal_port_combo.setEditable(True)
        self.internal_port_combo.setPlaceholderText("Select or type port")

        self.local_port_input = QLineEdit()
        self.local_port_input.setPlaceholderText("e.g. 6380")

        self.bind_host_combo = QComboBox()
        self.bind_host_combo.addItems(["localhost", "127.0.0.1"])

        grid.addWidget(internal_label, 0, 0)
        grid.addWidget(local_label, 0, 1)
        grid.addWidget(host_label, 0, 2)
        grid.addWidget(self.internal_port_combo, 1, 0)
        grid.addWidget(self.local_port_input, 1, 1)
        grid.addWidget(self.bind_host_combo, 1, 2)

        # Advanced options (collapsible – we keep it simple)
        self.advanced_btn = QPushButton(
            "Advanced options (remote host port, auto reconnect...)"
        )
        self.advanced_btn.setCheckable(True)
        self.advanced_widget = QWidget()
        self.advanced_widget.setVisible(False)
        adv_layout = QHBoxLayout(self.advanced_widget)
        adv_layout.addWidget(QLabel("Remote host port:"))
        self.remote_port_input = QLineEdit()
        self.remote_port_input.setPlaceholderText("random")
        adv_layout.addWidget(self.remote_port_input)
        self.advanced_btn.toggled.connect(self.advanced_widget.setVisible)

        start_btn = QPushButton("Start Tunnel")
        start_btn.setObjectName("greenButton")
        start_btn.setMinimumHeight(36)
        start_btn.setFont(QFont("Inter", 14, QFont.Weight.Bold))
        start_btn.clicked.connect(self.start_port_forward)

        forward_layout.addWidget(forward_title)
        forward_layout.addLayout(grid)
        forward_layout.addWidget(self.advanced_btn)
        forward_layout.addWidget(self.advanced_widget)
        forward_layout.addWidget(start_btn)

        layout.addWidget(forward_card)

        # Active tunnels card
        self.active_card = QFrame()
        self.active_card.setObjectName("activeCard")
        active_layout = QVBoxLayout(self.active_card)
        active_layout.setContentsMargins(14, 14, 14, 14)
        active_layout.setSpacing(14)

        header = QHBoxLayout()
        active_title = QLabel("ACTIVE TUNNELS")
        active_title.setStyleSheet("font-size:22px;font-weight:700;")
        self.active_badge = QLabel("0 active")
        self.active_badge.setStyleSheet(
            "background-color:#18311d; color:#6fe273; border-radius:10px; padding:6px 12px; font-weight:600;"
        )
        header.addWidget(active_title)
        header.addStretch()
        header.addWidget(self.active_badge)

        self.tunnels_list = (
            QListWidget()
        )  # Will hold custom widgets for each active tunnel
        active_layout.addLayout(header)
        active_layout.addWidget(self.tunnels_list)

        layout.addWidget(self.active_card)

        return wrapper

    # ------------------------------------------------------------
    # Docker backend (copied/adapted from main.py)
    # ------------------------------------------------------------
    def get_docker_contexts(self):
        contexts = {}
        try:
            result = subprocess.run(
                [
                    "docker",
                    "context",
                    "ls",
                    "--format",
                    "{{.Name}} {{.DockerEndpoint}}",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            for line in result.stdout.strip().splitlines():
                parts = line.split(maxsplit=1)
                name = parts[0]
                endpoint = parts[1] if len(parts) > 1 else "unknown"
                contexts[name] = endpoint
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to get Docker contexts:\n{e}")
        return contexts

    def get_docker_client_and_ssh_info(self, context_name):
        endpoint = self.contexts.get(context_name)
        if not endpoint:
            raise ValueError(f"Context {context_name} not found")
        if endpoint.startswith("ssh://"):
            rest = endpoint[6:]
            if "@" in rest:
                ssh_user, ssh_host = rest.split("@", 1)
            else:
                ssh_user = None
                ssh_host = rest
            if "/" in ssh_host:
                ssh_host = ssh_host.split("/")[0]
            docker_client = docker.DockerClient(base_url=endpoint)
            return docker_client, ssh_host, ssh_user
        else:
            raise ValueError(f"Only SSH contexts are supported (got {endpoint})")

    def refresh_containers(self):
        # Update context combo box
        self.context_combo.clear()
        for name, endpoint in self.contexts.items():
            self.context_combo.addItem(f"{name} ({endpoint})", name)
        if self.context_combo.count() > 0:
            self.context_combo.setCurrentIndex(0)
            self.on_context_changed()

    def on_context_changed(self):
        context_name = self.context_combo.currentData()
        if not context_name:
            return
        # Update sidebar footer
        endpoint = self.contexts.get(context_name, "unknown")
        self.context_label.setText(f"{context_name}\n{endpoint}")
        self.connected_label.setText("● Connected")
        self.status_label.setText("● Connected")

        # Load containers
        self.load_container_list()

    def load_container_list(self):
        if self.loading:
            return  # already loading
        context_name = self.context_combo.currentData()
        if not context_name:
            return
        try:
            docker_client, _, _ = self.get_docker_client_and_ssh_info(context_name)
            self.clients[context_name] = docker_client

            # Stop previous thread if it's still running
            if self.loader_thread and self.loader_thread.isRunning():
                self.loader_thread.stop()
                self.loader_thread.quit()
                self.loader_thread.wait(1000)
                self.loader_thread.deleteLater()
                self.loader_thread = None

            # Show loading indicator
            self.loading = True
            self.container_list.clear()
            self.loading_label.setVisible(True)
            self.container_list.setVisible(False)
            self.search_input.setVisible(False)
            self.search_input.setEnabled(False)
            self.context_combo.setEnabled(False)
            self.refresh_containers_btn.setEnabled(False)

            # Start background loading
            self.loader_thread = ContainerLoaderThread(docker_client)
            self.loader_thread.containers_loaded.connect(self.on_containers_loaded)
            self.loader_thread.error_occurred.connect(self.on_container_load_error)
            self.loader_thread.start()
        except Exception as e:
            self.loading = False
            QMessageBox.critical(self, "Error", f"Failed to list containers:\n{e}")
            self.loading_label.setVisible(False)
            self.container_list.setVisible(True)
            self.search_input.setEnabled(True)
            self.context_combo.setEnabled(True)
            self.refresh_containers_btn.setEnabled(True)

    def on_containers_loaded(self, containers):
        self.all_containers = containers
        self.container_list.clear()
        for display, cid, container,logo in containers:
            item = QListWidgetItem(display)
            if logo:
                item.setIcon(QIcon(logo))
            self.container_list.addItem(item)
        self.filter_container_list()
        self.loading_label.setVisible(False)
        self.container_list.setVisible(True)
        self.search_input.setEnabled(True)
        self.search_input.setVisible(True)
        self.context_combo.setEnabled(True)
        if hasattr(self, 'refresh_containers_btn'):
            self.refresh_containers_btn.setEnabled(True)
        self.loading = False
        if self.container_list.count() > 0:
            self.container_list.setCurrentRow(0)

    def on_container_load_error(self, error_msg):
        self.loading_label.setVisible(False)
        self.container_list.setVisible(True)
        self.search_input.setEnabled(True)
        self.context_combo.setEnabled(True)
        if hasattr(self, 'refresh_containers_btn'):
            self.refresh_containers_btn.setEnabled(True)
        self.loading = False
        QMessageBox.critical(self, "Error", f"Failed to load containers:\n{error_msg}")

    def closeEvent(self, event):
        if self.loader_thread and self.loader_thread.isRunning():
            self.loader_thread.quit()
            self.loader_thread.wait(2000)
        event.accept()

    def filter_container_list(self):
        filter_text = self.search_input.text().lower()
        self.container_list.clear()
        for display, cid, container,logo in self.all_containers:
            if filter_text in display.lower():
                item = QListWidgetItem(display)
                if logo:
                    item.setIcon(QIcon(logo))
                self.container_list.addItem(item)

        if self.container_list.count() > 0:
            self.container_list.setCurrentRow(0)

    def on_container_selected(self):
        current = self.container_list.currentItem()
        if not current:
            return
        display = current.text()
        # Find container id from stored list
        for d, cid, container,logo in self.all_containers:
            if d == display:
                self.current_container_id = cid
                self.update_container_details(container)
                break

    def update_container_details(self, container):
        """Update the summary card with container info and populate internal ports."""
        attrs = container.attrs
        name = container.name
        image = attrs["Config"]["Image"]
        status = container.status
        started_at = attrs["State"]["StartedAt"]
        created_at = attrs["Created"][:19]

        # Detect internal listening ports (using docker exec ss)
        ports_list = self.get_container_listening_ports(container.id)
        if not ports_list:
            # fallback to exposed ports
            exposed = attrs["Config"].get("ExposedPorts", {})
            ports_list = (
                [port.split("/")[0] for port in exposed.keys()] if exposed else []
            )

        self.summary_title.setText(name)
        self.summary_id.setText(container.short_id)
        self.summary_meta.setText(
            f"•Image: {image.split('@')[0]}\n•Status: {status}\n•Uptime: {started_at[:19]}"
        )
        self.summary_ports.setText(f"•Internal Ports:   {', '.join(ports_list)}")

        # Populate internal port combo
        self.internal_port_combo.clear()
        for p in ports_list:
            self.internal_port_combo.addItem(p, int(p))
        if ports_list:
            self.internal_port_combo.setCurrentIndex(0)

    def get_container_listening_ports(self, container_id):
        """Run ss inside container to find listening ports."""
        try:
            # Use docker exec (requires docker client on remote host)
            # We'll use the docker API via the client
            client = self.get_docker_client_for_current_context()
            container = client.containers.get(container_id)
            attrs = container.attrs
            exposed_ports = attrs["Config"].get("ExposedPorts", {})
            ports_list = (
                [port.split("/")[0] for port in exposed_ports.keys()]
                if exposed_ports
                else ["No exposed ports"]
            )
            return ports_list
        except Exception as e:
            print(f"Could not detect listening ports: {e}")
            return []

    def get_docker_client_for_current_context(self):
        context_name = self.context_combo.currentData()
        if context_name in self.clients:
            return self.clients[context_name]
        else:
            client, _, _ = self.get_docker_client_and_ssh_info(context_name)
            self.clients[context_name] = client
            return client

    # ------------------------------------------------------------
    # Logs and terminal
    # ------------------------------------------------------------
    def view_logs(self):
        if not self.current_container_id:
            QMessageBox.warning(self, "No container", "Please select a container.")
            return
        try:
            docker_client = self.get_docker_client_for_current_context()
            container = docker_client.containers.get(self.current_container_id)
            dialog = LogViewerDialog(self, docker_client, container.name, container.id)
            dialog.exec()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not retrieve logs:\n{str(e)}")

    def open_terminal(self):
        if not self.current_container_id:
            QMessageBox.warning(self, "No container", "Please select a container.")
            return
        context_name = self.context_combo.currentData()
        try:
            _, ssh_host, ssh_user = self.get_docker_client_and_ssh_info(context_name)
            if not ssh_user:
                ssh_user = "root"
            container_name = (
                self.get_docker_client_for_current_context()
                .containers.get(self.current_container_id)
                .name
            )
            ssh_cmd = (
                f"ssh {ssh_user}@{ssh_host} -t 'docker exec -it {container_name} sh'"
            )
            terminals = [
                ("gnome-terminal", ["--", "bash", "-c", ssh_cmd]),
                ("konsole", ["-e", "bash", "-c", ssh_cmd]),
                ("xfce4-terminal", ["-e", "bash", "-c", ssh_cmd]),
                ("xterm", ["-e", "bash", "-c", ssh_cmd]),
                ("urxvt", ["-e", "bash", "-c", ssh_cmd]),
            ]
            term_prog = None
            term_args = None
            for prog, args in terminals:
                if subprocess.run(["which", prog], capture_output=True).returncode == 0:
                    term_prog = prog
                    term_args = args
                    break
            if term_prog:
                subprocess.Popen([term_prog] + term_args, start_new_session=True)
            else:
                subprocess.Popen(
                    ["xterm", "-e", "bash", "-c", ssh_cmd], start_new_session=True
                )
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to open terminal:\n{str(e)}")

    # ------------------------------------------------------------
    # Port forward core (copied from main.py)
    # ------------------------------------------------------------
    def create_socat_proxy(
        self, docker_client, target_ip, target_port, network_name, remote_port=None
    ):
        if remote_port is None:
            port_config = {f"{target_port}/tcp": None}
        else:
            port_config = {f"{target_port}/tcp": remote_port}

        proxy_name = f"proxy_{target_port}_{int(time.time())}"
        existing = docker_client.containers.list(all=True, filters={"name": proxy_name})
        for c in existing:
            c.remove(force=True)

        proxy = docker_client.containers.run(
            image="alpine/socat",
            name=proxy_name,
            network="bridge",
            ports=port_config,
            command=f"TCP-LISTEN:{target_port},fork,reuseaddr TCP:{target_ip}:{target_port}",
            detach=True,
            remove=False,
        )
        docker_client.networks.get(network_name).connect(proxy.id)
        time.sleep(2)
        proxy.reload()
        if proxy.status != "running":
            logs = proxy.logs().decode("utf-8", errors="ignore")
            raise RuntimeError(f"socat container failed to start:\n{logs}")

        if remote_port is not None:
            published_port = remote_port
        else:
            port_bindings = proxy.attrs.get("HostConfig", {}).get("PortBindings", {})
            key = f"{target_port}/tcp"
            if key in port_bindings and port_bindings[key]:
                published_port = int(port_bindings[key][0]["HostPort"])
            else:
                ports = proxy.attrs.get("NetworkSettings", {}).get("Ports", {})
                if key in ports and ports[key]:
                    published_port = int(ports[key][0]["HostPort"])
                else:
                    raise RuntimeError(
                        "Could not determine published port for socat container"
                    )
        return proxy, published_port

    class SSHTunnel:
        def __init__(self, ssh_client, local_port, remote_host, remote_port):
            self.ssh_client = ssh_client
            self.local_port = local_port
            self.remote_host = remote_host
            self.remote_port = remote_port
            self.server_socket = None
            self._stop_event = threading.Event()
            self._thread = None
            self.all_containers = []

        def start(self):
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_socket.bind(("127.0.0.1", self.local_port))
            self.server_socket.listen(5)
            self._thread = threading.Thread(target=self._accept_loop, daemon=True)
            self._thread.start()

        def _accept_loop(self):
            while not self._stop_event.is_set():
                try:
                    client_sock, addr = self.server_socket.accept()
                    threading.Thread(
                        target=self._handle_client, args=(client_sock,), daemon=True
                    ).start()
                except socket.error:
                    break

        def _handle_client(self, client_sock):
            try:
                transport = self.ssh_client.get_transport()
                if not transport:
                    return
                chan = transport.open_channel(
                    "direct-tcpip",
                    (self.remote_host, self.remote_port),
                    client_sock.getpeername(),
                )
                if chan is None:
                    client_sock.close()
                    return

                def forward(src, dst):
                    while not self._stop_event.is_set():
                        try:
                            data = src.recv(4096)
                            if not data:
                                break
                            dst.send(data)
                        except (socket.error, EOFError):
                            break
                    src.close()
                    dst.close()

                threading.Thread(target=forward, args=(client_sock, chan)).start()
                threading.Thread(target=forward, args=(chan, client_sock)).start()
            except Exception as e:
                client_sock.close()

        def stop(self):
            self._stop_event.set()
            if self.server_socket:
                self.server_socket.close()
            if self._thread:
                self._thread.join(timeout=1)

    def start_port_forward(self):
        context_name = self.context_combo.currentData()
        if not context_name:
            QMessageBox.warning(self, "No context", "Select a Docker context.")
            return
        if not self.current_container_id:
            QMessageBox.warning(self, "No container", "Select a running container.")
            return

        # Get target port
        target_port_str = self.internal_port_combo.currentText().strip()
        if not target_port_str.isdigit():
            QMessageBox.warning(
                self, "Invalid port", "Container internal port must be a number."
            )
            return
        target_port = int(target_port_str)

        local_port_str = self.local_port_input.text().strip()
        if not local_port_str.isdigit():
            QMessageBox.warning(self, "Invalid port", "Local port must be a number.")
            return
        local_port = int(local_port_str)

        import random

        remote_port_str = self.remote_port_input.text().strip()
        remote_port = int(remote_port_str) if remote_port_str.isdigit() else random.randint(16000, 32000)

        if local_port in self.active_forwards:
            QMessageBox.warning(
                self, "Port busy", f"Local port {local_port} already forwarded."
            )
            return

        try:
            docker_client, ssh_host, ssh_user = self.get_docker_client_and_ssh_info(
                context_name
            )
            if not ssh_user:
                ssh_user = "root"

            container = docker_client.containers.get(self.current_container_id)
            networks = container.attrs["NetworkSettings"]["Networks"]
            if not networks:
                QMessageBox.warning(
                    self,
                    "No network",
                    "Container has no networks. Attach to a user-defined network.",
                )
                return
            network_names = list(networks.keys())
            network_name = next(
                (n for n in network_names if n != "bridge"), network_names[0]
            )
            target_ip = networks[network_name]["IPAddress"]
            if not target_ip:
                raise ValueError(f"Container has no IP on network {network_name}")

            proxy_container, published_port = self.create_socat_proxy(
                docker_client, target_ip, target_port, network_name, remote_port
            )

            ssh_client = paramiko.SSHClient()
            ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh_client.connect(
                ssh_host, username=ssh_user, look_for_keys=True, allow_agent=True
            )

            tunnel = self.SSHTunnel(ssh_client, local_port, "localhost", published_port)
            tunnel.start()

            forward_info = {
                "proxy": proxy_container,
                "ssh": ssh_client,
                "tunnel": tunnel,
                "local_port": local_port,
                "context": context_name,
                "container": container.name,
                "target_port": target_port,
            }
            self.active_forwards[local_port] = forward_info
            self.add_active_tunnel_item(
                local_port, container.name, target_port, context_name
            )
            QMessageBox.information(
                self,
                "Success",
                f"Forward established!\nlocalhost:{local_port} → {container.name}:{target_port}",
            )
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to start forward:\n{str(e)}")

    def add_active_tunnel_item(self, local_port, container_name, target_port, context_name):
        """Add an active tunnel entry to the list with Stop and Open buttons."""
        item = QListWidgetItem(self.tunnels_list)
        item.setSizeHint(QSize(600, 120))
        widget = QFrame()
        widget.setObjectName("card")
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(4, 4, 4, 4)

        info = QVBoxLayout()
        info.setSpacing(4)
        endpoint = QLabel(f"localhost:{local_port}  →  {container_name}:{target_port}")
        endpoint.setStyleSheet("font-size:18px;font-weight:700;")
        stats = QLabel(f"Context: {context_name}     •     Active")
        stats.setStyleSheet("color:#9aa4af;font-size:15px;")
        info.addWidget(endpoint)
        info.addWidget(stats)

        buttons = QVBoxLayout()
        buttons.setSpacing(4)

        # Stop button
        stop_btn = QPushButton("Stop Tunnel")
        stop_btn.setObjectName("dangerButton")
        stop_btn.clicked.connect(lambda checked, lp=local_port: self.stop_forward(lp))
        buttons.addWidget(stop_btn)

        # Open in browser button (only for HTTP services – you can add condition)
        open_btn = QPushButton("Open in Browser")
        open_btn.setObjectName("greenButton")
        open_btn.clicked.connect(lambda checked, lp=local_port: self.open_browser(lp))
        buttons.addWidget(open_btn)

        layout.addLayout(info)
        layout.addStretch()
        layout.addLayout(buttons)

        self.tunnels_list.addItem(item)
        self.tunnels_list.setItemWidget(item, widget)
        self.active_badge.setText(f"{len(self.active_forwards)} active")

    def open_browser(self, local_port):
        """Open the default web browser at http://localhost:<port>"""
        url = QUrl(f"http://localhost:{local_port}")
        if not QDesktopServices.openUrl(url):
            QMessageBox.warning(self, "Browser Error", f"Could not open browser for {url.toString()}")

    def stop_forward(self, local_port):
        if local_port not in self.active_forwards:
            return
        info = self.active_forwards.pop(local_port)
        info["tunnel"].stop()
        info["ssh"].close()
        try:
            info["proxy"].remove(force=True)
        except:
            pass
        # Remove from list widget
        for i in range(self.tunnels_list.count()):
            item = self.tunnels_list.item(i)
            widget = self.tunnels_list.itemWidget(item)
            if widget:
                # Find the label with the port
                for child in widget.findChildren(QLabel):
                    if f"localhost:{local_port}" in child.text():
                        self.tunnels_list.takeItem(i)
                        break
        self.active_badge.setText(f"{len(self.active_forwards)} active")
        QMessageBox.information(
            self, "Stopped", f"Forward on port {local_port} stopped."
        )


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = PortHoleWindow()
    window.show()
    sys.exit(app.exec())
