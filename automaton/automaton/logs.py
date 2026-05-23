"""Logging setup.

CLI commands' user-facing output stays on stdout via print(). Daemon-loop
chatter (worker leases, scheduler ticks, plugin loads, HTTP requests) goes
through stdlib logging, so operators can tail/grep/redirect/rotate it.

Env vars:
  AUTOMATON_LOG_LEVEL   (default: INFO)         - root level
  AUTOMATON_LOG_FILE    (default: None)         - path to log file (also keeps stderr)
  AUTOMATON_LOG_FORMAT  (default: 'text')       - 'text' or 'json'
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import time


class _JsonFormatter(logging.Formatter):
    def format(self, record):
        out = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out)


def setup(level: str | None = None, log_file: str | None = None,
          fmt: str | None = None) -> None:
    """Configure the root logger. Safe to call multiple times - subsequent
    calls replace the handlers."""
    level = (level or os.environ.get("AUTOMATON_LOG_LEVEL") or "INFO").upper()
    log_file = log_file or os.environ.get("AUTOMATON_LOG_FILE")
    fmt = (fmt or os.environ.get("AUTOMATON_LOG_FORMAT") or "text").lower()

    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)

    if fmt == "json":
        formatter = _JsonFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(formatter)
    root.addHandler(stderr)

    if log_file:
        fh = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=5
        )
        fh.setFormatter(formatter)
        root.addHandler(fh)
