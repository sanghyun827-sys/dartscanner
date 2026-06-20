import logging
import threading
from collections import deque
from datetime import datetime

_records: deque = deque(maxlen=500)
_lock = threading.Lock()


class _MemHandler(logging.Handler):
    def emit(self, record: logging.LogRecord):
        with _lock:
            _records.append({
                "time": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
                "level": record.levelname,
                "name": record.name.split(".")[-1],
                "message": self.format(record),
            })


_handler = _MemHandler()
_handler.setFormatter(logging.Formatter("%(message)s"))


def setup():
    root = logging.getLogger()
    root.addHandler(_handler)


def get_logs() -> list:
    with _lock:
        return list(_records)
