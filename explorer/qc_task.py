# -*- coding: utf-8 -*-
"""Background QC execution task for the Explorer panel."""

from __future__ import annotations

from typing import List, Optional, Sequence

from qgis.core import QgsTask

from ..laydata import QcRunner


def _task_flag(name: str, default: int = 0):
    enum = getattr(QgsTask, "Flag", QgsTask)
    return getattr(enum, name, default)


_CAN_CANCEL = _task_flag("CanCancel")


class QcRunTask(QgsTask):
    """Run selected QC checks over a dataset on a worker thread."""

    def __init__(self, dataset, checks: Sequence, description: str = "Running QC checks"):
        super().__init__(description, _CAN_CANCEL)
        self._dataset = dataset
        self._checks = list(checks)
        self.findings: List = []
        self.error: Optional[str] = None

    def run(self) -> bool:
        try:
            total = max(len(self._checks), 1)

            def _progress(done, _total, _check_id):
                self.setProgress(min(100.0, float(done) / float(total) * 100.0))

            runner = QcRunner(self._dataset)
            self.findings = runner.run(
                self._checks,
                progress=_progress,
                is_canceled=self.isCanceled,
            )
            self.setProgress(100.0)
            return not self.isCanceled()
        except Exception as exc:  # pragma: no cover
            self.error = str(exc)
            return False
