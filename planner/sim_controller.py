# -*- coding: utf-8 -*-
"""QTimer-driven simulation clock for Planner playback."""

from __future__ import annotations

from datetime import timedelta

from qgis.PyQt.QtCore import QObject, QElapsedTimer, QTimer, pyqtSignal


class SimulationController(QObject):
    timeChanged = pyqtSignal(object)
    playingChanged = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.result = None
        self.current_time = None
        self.sim_seconds_per_real_second = 3600.0
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._tick)
        self.elapsed = QElapsedTimer()

    def set_result(self, result, preserve_time=True):
        self.result = result
        if result is None or result.span_start is None:
            self.current_time = None
            self.pause()
            return
        if (not preserve_time or self.current_time is None or
                self.current_time < result.span_start or self.current_time > result.span_end):
            self.current_time = result.span_start
        self.timeChanged.emit(self.current_time)

    def is_playing(self):
        return self.timer.isActive()

    def set_speed(self, sim_seconds_per_real_second):
        self.sim_seconds_per_real_second = max(0.0, float(sim_seconds_per_real_second))

    def play(self):
        if self.result is None or self.result.span_end is None:
            return
        if self.current_time is None or self.current_time >= self.result.span_end:
            self.current_time = self.result.span_start
        self.elapsed.start()
        self.timer.start()
        self.playingChanged.emit(True)

    def pause(self):
        was_active = self.timer.isActive()
        self.timer.stop()
        if was_active:
            self.playingChanged.emit(False)

    def toggle(self, playing):
        self.play() if playing else self.pause()

    def seek_fraction(self, fraction):
        if self.result is None or self.result.span_start is None or self.result.span_end is None:
            return
        fraction = min(1.0, max(0.0, float(fraction)))
        seconds = (self.result.span_end - self.result.span_start).total_seconds()
        self.current_time = self.result.span_start + timedelta(seconds=seconds * fraction)
        self.timeChanged.emit(self.current_time)

    def step_boundary(self, direction):
        if self.result is None or not self.result.tasks:
            return
        boundaries = sorted(set(
            [self.result.span_start, self.result.span_end] +
            [task.start for task in self.result.tasks] + [task.finish for task in self.result.tasks]
        ))
        current = self.current_time or self.result.span_start
        candidates = [value for value in boundaries if value > current] if direction > 0 else [
            value for value in boundaries if value < current]
        if candidates:
            self.current_time = candidates[0] if direction > 0 else candidates[-1]
        else:
            self.current_time = self.result.span_end if direction > 0 else self.result.span_start
        self.timeChanged.emit(self.current_time)

    def _tick(self):
        if self.result is None or self.current_time is None:
            self.pause()
            return
        milliseconds = self.elapsed.restart()
        self.current_time += timedelta(
            seconds=(milliseconds / 1000.0) * self.sim_seconds_per_real_second)
        if self.current_time >= self.result.span_end:
            self.current_time = self.result.span_end
            self.timeChanged.emit(self.current_time)
            self.pause()
            return
        self.timeChanged.emit(self.current_time)

    def shutdown(self):
        self.pause()
