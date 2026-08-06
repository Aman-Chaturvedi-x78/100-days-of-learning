"""
Simple Snowflake-like ID generator with Base62 encoding.
This implementation is intentionally small for demo purposes.
"""
import threading
import time
import os

EPOCH = 1672531200000  # custom epoch (Jan 1, 2023) in ms
NODE_ID = int(os.getenv("NODE_ID", "1")) & 0x1F  # 5 bits for node id

alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

class IDGenerator:
    def __init__(self):
        self.lock = threading.Lock()
        self.last_ts = 0
        self.sequence = 0

    def _timestamp(self):
        return int(time.time() * 1000)

    def next_id(self):
        with self.lock:
            ts = self._timestamp()
            if ts == self.last_ts:
                self.sequence = (self.sequence + 1) & 0xFFF  # 12 bits
                if self.sequence == 0:
                    # wait next ms
                    while ts <= self.last_ts:
                        ts = self._timestamp()
            else:
                self.sequence = 0
            self.last_ts = ts
            ts_part = (ts - EPOCH) & 0x1FFFFFFFFFF  # 41 bits
            id_val = (ts_part << (5 + 12)) | (NODE_ID << 12) | self.sequence
            return self._encode_base62(id_val)

    def _encode_base62(self, n):
        if n == 0:
            return alphabet[0]
        s = ""
        base = len(alphabet)
        while n > 0:
            s = alphabet[n % base] + s
            n //= base
        return s

# module-level generator
_gen = IDGenerator()

def generate_id():
    return _gen.next_id()
