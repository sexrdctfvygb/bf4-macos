#!/usr/bin/env python3
"""Private input process watcher; optional DXGI Present collector. No GPU claims."""
# SPDX-License-Identifier: MIT
import argparse
from collections import Counter, deque
import csv
import ctypes
import json
import math
import os
from pathlib import Path
import signal
import time
from bf4 import atomic_json, game_pid, load_config
from frame_ring import FrameRing


class Metrics:
    def __init__(self):
        self.counts = Counter()
        self.recent = deque(maxlen=600)
        self.frames = 0
        self.total_ms = 0.0
        self.maximum = 0.0
        self.thresholds = {16.7: 0, 33.3: 0, 50.0: 0}

    def add(self, interval):
        if not math.isfinite(interval) or interval <= 0: raise ValueError('Invalid interval')
        self.counts[max(.001, round(interval, 3))] += 1
        self.recent.append(interval)
        self.frames += 1
        self.total_ms += interval
        self.maximum = max(self.maximum, interval)
        for threshold in self.thresholds:
            self.thresholds[threshold] += interval > threshold

    def snapshot(self):
        def low(fraction):
            if not self.frames: return None
            needed = math.ceil(self.frames * fraction)
            remaining = needed
            total = 0.0
            for interval, count in sorted(self.counts.items(), reverse=True):
                take = min(remaining, count)
                total += take * interval
                remaining -= take
                if not remaining: break
            return 1000 * needed / total
        recent = list(self.recent)[-120:]
        return {'frames': self.frames, 'elapsed_seconds': self.total_ms / 1000,
                'avg_fps': 1000 * self.frames / self.total_ms if self.frames else None,
                'current_fps': 1000 * len(recent) / math.fsum(recent) if recent else None,
                'low_1_fps': low(.01) if self.frames >= 100 else None,
                'low_01_fps': low(.001) if self.frames >= 1000 else None,
                'low_01_preliminary': self.frames < 10000,
                'max_frame_ms': self.maximum if self.frames else None,
                'over_16_7_ms': self.thresholds[16.7], 'over_33_3_ms': self.thresholds[33.3],
                'over_50_ms': self.thresholds[50.0], 'recent_ms': list(self.recent)[-160:]}


class ProcessInfo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in
        ('flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid', 'rgid', 'svuid', 'svgid', 'reserved')]
    _fields_ += [('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)]
    _fields_ += [(name, ctypes.c_uint32) for name in ('nfiles', 'pgid', 'jobc', 'tdev', 'tpgid', 'nice')]
    _fields_ += [('start_seconds', ctypes.c_uint64), ('start_microseconds', ctypes.c_uint64)]


def process_token(pid):
    library = ctypes.CDLL('/usr/lib/libproc.dylib')
    info = ProcessInfo()
    if library.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
        return None
    return info.pid, info.start_seconds, info.start_microseconds


class CachedFile:
    def __init__(self, path, default=''):
        self.path, self.default, self.stamp, self.value = path, default, None, default

    def read(self):
        try:
            stat = self.path.stat()
            stamp = stat.st_ino, stat.st_mtime_ns, stat.st_size
            if stamp != self.stamp:
                self.value, self.stamp = self.path.read_text(), stamp
            return self.value
        except OSError: return self.default


def live(data):
    try: os.nice(5)
    except OSError: pass
    config = load_config(data)
    monitoring = config['monitoring']
    runtime = data / 'runtime'
    runtime.mkdir(exist_ok=True)
    runs = runtime / 'runs'
    if monitoring: runs.mkdir(exist_ok=True)
    source = data / 'prefix/drive_c/bf4-tools/telemetry.bin'
    focus = CachedFile(runtime / 'input-state.json', '{}')
    reset = CachedFile(runtime / 'reset-request')
    recording_control = CachedFile(runtime / 'record-frames', '0')
    last_reset = reset.read()
    metrics, ring = Metrics(), None
    pid, token, last_probe, last_data = None, None, 0, 0
    last_publish, last_csv_flush, active_before = 0, 0, False
    run_name = None
    output, writer = None, None
    stopping = False
    baseline_missed = baseline_torn = excluded = source_errors = 0

    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)

    def summary():
        result = metrics.snapshot()
        missed = max(0, ring.missed - baseline_missed) if ring else 0
        torn = max(0, ring.torn - baseline_torn) if ring else 0
        result.update(source='Intervals between successful DXGI Present calls (QPC)',
                      bf4_pid=pid or 0, updated_unix=time.time(),
                      status='live' if time.monotonic() - last_data < 1.25 else 'waiting_for_frames',
                      data_complete=not (missed or torn or source_errors), missed_frames=missed, torn_frames=torn,
                      source_errors=source_errors,
                      excluded_background_frames=excluded, recording=output is not None,
                      low_method='1000 / mean of largest ceil(N * fraction) intervals; 1 us histogram')
        return result

    def finish():
        nonlocal output, writer
        if output:
            output.flush(); output.close(); output = writer = None
        if run_name:
            atomic_json(runs / (run_name + '.json'), summary())

    def begin():
        nonlocal metrics, run_name, excluded, baseline_missed, baseline_torn, active_before, source_errors
        finish()
        metrics = Metrics(); excluded = source_errors = 0; active_before = False
        run_name = time.strftime('%Y%m%d-%H%M%S') + '-' + str(time.time_ns() % 1000000)
        if ring:
            ring.reset_cursor()
            baseline_missed, baseline_torn = ring.missed, ring.torn

    try:
        while not stopping:
            now = time.monotonic()
            if now - last_probe > 1:
                found = pid if pid and process_token(pid) == token else game_pid(data)
                if found != pid:
                    if monitoring: finish()
                    if ring: ring.close(); ring = None
                    pid = found; token = process_token(pid) if pid else None
                    if monitoring: begin()
                atomic_json(runtime / 'game-state.json', {'bf4_pid': pid or 0})
                last_probe = now
            if monitoring and pid:
                if ring is None and source.exists():
                    candidate = None
                    try:
                        candidate = FrameRing(source)
                        candidate.header()
                        ring = candidate; begin()
                    except (OSError, ValueError):
                        if candidate: candidate.close()
                request = reset.read()
                if request != last_reset:
                    last_reset = request; begin()
                try: active = json.loads(focus.read()).get('game_active', False)
                except (ValueError, TypeError): active = False
                if ring:
                    try:
                        resets = ring.resets
                        records = ring.read()
                        if ring.resets != resets:
                            begin(); records = []
                    except (OSError, ValueError):
                        source_errors += 1
                        ring.close(); ring = None; records = []
                    if active and active_before:
                        for sequence, timestamp, interval, flags in records:
                            if flags & 3 != 3:
                                excluded += 1; continue
                            metrics.add(interval); last_data = now
                            if writer: writer.writerow((sequence, timestamp, '%.9f' % interval, flags))
                    else: excluded += len(records)
                active_before = active
                recording = recording_control.read().strip() == '1'
                if recording and output is None:
                    if not run_name: begin()
                    output = (runs / (run_name + '.csv')).open('a', newline='')
                    writer = csv.writer(output)
                    if output.tell() == 0: writer.writerow(('sequence', 'qpc', 'present_ms', 'flags'))
                elif not recording and output:
                    output.flush(); output.close(); output = writer = None
                if output and now - last_csv_flush > 2:
                    output.flush(); last_csv_flush = now
            if monitoring and now - last_publish >= .25:
                atomic_json(runtime / 'stats.json', summary()); last_publish = now
            time.sleep(.25 if monitoring else .5)
    finally:
        if monitoring: finish()
        if ring: ring.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    live(parser.parse_args().data_dir.resolve())
