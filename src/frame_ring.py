"""Read the BF4-only DXGI Present ring. Each record is committed atomically."""
import mmap
from pathlib import Path
import struct

HEADER = struct.Struct('<8sIIIIqqq16x')
RECORD = struct.Struct('<qqqII')
SEQUENCE = struct.Struct('<q')


class FrameRing:
    def __init__(self, path):
        self.path = Path(path)
        self.file = self.path.open('rb')
        try:
            self.mapping = mmap.mmap(self.file.fileno(), 0, access=mmap.ACCESS_READ)
        except (OSError, ValueError):
            self.file.close()
            raise
        self.last = 0
        self.session = None
        self.missed = 0
        self.torn = 0
        self.resets = 0

    def header(self):
        if len(self.mapping) < HEADER.size:
            raise ValueError('BF4 Present telemetry header is incomplete')
        values = HEADER.unpack_from(self.mapping)
        magic, version, capacity, writer, size, committed, frequency, session = values
        if magic != b'BF4FT02\0' or version != 2 or capacity != 65536 or size != 32 or frequency <= 0:
            raise ValueError('BF4 Present telemetry is not initialized')
        if len(self.mapping) != HEADER.size + capacity * size or committed < 0 or session <= 0:
            raise ValueError('Invalid BF4 Present telemetry size or counters')
        return values

    def reset_cursor(self):
        h = self.header()
        self.session, self.last = h[7], h[5]

    def read(self):
        h = self.header()
        capacity, committed, frequency, session = h[2], h[5], h[6], h[7]
        if session != self.session:
            if self.session is not None:
                self.resets += 1
            self.session, self.last = session, committed
            return []
        if committed < self.last:
            raise ValueError('BF4 Present source counter moved backwards')
        first = max(self.last + 1, committed - capacity + 1)
        self.missed += max(0, first - self.last - 1)
        result = []
        for index in range(first, committed + 1):
            offset = HEADER.size + ((index - 1) % capacity) * RECORD.size
            sequence, timestamp, interval, flags, _ = RECORD.unpack_from(self.mapping, offset)
            after = SEQUENCE.unpack_from(self.mapping, offset)[0]
            if sequence != index or after != index:
                self.torn += 1
                continue
            if interval <= 0:
                self.torn += 1
                continue
            result.append((index, timestamp, 1000 * interval / frequency, flags))
        self.last = committed
        return result

    def close(self):
        self.mapping.close()
        self.file.close()
