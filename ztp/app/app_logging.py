"""Device-oriented application events and quiet successful polling logs."""

import json
import logging
import re
import time


logger = logging.getLogger("ztp")


def log_event(level, event, *, device=None, attempt=None, **fields):
    values = {"device": device, "attempt": attempt, "event": event, **fields}
    parts = []
    for key, value in values.items():
        text = "-" if value is None else str(value)
        if not re.fullmatch(r"[A-Za-z0-9_./:@+\-]+", text):
            text = json.dumps(text, ensure_ascii=True)
        parts.append(f"{key}={text}")
    logger.log(level, " ".join(parts))


class UTCFormatter(logging.Formatter):
    converter = time.gmtime

    def format(self, record):
        return super().format(record).replace("\r", r"\r").replace("\n", r"\n")


class QuietPollingFilter(logging.Filter):
    def filter(self, record):
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5:
            return True
        _, method, target, _, status = args
        if method != "GET" or not isinstance(status, int) or not 200 <= status < 300:
            return True
        path = target.split("?", 1)[0]
        return not (path == "/healthz" or path == "/api/v1/provisioning"
                    or path.startswith("/api/v1/provisioning/"))
