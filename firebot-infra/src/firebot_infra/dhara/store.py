"""DHARA: a small log-structured key-value store (Bitcask-style) with crash-injection tests.

SqliteKV is the established baseline with the same interface: keep using SQLite unless benchmarks
(dhara.compare) on your workload justify this engine.

Record: crc32 u32 | type u8 | klen u32 | vlen u32 | key | value   (crc covers everything after itself)
Types : 1 PUT, 2 DELETE, 3 BATCH (value = concatenated sub-ops; ONE crc => all-or-nothing).
Recovery: open scans from the start and stops at the first torn/corrupt record, then truncates the file there,
so the store always opens to a state equal to some prefix of committed operations.
"""
from __future__ import annotations

import os
import sqlite3
import struct
import threading
import time
import zlib
from pathlib import Path
from typing import Iterable, Iterator

try:
    import fcntl
except ImportError:  # non-POSIX
    fcntl = None  # type: ignore[assignment]

HDR = struct.Struct(">IBII")
SUB = struct.Struct(">BII")
PUT, DEL, BATCH = 1, 2, 3
MAX_KEY, MAX_VAL = 1 << 16, 1 << 28

Key = bytes | str


class DharaCorruption(Exception): ...
class StoreLocked(Exception): ...


def _k(key: Key) -> bytes:
    k = key.encode() if isinstance(key, str) else bytes(key)
    if not 0 < len(k) <= MAX_KEY:
        raise ValueError("key must be 1..65536 bytes")
    return k


def _record(rtype: int, key: bytes, value: bytes) -> bytes:
    body = SUB.pack(rtype, len(key), len(value)) + key + value
    return (zlib.crc32(body) & 0xFFFFFFFF).to_bytes(4, "big") + body


class DharaStore:
    def __init__(self, directory: str | os.PathLike, sync: str = "interval", fsync_interval_s: float = 1.0) -> None:
        if sync not in ("always", "interval", "none"):
            raise ValueError("sync must be always|interval|none")
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "dhara.log"
        self._sync, self._interval = sync, fsync_interval_s
        self._lock = threading.RLock()
        self._lockfd = os.open(self.dir / "LOCK", os.O_CREAT | os.O_RDWR)
        if fcntl is not None:
            try:
                fcntl.flock(self._lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(self._lockfd)
                raise StoreLocked(str(self.dir)) from None
        (self.dir / "dhara.log.compact").unlink(missing_ok=True)   # leftover from a crashed compaction
        self._index: dict[bytes, tuple[int, int, int, int]] = {}   # key -> (rec_off, rec_len, val_off_in_rec, vlen)
        self._live_payload = 0
        self.recovered_dropped_bytes = 0
        self._last_sync = time.monotonic()
        self._closed = False
        self._open_and_recover()

    # ---------------------------------------------------------------- recovery
    def _open_and_recover(self) -> None:
        self._index.clear()
        self._live_payload = 0
        self.path.touch(exist_ok=True)
        good = 0
        with open(self.path, "rb") as f:
            while True:
                hdr = f.read(HDR.size)
                if len(hdr) < HDR.size:
                    break
                crc, rtype, kl, vl = HDR.unpack(hdr)
                if rtype not in (PUT, DEL, BATCH) or kl > MAX_KEY or vl > MAX_VAL:
                    break
                body = f.read(kl + vl)
                if len(body) < kl + vl or zlib.crc32(hdr[4:] + body) & 0xFFFFFFFF != crc:
                    break
                if not self._apply(good, HDR.size + kl + vl, rtype, body[:kl], body[kl:]):
                    break
                good += HDR.size + kl + vl
        size = self.path.stat().st_size
        if good < size:
            self.recovered_dropped_bytes = size - good
            with open(self.path, "r+b") as f:
                f.truncate(good)
        self._f = open(self.path, "ab")
        self._rf = open(self.path, "rb")
        self._size = good

    def _apply(self, off: int, rec_len: int, rtype: int, key: bytes, value: bytes) -> bool:
        if rtype == PUT:
            self._set(key, (off, rec_len, HDR.size + len(key), len(value)))
        elif rtype == DEL:
            self._drop(key)
        else:
            pos = 0
            while pos < len(value):
                if pos + SUB.size > len(value):
                    return False
                t, kl, vl = SUB.unpack_from(value, pos)
                pos += SUB.size
                if t not in (PUT, DEL) or pos + kl + vl > len(value):
                    return False
                k = value[pos:pos + kl]
                if t == PUT:
                    self._set(k, (off, rec_len, HDR.size + pos + kl, vl))
                else:
                    self._drop(k)
                pos += kl + vl
        return True

    def _set(self, key: bytes, ent: tuple[int, int, int, int]) -> None:
        old = self._index.get(key)
        if old:
            self._live_payload -= len(key) + old[3]
        self._index[key] = ent
        self._live_payload += len(key) + ent[3]

    def _drop(self, key: bytes) -> None:
        old = self._index.pop(key, None)
        if old:
            self._live_payload -= len(key) + old[3]

    # ---------------------------------------------------------------- writes
    def _append(self, buf: bytes) -> None:
        """Single point where bytes hit the log (crash-injection tests override this)."""
        try:
            self._f.write(buf)
            self._f.flush()
            if self._sync == "always" or (self._sync == "interval" and time.monotonic() - self._last_sync >= self._interval):
                os.fsync(self._f.fileno())
                self._last_sync = time.monotonic()
        except OSError:
            self._rollback()
            raise

    def _rollback(self) -> None:
        """A failed write must leave no trace: drop any part of the record that reached the file, so the log stays
        whole records only and the operation the caller saw fail does not reappear later."""
        try:
            self._f.close()
        except OSError:
            pass
        try:
            with open(self.path, "r+b") as f:
                f.truncate(self._size)
        finally:
            self._f = open(self.path, "ab")

    def _commit(self, buf: bytes, rtype: int, key: bytes, value: bytes) -> None:
        off = self._size
        self._append(buf)
        self._size += len(buf)
        self._apply(off, len(buf), rtype, key, value)

    def put(self, key: Key, value: bytes) -> None:
        k, v = _k(key), bytes(value)
        if len(v) > MAX_VAL:
            raise ValueError("value too large")
        with self._lock:
            self._commit(_record(PUT, k, v), PUT, k, v)

    def delete(self, key: Key) -> bool:
        k = _k(key)
        with self._lock:
            if k not in self._index:
                return False
            self._commit(_record(DEL, k, b""), DEL, k, b"")
            return True

    def write_batch(self, ops: Iterable[tuple]) -> None:
        """ops: ("put", key, value) | ("delete", key). Atomic: all applied or none, across crashes."""
        sub = bytearray()
        for op in ops:
            if op[0] == "put":
                k, v = _k(op[1]), bytes(op[2])
                sub += SUB.pack(PUT, len(k), len(v)) + k + v
            elif op[0] == "delete":
                k = _k(op[1])
                sub += SUB.pack(DEL, len(k), 0) + k
            else:
                raise ValueError(f"bad op {op[0]!r}")
        if not sub:
            return
        with self._lock:
            self._commit(_record(BATCH, b"", bytes(sub)), BATCH, b"", bytes(sub))

    # ---------------------------------------------------------------- reads
    def get(self, key: Key) -> bytes | None:
        k = _k(key)
        with self._lock:
            ent = self._index.get(k)
            if ent is None:
                return None
            off, rlen, voff, vlen = ent
            rec = os.pread(self._rf.fileno(), rlen, off)
        if len(rec) != rlen or zlib.crc32(rec[4:]) & 0xFFFFFFFF != int.from_bytes(rec[:4], "big"):
            raise DharaCorruption(f"record for {k!r} failed checksum")
        return rec[voff:voff + vlen]

    def __contains__(self, key: Key) -> bool:
        return _k(key) in self._index

    def __len__(self) -> int:
        return len(self._index)

    def keys(self, prefix: Key = b"") -> list[bytes]:
        p = prefix.encode() if isinstance(prefix, str) else bytes(prefix)
        with self._lock:
            return sorted(k for k in self._index if k.startswith(p))

    def items(self, prefix: Key = b"") -> Iterator[tuple[bytes, bytes]]:
        for k in self.keys(prefix):
            v = self.get(k)
            if v is not None:
                yield k, v

    # ---------------------------------------------------------------- maintenance
    def stats(self) -> dict:
        with self._lock:
            return {"keys": len(self._index), "file_bytes": self._size, "live_payload_bytes": self._live_payload,
                    "garbage_ratio": max(0.0, 1 - (self._live_payload + len(self._index) * HDR.size) / max(1, self._size)),
                    "recovered_dropped_bytes": self.recovered_dropped_bytes}

    def compact(self) -> None:
        """Rewrite only live data. Crash-safe: the old log stays valid until the atomic rename."""
        with self._lock:
            tmp = self.dir / "dhara.log.compact"
            with open(tmp, "wb") as f:
                for k in sorted(self._index):
                    f.write(_record(PUT, k, self.get(k) or b""))
                f.flush()
                os.fsync(f.fileno())
            self._f.close()
            self._rf.close()
            os.replace(tmp, self.path)
            dfd = os.open(self.dir, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
            self._open_and_recover()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._f.flush()
                os.fsync(self._f.fileno())
            finally:
                self._f.close()
                self._rf.close()
                if fcntl is not None:
                    fcntl.flock(self._lockfd, fcntl.LOCK_UN)
                os.close(self._lockfd)

    def __enter__(self): return self
    def __exit__(self, *a): self.close()


class SqliteKV:
    """Established baseline with the same interface."""

    def __init__(self, directory: str | os.PathLike) -> None:
        Path(directory).mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(Path(directory) / "kv.sqlite", check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS kv (k BLOB PRIMARY KEY, v BLOB NOT NULL) WITHOUT ROWID")
        self._db.commit()

    def put(self, key: Key, value: bytes) -> None:
        self._db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (_k(key), bytes(value)))
        self._db.commit()

    def get(self, key: Key) -> bytes | None:
        r = self._db.execute("SELECT v FROM kv WHERE k = ?", (_k(key),)).fetchone()
        return None if r is None else bytes(r[0])

    def delete(self, key: Key) -> bool:
        cur = self._db.execute("DELETE FROM kv WHERE k = ?", (_k(key),))
        self._db.commit()
        return cur.rowcount > 0

    def write_batch(self, ops: Iterable[tuple]) -> None:
        with self._db:
            for op in ops:
                if op[0] == "put":
                    self._db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (_k(op[1]), bytes(op[2])))
                elif op[0] == "delete":
                    self._db.execute("DELETE FROM kv WHERE k = ?", (_k(op[1]),))
                else:
                    raise ValueError(f"bad op {op[0]!r}")

    def keys(self, prefix: Key = b"") -> list[bytes]:
        p = prefix.encode() if isinstance(prefix, str) else bytes(prefix)
        return sorted(bytes(r[0]) for r in self._db.execute("SELECT k FROM kv") if bytes(r[0]).startswith(p))

    def items(self, prefix: Key = b"") -> Iterator[tuple[bytes, bytes]]:
        for k in self.keys(prefix):
            yield k, self.get(k)  # type: ignore[misc]

    def __len__(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM kv").fetchone()[0]

    def close(self) -> None:
        self._db.close()


def compare(directory: str | os.PathLike, n: int = 2000, value_size: int = 100) -> dict[str, dict[str, float]]:
    """Writes/reads on both engines in fresh subdirectories; returns ops/sec. Measure before choosing."""
    out = {}
    val = os.urandom(value_size)
    for name, make in (("dhara", lambda p: DharaStore(p, sync="none")), ("sqlite", SqliteKV)):
        s = make(Path(directory) / name)
        t0 = time.perf_counter()
        for i in range(n):
            s.put(f"key{i:08d}", val)
        t1 = time.perf_counter()
        for i in range(n):
            s.get(f"key{i:08d}")
        t2 = time.perf_counter()
        s.close()
        out[name] = {"put_ops_s": n / (t1 - t0), "get_ops_s": n / (t2 - t1)}
    return out
