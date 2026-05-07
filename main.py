#!/usr/bin/env python3
"""
Docker Internal Port Forwarder – Elegant GUI with searchable container list,
logs viewer, terminal access, and unpublished port forwarding.
"""

import sys
import threading
import socket
import subprocess
import json
import time
import paramiko
import docker
import os
from PyQt6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QComboBox,
    QLineEdit,
    QMessageBox,
    QListWidget,
    QListWidgetItem,
    QTextEdit,
    QDialog,
    QCheckBox,
    QGroupBox,
    QCompleter,
    QProgressBar,
)
from PyQt6.QtCore import QThread, pyqtSignal, Qt, QStringListModel
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtGui import QFont, QIcon


# ---------- Logs streaming thread ----------
class LogsThread(QThread):
    line_received = pyqtSignal(str)
    finished = pyqtSignal()

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


# ---------- Main application ----------
class DockerPortForwarder(QWidget):

    def __init__(self):
        super().__init__()
        logo_path = os.path.join(os.path.dirname(__file__), "PortHole.png")
        if os.path.exists(logo_path):
            self.setWindowIcon(QIcon(logo_path))
        self.setWindowTitle("PortHole (Remote Docker Unpublished Port Forwarder)")
        self.setGeometry(200, 200, 800, 600)

        # Apply a clean stylesheet
        self.setStyleSheet(
            """
            QGroupBox {
                font-weight: bold;
                border: 1px solid #ccc;
                border-radius: 5px;
                margin-top: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px 0 5px;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                padding: 6px 12px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
            QPushButton:disabled {
                background-color: #cccccc;
            }
            QLineEdit, QComboBox {
                padding: 4px;
                border-radius: 3px;
                border: 1px solid #aaa;
                background-color: #eee;
            }
            QListWidget::item {
                padding: 5px;
            }
        """
        )

        self.clients = {}  # Docker clients per context
        self.active_forwards = {}  # local_port -> forward_info
        self.contexts = self.get_docker_contexts()
        self.main_layout = QVBoxLayout()
        self.main_layout.setSpacing(15)
        self.main_layout.setContentsMargins(15, 15, 15, 15)

        # ---------- Header with logo and title ----------
        header_layout = QHBoxLayout()
        # Logo (SVG)
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
            header_layout.addWidget(logo_label)  # Add the label, not the pixmap
        # Title text
        title_label = QLabel(
            "<h1>PortHole</h1><i>Forward unpublished container ports</i>"
        )
        header_layout.addWidget(title_label)
        header_layout.addStretch()
        self.main_layout.addLayout(header_layout)

        # ---------- Connection group ----------
        conn_group = QGroupBox("Docker Connection")
        conn_layout = QVBoxLayout()
        context_layout = QHBoxLayout()
        context_layout.addWidget(QLabel("Context:"))
        self.env_combo = QComboBox()
        for name, endpoint in self.contexts.items():
            self.env_combo.addItem(f"{name} ({endpoint})", name)
        self.env_combo.currentIndexChanged.connect(self.refresh_containers)
        context_layout.addWidget(self.env_combo)
        context_layout.addStretch()
        self.refresh_containers_btn = QPushButton("🔄 Refresh Containers")
        self.refresh_containers_btn.clicked.connect(self.refresh_containers)
        context_layout.addWidget(self.refresh_containers_btn)
        conn_layout.addLayout(context_layout)
        conn_group.setLayout(conn_layout)
        self.main_layout.addWidget(conn_group)

        # ---------- Container selection + actions ----------
        container_group = QGroupBox("Container Operations")
        container_layout = QVBoxLayout()

        # Searchable container combo
        container_select_layout = QHBoxLayout()
        container_select_layout.addWidget(QLabel("Container:"))
        self.container_combo = QComboBox()
        self.container_combo.setEditable(True)
        self.container_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.container_combo.setMinimumWidth(350)
        container_select_layout.addWidget(self.container_combo)
        container_select_layout.addStretch()
        container_layout.addLayout(container_select_layout)

        # Action buttons
        action_buttons_layout = QHBoxLayout()
        self.logs_btn = QPushButton("📄 View Logs")
        self.logs_btn.clicked.connect(self.view_logs)
        self.terminal_btn = QPushButton("💻 Open Terminal")
        self.terminal_btn.clicked.connect(self.open_terminal)
        action_buttons_layout.addWidget(self.logs_btn)
        action_buttons_layout.addWidget(self.terminal_btn)
        action_buttons_layout.addStretch()
        container_layout.addLayout(action_buttons_layout)
        container_group.setLayout(container_layout)
        self.main_layout.addWidget(container_group)

        # ---------- Port forwarding group ----------
        forward_group = QGroupBox("Port Forwarding (without publishing)")
        forward_layout = QVBoxLayout()

        # Internal port
        internal_layout = QHBoxLayout()
        internal_layout.addWidget(QLabel("Container internal port:"))
        self.target_port_input = QLineEdit()
        self.target_port_input.setPlaceholderText("e.g. 6379")
        internal_layout.addWidget(self.target_port_input)
        forward_layout.addLayout(internal_layout)

        # Local port
        local_layout = QHBoxLayout()
        local_layout.addWidget(QLabel("Local port on your machine:"))
        self.local_port_input = QLineEdit()
        self.local_port_input.setPlaceholderText("e.g. 6380")
        local_layout.addWidget(self.local_port_input)
        forward_layout.addLayout(local_layout)

        # Remote port (optional)
        # remote_layout = QHBoxLayout()
        # remote_layout.addWidget(
        #     QLabel("Optional remote host port (leave empty for random):")
        # )
        # self.remote_port_input = QLineEdit()
        # self.remote_port_input.setPlaceholderText("e.g. 16379")
        # remote_layout.addWidget(self.remote_port_input)
        # forward_layout.addLayout(remote_layout)

        # Forward button
        self.forward_button = QPushButton("🚀 Start Port Forward")
        self.forward_button.clicked.connect(self.start_port_forward)
        forward_layout.addWidget(self.forward_button)

        forward_group.setLayout(forward_layout)
        self.main_layout.addWidget(forward_group)

        # ---------- Active forwards list ----------
        active_group = QGroupBox("Active Forwards")
        active_layout = QVBoxLayout()
        self.active_list = QListWidget()
        active_layout.addWidget(self.active_list)
        active_group.setLayout(active_layout)
        self.main_layout.addWidget(active_group)

        self.setLayout(self.main_layout)

        # Initially populate containers
        self.refresh_containers()

        # Setup searchable combo completer
        self.setup_container_completer()

    def setup_container_completer(self):
        """Add a completer to the container combo for search as you type."""
        completer = QCompleter()
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.container_combo.setCompleter(completer)
        # Update completer model whenever the combo items change
        self.container_combo.model().rowsInserted.connect(self.update_completer_model)
        self.container_combo.model().rowsRemoved.connect(self.update_completer_model)
        self.update_completer_model()

    def update_completer_model(self, *args):
        """Update the completer with current container names."""
        items = [
            self.container_combo.itemText(i)
            for i in range(self.container_combo.count())
        ]
        model = QStringListModel(items)
        self.container_combo.completer().setModel(model)

    # ---------- Docker context helpers ----------
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
        self.container_combo.clear()
        context_name = self.env_combo.currentData()
        if not context_name:
            return
        try:
            docker_client, _, _ = self.get_docker_client_and_ssh_info(context_name)
            containers = docker_client.containers.list(filters={"status": "running"})
            for c in containers:
                self.container_combo.addItem(f"{c.name} ({c.short_id})", c.id)
            # Cache the client for later use
            self.clients[context_name] = docker_client
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to list containers:\n{e}")

    # ---------- Logs and terminal ----------
    def view_logs(self):
        container_id = self.container_combo.currentData()
        if not container_id:
            QMessageBox.warning(
                self, "No container", "Please select a running container."
            )
            return
        context_name = self.env_combo.currentData()
        try:
            docker_client, _, _ = self.get_docker_client_and_ssh_info(context_name)
            container = docker_client.containers.get(container_id)
            dialog = LogViewerDialog(self, docker_client, container.name, container.id)
            dialog.exec()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not retrieve logs:\n{str(e)}")

    def open_terminal(self):
        container_id = self.container_combo.currentData()
        if not container_id:
            QMessageBox.warning(
                self, "No container", "Please select a running container."
            )
            return
        context_name = self.env_combo.currentData()
        try:
            docker_client, ssh_host, ssh_user = self.get_docker_client_and_ssh_info(
                context_name
            )
            if not ssh_user:
                ssh_user = "root"
            container = docker_client.containers.get(container_id)
            container_name = container.name

            ssh_cmd = (
                f"ssh {ssh_user}@{ssh_host} -t 'docker exec -it {container_name} sh'"
            )
            import shlex

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

    # ---------- Port forward core (unchanged, robust) ----------
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
        context_name = self.env_combo.currentData()
        if not context_name:
            QMessageBox.warning(self, "No context", "Select a Docker context.")
            return
        container_id = self.container_combo.currentData()
        if not container_id:
            QMessageBox.warning(self, "No container", "Select a running container.")
            return
        target_port_str = self.target_port_input.text().strip()
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

        remote_port_str = str(random.randint(16000, 32000))
        # remote_port_str = self.remote_port_input.text().strip()
        remote_port = int(remote_port_str) if remote_port_str.isdigit() else None

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

            container = docker_client.containers.get(container_id)
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
            self._add_active_forward_item(
                local_port, container.name, target_port, context_name
            )
            QMessageBox.information(
                self,
                "Success",
                f"Forward established!\nlocalhost:{local_port} → {container.name}:{target_port}",
            )
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to start forward:\n{str(e)}")

    def _add_active_forward_item(
        self, local_port, container_name, target_port, context_name
    ):
        item_widget = QWidget()
        item_layout = QHBoxLayout()
        item_layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel(
            f"localhost:{local_port} → {container_name}:{target_port} (context: {context_name})"
        )
        stop_btn = QPushButton("Stop")
        stop_btn.clicked.connect(lambda checked, lp=local_port: self.stop_forward(lp))
        item_layout.addWidget(label)
        item_layout.addWidget(stop_btn)
        item_widget.setLayout(item_layout)

        list_item = QListWidgetItem(self.active_list)
        list_item.setSizeHint(item_widget.sizeHint())
        self.active_list.addItem(list_item)
        self.active_list.setItemWidget(list_item, item_widget)

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
        for i in range(self.active_list.count()):
            item = self.active_list.item(i)
            widget = self.active_list.itemWidget(item)
            if widget and widget.layout():
                label = widget.layout().itemAt(0).widget().text()
                if f"localhost:{local_port}" in label:
                    self.active_list.takeItem(i)
                    break
        QMessageBox.information(
            self, "Stopped", f"Forward on port {local_port} stopped."
        )


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = DockerPortForwarder()
    window.show()
    sys.exit(app.exec())
