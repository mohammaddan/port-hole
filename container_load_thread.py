import os

from PySide6.QtCore import QThread, Signal


class ContainerLoaderThread(QThread):
    containers_loaded = Signal(list)
    error_occurred = Signal(str)

    def __init__(self, docker_client):
        super().__init__()
        self.docker_client = docker_client
        self._stop = False

    def run(self):
        try:
            containers = self.docker_client.containers.list(filters={"status": "running"})
            icon_path = os.path.join(os.path.dirname(__file__), "icons/")
            if self._stop:
                return
            result = []
            for c in containers:
                if self._stop:
                    return
                logo=self.logo_mapping(c.attrs['Config']['Image'])
                display = f"{c.name}\n{c.short_id}"
                result.append((display, c.id, c, logo))
            if not self._stop:
                self.containers_loaded.emit(result)
        except Exception as e:
            if not self._stop:
                self.error_occurred.emit(str(e))

    def stop(self):
        self._stop = True

    @staticmethod
    def logo_mapping(image_name):
        logos = os.listdir(os.path.join(os.path.dirname(__file__), "icons/"))
        dir_name=os.path.join(os.path.dirname(__file__), "icons/")
        for logo in logos:
            if logo.replace(dir_name,"").replace('.png','') in image_name:
                return dir_name+ logo
        return dir_name+'docker.png'