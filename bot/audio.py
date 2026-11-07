"""
Audio engine: per-user relay.

source voice chat --(py-tgcalls record frames)--> ffmpeg (effects) --> one FIFO per target
                                                                       --> py-tgcalls play(MediaStream(fifo))

All py-tgcalls specific calls live in Relay.start_record / Relay.join / Relay.leave_*
so they are easy to adapt if the library API changes.
"""
import asyncio
import logging
import math
import os
import struct
import subprocess
import time
from dataclasses import asdict, dataclass
from typing import Awaitable, Callable, Dict, Optional

import numpy as np

from .config import REC_CHANNELS, REC_SAMPLE_RATE

log = logging.getLogger("relay")

CHUNK = REC_SAMPLE_RATE * REC_CHANNELS * 2 // 50  # 20 ms of s16le audio
FIFO_DIR = "/tmp/audio_forwarder"
REC_DIR = "/tmp/audio_forwarder/rec"


def _pick_codec():
    """mp3 if available (works everywhere), else AAC/m4a."""
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    except Exception:
        out = ""
    return ("libmp3lame", "mp3") if "libmp3lame" in out else ("aac", "m4a")


FILE_CODEC, FILE_EXT = _pick_codec()


# --------------------------------------------------------------------------- effects
@dataclass
class Effects:
    level: float = 1.0        # volume multiplier
    bass: float = 0.0         # dB
    treble: float = 0.0       # dB
    gain: float = 0.0         # dB
    overdrive: float = 0.0    # dB of pre-clip drive (distortion)
    echo_delay: int = 0       # ms (0 = off)
    echo_decay: float = 0.0   # 0..1

    @staticmethod
    def _stack(name: str, db: float) -> list:
        # ffmpeg limits one bass/treble filter to +-20 dB; chain several for "no limits".
        parts, remaining = [], db
        while remaining > 0:
            step = min(remaining, 20.0)
            parts.append(f"{name}=g={step:.3f}")
            remaining -= step
        return parts

    def filter_chain(self) -> str:
        f = []
        if self.bass:
            f += self._stack("bass", self.bass)
        if self.treble:
            f += self._stack("treble", self.treble)
        if self.gain:
            f.append(f"volume={self.gain:.3f}dB")
        if self.overdrive:
            f.append(f"volume={self.overdrive:.3f}dB")
            f.append("asoftclip=type=tanh")
        if self.echo_delay:
            f.append(f"aecho=0.8:0.9:{int(self.echo_delay)}:{self.echo_decay:.3f}")
        if self.level != 1.0:
            f.append(f"volume={self.level:.4f}")
        return ",".join(f) if f else "anull"

    def describe(self) -> str:
        echo = f"{self.echo_delay}ms / {self.echo_decay:g}" if self.echo_delay else "off"
        return (
            f"🔊 Level: <b>{self.level:g}</b>\n"
            f"🥁 Bass: <b>{self.bass:g}</b>\n"
            f"🎼 Treble: <b>{self.treble:g}</b>\n"
            f"📈 Gain: <b>{self.gain:g}</b>\n"
            f"🔥 Overdrive: <b>{self.overdrive:g}</b>\n"
            f"🔁 Echo: <b>{echo}</b>"
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Effects":
        e = cls()
        for k, v in (d or {}).items():
            if hasattr(e, k):
                setattr(e, k, type(getattr(e, k))(v))
        return e


def parse_number(text: str) -> float:
    value = float(text)
    if math.isnan(value) or math.isinf(value) or value < 0:
        raise ValueError("number must be a finite value >= 0")
    return value


def wav_header(rate: int, channels: int) -> bytes:
    """WAV header with 'unknown' length so ffmpeg treats the FIFO as a live stream."""
    return (
        b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * channels * 2, channels * 2, 16)
        + b"data" + struct.pack("<I", 0xFFFFFFFF)
    )


# --------------------------------------------------------------------------- relay
class Relay:
    def __init__(self, uid: int, rt, on_change: Optional[Callable[[], Awaitable[None]]] = None):
        self.uid = uid
        self.rt = rt                                # Runtime: gives access to the rec / bc accounts
        self.sendfile = True                        # send the recording file to targets on stop
        self.rec_proc: Optional[asyncio.subprocess.Process] = None
        self.rec_path: Optional[str] = None
        self.rec_started = 0.0
        self.on_change = on_change
        self.effects = Effects()
        self.muted = False
        self.source: Optional[int] = None          # chat we record from (saved)
        self.saved_targets: list = []               # chats we forward to (saved)
        self.recording = False
        self.targets: Dict[int, dict] = {}          # active: chat_id -> {"path","fd"}
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._pump_task: Optional[asyncio.Task] = None
        self._tick_task: Optional[asyncio.Task] = None
        self._last_real = 0.0
        self._lock = asyncio.Lock()
        os.makedirs(FIFO_DIR, exist_ok=True)

    # ---- persistence
    def export(self) -> dict:
        return {"effects": self.effects.to_dict(), "source": self.source,
                "targets": self.saved_targets, "sendfile": self.sendfile}

    def load(self, data: dict) -> None:
        data = data or {}
        self.effects = Effects.from_dict(data.get("effects"))
        self.source = data.get("source")
        self.saved_targets = list(data.get("targets") or [])
        self.sendfile = bool(data.get("sendfile", True))

    async def _changed(self) -> None:
        if self.on_change:
            try:
                await self.on_change()
            except Exception:
                log.exception("persist failed")

    # ---- ffmpeg pipeline
    async def _start_ffmpeg(self) -> None:
        if self.proc and self.proc.returncode is None:
            return
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-fflags", "+nobuffer+flush_packets",
            "-probesize", "32", "-analyzeduration", "0",
            "-f", "s16le", "-ar", str(REC_SAMPLE_RATE), "-ac", str(REC_CHANNELS), "-i", "pipe:0",
            "-af", self.effects.filter_chain(),
            "-f", "s16le", "-ar", str(REC_SAMPLE_RATE), "-ac", str(REC_CHANNELS), "pipe:1",
        ]
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._pump_task = asyncio.create_task(self._pump(self.proc))
        if not self._tick_task or self._tick_task.done():
            self._tick_task = asyncio.create_task(self._ticker())

    async def _stop_ffmpeg(self) -> None:
        proc, self.proc = self.proc, None
        for t in (self._pump_task, self._tick_task):
            if t and not t.done():
                t.cancel()
        self._pump_task = self._tick_task = None
        if proc and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass

    async def _restart_ffmpeg(self) -> None:
        old, old_pump = self.proc, self._pump_task
        self.proc = None
        if old_pump and not old_pump.done():
            old_pump.cancel()
        if old and old.returncode is None:
            try:
                old.kill()
                await old.wait()
            except ProcessLookupError:
                pass
        if self.recording or self.targets:
            await self._start_ffmpeg()

    async def _pump(self, proc: asyncio.subprocess.Process) -> None:
        """ffmpeg stdout -> every target FIFO."""
        try:
            while True:
                data = await proc.stdout.readexactly(CHUNK)
                for t in list(self.targets.values()):
                    try:
                        os.write(t["fd"], data)
                    except BlockingIOError:
                        pass  # consumer is behind: drop to stay live
                    except OSError:
                        pass
        except (asyncio.IncompleteReadError, asyncio.CancelledError):
            return
        except Exception:
            log.exception("pump crashed")

    async def _ticker(self) -> None:
        """Keep the stream continuous with silence while nobody speaks."""
        silence = bytes(CHUNK)
        try:
            while True:
                await asyncio.sleep(0.02)
                if time.monotonic() - self._last_real > 0.06:
                    self._write(silence)
        except asyncio.CancelledError:
            return

    def _write(self, data: bytes) -> None:
        proc = self.proc
        if not proc or proc.returncode is not None or proc.stdin is None:
            return
        try:
            if proc.stdin.transport.get_write_buffer_size() > 1_000_000:
                return  # backlog: drop
            proc.stdin.write(data)
        except Exception:
            pass

    # ---- incoming frames from py-tgcalls
    async def on_update(self, update) -> None:
        frames = getattr(update, "frames", None)
        if frames is None:
            return
        chat_id = getattr(update, "chat_id", None)
        if self.source is None or chat_id != self.source or not self.recording:
            return
        direction = str(getattr(update, "direction", "")).lower()
        if "outgoing" in direction:
            return
        chunks = []
        for fr in frames:
            raw = getattr(fr, "frame", None) or getattr(fr, "data", None)
            if raw:
                chunks.append(np.frombuffer(raw, dtype=np.int16))
        if not chunks:
            return
        if len(chunks) == 1:
            pcm = chunks[0].tobytes()
        else:  # several speakers at once: mix
            n = min(len(c) for c in chunks)
            mixed = np.sum([c[:n].astype(np.int32) for c in chunks], axis=0)
            pcm = np.clip(mixed, -32768, 32767).astype(np.int16).tobytes()
        self._last_real = time.monotonic()
        self._write_rec(pcm)  # the saved recording is always the raw, unmuted source
        self._write(bytes(len(pcm)) if self.muted else pcm)

    def _write_rec(self, data: bytes) -> None:
        p = self.rec_proc
        if not p or p.returncode is not None or p.stdin is None:
            return
        try:
            p.stdin.write(data)
        except Exception:
            pass

    # ---- recording to a file (sent to the target chats when recording stops)
    async def _start_rec_file(self) -> None:
        if self.rec_proc and self.rec_proc.returncode is None:
            return
        os.makedirs(REC_DIR, exist_ok=True)
        self.rec_path = os.path.join(REC_DIR, f"rec_{self.uid}_{int(time.time())}.{FILE_EXT}")
        self.rec_started = time.time()
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "s16le", "-ar", str(REC_SAMPLE_RATE), "-ac", str(REC_CHANNELS), "-i", "pipe:0",
               "-c:a", FILE_CODEC, "-b:a", "64k", self.rec_path]
        self.rec_proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)

    async def _finish_rec_file(self) -> Optional[dict]:
        proc, path = self.rec_proc, self.rec_path
        self.rec_proc = None
        if not proc or not path:
            return None
        try:
            if proc.stdin:
                proc.stdin.close()
            await asyncio.wait_for(proc.wait(), 30)
        except Exception:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        if os.path.exists(path) and os.path.getsize(path) > 2000:
            return {"path": path, "seconds": int(time.time() - self.rec_started)}
        try:
            os.remove(path)
        except OSError:
            pass
        return None

    # ---- controls
    @property
    def active(self) -> bool:
        return self.recording or bool(self.targets)

    async def start_record(self, chat_id: int) -> None:
        async with self._lock:
            from pytgcalls.types import AudioQuality, RecordStream

            if chat_id in self.targets or chat_id in self.saved_targets:
                raise ValueError("The SOURCE and a TARGET can't be the same chat. Remove it from targets first.")
            side = await self.rt.ensure("rec")
            self.source = chat_id
            self.recording = True
            await self._start_ffmpeg()
            await self._start_rec_file()
            try:
                await side.call.record(chat_id, RecordStream(audio=True, audio_parameters=AudioQuality.HIGH))
            except Exception:
                self.recording = False
                await self._finish_rec_file()
                if not self.targets:
                    await self._stop_ffmpeg()
                raise
        await self._changed()

    async def join(self, chat_id: int) -> None:
        async with self._lock:
            if chat_id == self.source:
                raise ValueError("This chat is your SOURCE. The target must be a different group.")
            if chat_id in self.targets:
                raise ValueError("Already forwarding to this chat.")
            from pytgcalls.types import AudioQuality, MediaStream

            side = await self.rt.ensure("bc")
            await self._start_ffmpeg()
            path = os.path.join(FIFO_DIR, f"{self.uid}_{abs(chat_id)}.wav")
            if os.path.exists(path):
                os.remove(path)
            os.mkfifo(path)
            fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
            os.write(fd, wav_header(REC_SAMPLE_RATE, REC_CHANNELS))
            self.targets[chat_id] = {"path": path, "fd": fd}
            try:
                await side.call.play(
                    chat_id,
                    MediaStream(path, AudioQuality.HIGH, video_flags=MediaStream.Flags.IGNORE),
                )
            except Exception:
                self._drop_target(chat_id)
                if not self.active:
                    await self._stop_ffmpeg()
                raise
            if chat_id not in self.saved_targets:
                self.saved_targets.append(chat_id)
        await self._changed()

    def _drop_target(self, chat_id: int) -> None:
        t = self.targets.pop(chat_id, None)
        if not t:
            return
        try:
            os.close(t["fd"])
        except OSError:
            pass
        try:
            os.remove(t["path"])
        except OSError:
            pass

    async def leave(self, chat_id: int, forget: bool = True) -> None:
        async with self._lock:
            if chat_id not in self.targets and chat_id not in self.saved_targets:
                raise ValueError("Not forwarding to that chat.")
            if chat_id in self.targets:
                bc = self.rt.sides.get("bc")
                if bc:
                    try:
                        await bc.call.leave_call(chat_id)
                    except Exception as exc:
                        log.warning("leave_call(%s): %s", chat_id, exc)
                self._drop_target(chat_id)
            if forget and chat_id in self.saved_targets:
                self.saved_targets.remove(chat_id)
            if not self.active:
                await self._stop_ffmpeg()
        await self._changed()

    async def leave_record(self) -> Optional[dict]:
        """Stops recording. Returns {"path","seconds"} of the saved recording, if any."""
        async with self._lock:
            if not self.recording or self.source is None:
                raise ValueError("Not recording.")
            rec = self.rt.sides.get("rec")
            if rec:
                try:
                    await rec.call.leave_call(self.source)
                except Exception as exc:
                    log.warning("leave_call(source): %s", exc)
            self.recording = False
            result = await self._finish_rec_file()
            if not self.targets:
                await self._stop_ffmpeg()
        await self._changed()
        return result

    async def leave_all(self, forget: bool = True) -> tuple:
        """Returns (source_left, targets_left, recording_info_or_None)."""
        src, info = False, None
        if self.recording:
            try:
                info = await self.leave_record()
                src = True
            except Exception:
                pass
        n = 0
        for chat_id in list(self.targets):
            try:
                await self.leave(chat_id, forget=forget)
                n += 1
            except Exception:
                pass
        if forget:
            self.saved_targets.clear()
        await self._changed()
        return src, n, info

    async def rejoin(self) -> tuple:
        """Reconnect saved source + targets. Returns (ok_lines, error_lines)."""
        ok, errs = [], []
        if self.source is not None and not self.recording:
            try:
                await self.start_record(self.source)
                ok.append(f"source {self.source}")
            except Exception as exc:
                errs.append((f"source {self.source}", exc))
        for chat_id in list(self.saved_targets):
            if chat_id in self.targets:
                continue
            try:
                await self.join(chat_id)
                ok.append(f"target {chat_id}")
            except Exception as exc:
                errs.append((f"target {chat_id}", exc))
        return ok, errs

    async def set_effects(self, **changes) -> None:
        for k, v in changes.items():
            setattr(self.effects, k, v)
        if self.proc:
            await self._restart_ffmpeg()
        await self._changed()

    async def reset_effects(self) -> None:
        self.effects = Effects()
        if self.proc:
            await self._restart_ffmpeg()
        await self._changed()

    async def add_target(self, chat_id: int) -> None:
        if chat_id == self.source:
            raise ValueError("This chat is your SOURCE. The target must be a different group.")
        if chat_id not in self.saved_targets:
            self.saved_targets.append(chat_id)
        await self._changed()

    async def set_sendfile(self, on: bool) -> None:
        self.sendfile = on
        await self._changed()

    async def shutdown(self) -> None:
        bc, rec = self.rt.sides.get("bc"), self.rt.sides.get("rec")
        for chat_id in list(self.targets):
            if bc:
                try:
                    await bc.call.leave_call(chat_id)
                except Exception:
                    pass
            self._drop_target(chat_id)
        if self.recording and self.source is not None and rec:
            try:
                await rec.call.leave_call(self.source)
            except Exception:
                pass
        self.recording = False
        await self._finish_rec_file()
        await self._stop_ffmpeg()
