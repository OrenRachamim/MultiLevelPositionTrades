"""Back-test and history-download jobs started from the dashboard.

A job is a separate ``python -m mlpt ...`` process, so a long back-test never
blocks the event loop that is trading. One job runs at a time; its console
output goes to a log file and its result to a JSON file, both polled by the
dashboard.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from ..config import Config

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class JobRunner:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.dir = Path(cfg.data_dir).resolve()
        self.log_path = self.dir / "backtest_job.log"
        self.result_path = self.dir / "backtest_result.json"
        self.lists_path = self.dir / "backtest_lists.csv"
        self.proc: Optional[subprocess.Popen] = None
        self.kind = ""
        self.started = 0.0
        self.finished = 0.0
        self._log_file: Any = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, kind: str, args: list[str]) -> tuple[bool, str]:
        if self.running:
            return False, "כבר רצה משימה - המתן לסיומה או עצור אותה"
        self.dir.mkdir(parents=True, exist_ok=True)
        if kind == "backtest":
            self.result_path.unlink(missing_ok=True)
        cmd = [sys.executable, "-m", "mlpt"]
        if self.cfg.path:
            cmd += ["-c", self.cfg.path]
        cmd += ["--data-dir", str(self.dir)] + args
        env = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1",
                   PYTHONPATH=os.pathsep.join(filter(None, [str(PROJECT_ROOT), os.environ.get("PYTHONPATH", "")])))
        self._close_log()
        self._log_file = open(self.log_path, "w", encoding="utf-8")
        try:
            self.proc = subprocess.Popen(cmd, stdout=self._log_file, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, env=env, cwd=os.getcwd())
        except OSError as e:
            self._close_log()
            return False, f"הפעלת המשימה נכשלה: {e}"
        self.kind, self.started, self.finished = kind, time.time(), 0.0
        return True, "המשימה התחילה"

    def cancel(self) -> tuple[bool, str]:
        if not self.running:
            return False, "אין משימה רצה"
        self.proc.terminate()   # type: ignore[union-attr]
        return True, "המשימה נעצרה"

    def _close_log(self) -> None:
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None

    def status(self) -> dict[str, Any]:
        state, code = "idle", None
        if self.proc is not None:
            code = self.proc.poll()
            if code is None:
                state = "running"
            else:
                state = "done" if code == 0 else "failed"
                if not self.finished:
                    self.finished = time.time()
                    self._close_log()
        log = ""
        if self.log_path.exists() and self.proc is not None:
            log = self.log_path.read_text(encoding="utf-8", errors="replace")[-6000:]
        result = None
        if self.kind == "backtest" and state == "done" and self.result_path.exists():
            try:
                result = json.loads(self.result_path.read_text(encoding="utf-8"))
            except ValueError:
                state = "failed"
        elif self.proc is None and self.result_path.exists():
            # result of the last back-test before the dashboard was restarted
            try:
                result = json.loads(self.result_path.read_text(encoding="utf-8"))
            except ValueError:
                result = None
        return {"state": state, "kind": self.kind, "returncode": code, "log": log, "result": result,
                "elapsed": round((self.finished or time.time()) - self.started, 1) if self.started else 0}
