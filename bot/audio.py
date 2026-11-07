"""
Audio engine (per user).

SOURCE voice chat --(source account: py-tgcalls record frames, one buffer per speaker)--> clocked 20 ms mixer
   --> ffmpeg (effects) --> TARGET voice chat(s) (target account)

Playback uses raw frames (ExternalMedia + send_frame) when py-tgcalls supports it, otherwise a WAV FIFO.
Dropped voice chats are detected and re-joined automatically.
"""
import asyncio
import logging
import math
import os
import struct
import subprocess
import time
import wave
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Awaitable, Callable, Dict, Optional

import numpy as np

from .config import PLAYBACK_MODE, REC_CHANNELS, REC_SAMPLE_RATE

log = logging.getLogger("relay")

BPS = REC_SAMPLE_RATE * REC_CHANNELS * 2        # bytes per second of s16le audio
CHUNK = BPS // 50                               # 20 ms
TICK = 0.02
WATCH_WARN = 20.0                               # seconds without any audio before we warn the user
WATCH_SETTLE = 4.0                              # seconds to let format detection settle before reporting
FIFO_DIR = "/tmp/audio_forwarder"
REC_DIR = "/tmp/audio_forwarder/rec"
KNOWN_FORMATS = [(48000, 2), (48000, 1), (24000, 2), (24000, 1), (36000, 1), (16000, 1), (96000, 2)]


def _pick_codec():
    """mp3 if available (works everywhere), else AAC/m4a."""
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    except Exception:
        out = ""
    return ("libmp3lame", "mp3") if "libmp3lame" in out else ("aac", "m4a")


FILE_CODEC, FILE_EXT = _pick_codec()


def audio_params():
    """Explicit sample-rate/channels so record, mixer, effects and playback all agree."""
    from pytgcalls.types import AudioQuality
    try:
        from pytgcalls.types import AudioParameters
        return AudioParameters(REC_SAMPLE_RATE, REC_CHANNELS)
    except Exception:
        return AudioQuality.HIGH


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
        if f:  # any effect active: lookahead limiter keeps it LOUD but never clipped/crackly
            f.append("alimiter=limit=0.95:attack=5:release=60:level=0")
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
        self.rt = rt                                # Runtime: gives access to the source / target accounts
        self.on_change = on_change
        self.notify: Optional[Callable[[str], Awaitable[None]]] = None
        # saved settings
        self.effects = Effects()
        self.muted = False
        self.source: Optional[int] = None
        self.saved_targets: list = []
        self.sendfile = True
        self.rec_content = "source"                 # "source" = original voice, "output" = processed (with effects)
        self.rec_to = "both"                        # "dm" | "target" | "both"
        # live state
        self.recording = False
        self.targets: Dict[int, dict] = {}
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._pump_task: Optional[asyncio.Task] = None
        self._mix_task: Optional[asyncio.Task] = None
        self.rec_proc: Optional[asyncio.subprocess.Process] = None
        self.rec_path: Optional[str] = None
        self.rec_started = 0.0
        self._lock = asyncio.Lock()
        self._streams: Dict[int, dict] = {}         # ssrc -> {"buf","primed","last"}
        self._recovering = set()
        self._leaving = set()
        self._sample: Optional[dict] = None
        self.playback_mode = "fifo" if PLAYBACK_MODE == "auto" else PLAYBACK_MODE   # fifo | external | auto
        self.in_sr, self.in_ch = REC_SAMPLE_RATE, REC_CHANNELS   # format of frames from the SOURCE voice chat
        self.fmt_detected = False
        self._fmt_vote = None
        self._last_out = time.monotonic()
        self._tone: Optional[dict] = None
        self._watch_task: Optional[asyncio.Task] = None
        self._apply_task: Optional[asyncio.Task] = None
        self._reset_stats()
        os.makedirs(FIFO_DIR, exist_ok=True)

    @property
    def in_align(self) -> int:
        return self.in_ch * 2

    @property
    def in_chunk(self) -> int:                      # 20 ms of input-format audio
        return self.in_sr * self.in_ch * 2 // 50

    @property
    def prebuffer(self) -> int:
        return self.in_chunk * 2

    @property
    def max_buffer(self) -> int:
        return self.in_chunk * 25

    def _reset_stats(self) -> None:
        self.stats = {"updates": 0, "frames": 0, "bytes": 0, "first": 0.0, "last": 0.0, "peak": 0,
                      "sizes": Counter(), "ssrcs": set(), "dropped_out": 0, "restarts": 0, "recoveries": 0,
                      "last_rate": 0, "detect_fail": 0}

    # ---- persistence
    def export(self) -> dict:
        return {"effects": self.effects.to_dict(), "source": self.source, "targets": self.saved_targets,
                "sendfile": self.sendfile, "rec_content": self.rec_content, "rec_to": self.rec_to,
                "playback": self.playback_mode}

    def load(self, data: dict) -> None:
        data = data or {}
        self.effects = Effects.from_dict(data.get("effects"))
        self.source = data.get("source")
        self.saved_targets = list(data.get("targets") or [])
        self.sendfile = bool(data.get("sendfile", True))
        self.rec_content = data.get("rec_content") if data.get("rec_content") in ("source", "output") else "source"
        self.rec_to = data.get("rec_to") if data.get("rec_to") in ("dm", "target", "both") else "both"
        if data.get("playback") in ("fifo", "external", "auto"):
            self.playback_mode = data["playback"]

    async def _changed(self) -> None:
        if self.on_change:
            try:
                await self.on_change()
            except Exception:
                log.exception("persist failed")

    async def _say(self, text: str) -> None:
        if self.notify:
            try:
                await self.notify(text)
            except Exception:
                log.exception("notify failed")

    @property
    def active(self) -> bool:
        return self.recording or bool(self.targets)

    # ---- effects ffmpeg
    async def _start_ffmpeg(self) -> None:
        if not (self.proc and self.proc.returncode is None):
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-fflags", "+nobuffer",
                "-probesize", "32", "-analyzeduration", "0",
                "-f", "s16le", "-ar", str(self.in_sr), "-ac", str(self.in_ch), "-i", "pipe:0",
                "-af", self.effects.filter_chain(),
                "-f", "s16le", "-ar", str(REC_SAMPLE_RATE), "-ac", str(REC_CHANNELS),
                "-flush_packets", "1", "pipe:1",
            ]
            self.proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            self._last_out = time.monotonic()
            self._pump_task = asyncio.create_task(self._pump(self.proc))
        if not self._mix_task or self._mix_task.done():
            self._mix_task = asyncio.create_task(self._mixer())

    async def _kill_proc(self, proc) -> None:
        if proc and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass

    async def _stop_ffmpeg(self) -> None:
        proc, self.proc = self.proc, None
        for t in (self._pump_task, self._mix_task):
            if t and not t.done() and t is not asyncio.current_task():
                t.cancel()
        self._pump_task = self._mix_task = None
        await self._kill_proc(proc)
        self._streams.clear()

    async def _restart_ffmpeg(self) -> None:
        old, old_pump = self.proc, self._pump_task
        self.proc = None
        if old_pump and not old_pump.done():
            old_pump.cancel()
        await self._kill_proc(old)
        if self.active:
            await self._start_ffmpeg()

    # ---- ingest: frames from py-tgcalls
    async def on_update(self, update, role: Optional[str] = None) -> None:
        frames = getattr(update, "frames", None)
        if frames is not None:
            if role in (None, "rec"):
                self._ingest(update, frames)
            return
        await self._on_event(update, role)

    def _ingest(self, update, frames) -> None:
        if not self.recording or self.source is None:
            return
        if getattr(update, "chat_id", None) != self.source:
            return
        if "outgoing" in str(getattr(update, "direction", "")).lower():
            return
        dev = str(getattr(update, "device", "")).lower()
        if "camera" in dev or "screen" in dev:
            return
        now, st = time.monotonic(), self.stats
        st["updates"] += 1
        per_update: Dict[int, int] = {}
        for fr in frames:
            raw = getattr(fr, "frame", None) or getattr(fr, "data", None)
            if not raw:
                continue
            ssrc = getattr(fr, "ssrc", 0)
            s = self._streams.get(ssrc)
            if s is None:
                s = self._streams[ssrc] = {"buf": bytearray(), "primed": False, "last": now,
                                           "lu": None, "acc_b": 0, "acc_t": 0.0}
            s["buf"] += raw
            per_update[ssrc] = per_update.get(ssrc, 0) + len(raw)
            st["frames"] += 1
            st["bytes"] += len(raw)
            st["sizes"][len(raw)] += 1
            st["ssrcs"].add(ssrc)
            if len(raw) >= 2:
                st["peak"] = max(st["peak"], int(np.abs(np.frombuffer(raw[: len(raw) // 2 * 2], dtype=np.int16)).max()))
        for ssrc, nbytes in per_update.items():
            s = self._streams[ssrc]
            # measure the real byte rate of this speaker (only over continuous stretches) -> detect the format
            if s["lu"] is not None and 0 < now - s["lu"] < 0.12:
                s["acc_b"] += nbytes
                s["acc_t"] += now - s["lu"]
                if s["acc_t"] >= 1.5:
                    self._detect(s["acc_b"] / s["acc_t"])
                    s["acc_b"], s["acc_t"] = 0, 0.0
            elif s["lu"] is not None and now - s["lu"] >= 0.12:
                s["acc_b"], s["acc_t"] = 0, 0.0
            s["lu"] = s["last"] = now
        if not st["first"]:
            st["first"] = now
        st["last"] = now

    def _detect(self, rate: float) -> None:
        """Work out the real PCM format from the measured byte rate and convert automatically."""
        errs = sorted((abs(sr * ch * 2 / rate - 1), sr, ch) for sr, ch in KNOWN_FORMATS)
        self.stats["last_rate"] = int(rate)
        if errs[0][0] > 0.15:
            self.stats["detect_fail"] += 1
            return
        near = sorted((e for e in errs if e[0] <= errs[0][0] + 0.04), key=lambda e: (e[1] != 48000, e[0]))
        _, sr, ch = near[0]
        if (sr, ch) == (self.in_sr, self.in_ch):
            self.fmt_detected, self._fmt_vote = True, None
        elif self._fmt_vote == (sr, ch):                 # same answer twice in a row: trust it
            self._fmt_vote = None
            log.warning("source audio is %s Hz x %s ch (assumed %s x %s) - converting", sr, ch, self.in_sr, self.in_ch)
            self._apply_task = asyncio.get_running_loop().create_task(self._apply_fmt(sr, ch))
        else:
            self._fmt_vote = (sr, ch)

    async def _apply_fmt(self, sr: int, ch: int) -> None:
        old = (self.in_sr, self.in_ch)
        self.in_sr, self.in_ch, self.fmt_detected = sr, ch, True
        self._streams.clear()
        await self._restart_ffmpeg()
        if self.rec_proc and self.rec_content == "source":     # file was started with the wrong format
            old_proc, old_path = self.rec_proc, self.rec_path
            self.rec_proc = None
            await self._kill_proc(old_proc)
            try:
                os.remove(old_path)
            except OSError:
                pass
            await self._start_rec_file()
        await self._say(f"🎚 The SOURCE voice chat audio is really <b>{sr} Hz, {'mono' if ch == 1 else 'stereo'}</b> "
                        f"(I assumed {old[0]} Hz, {'mono' if old[1] == 1 else 'stereo'}). Converted automatically.")

    def _mix_chunk(self) -> bytes:
        """Exactly one 20 ms chunk (input format): all speakers mixed, each from its own buffer."""
        now, parts = time.monotonic(), []
        chunk, align, pre = self.in_chunk, self.in_align, self.prebuffer
        for ssrc, s in list(self._streams.items()):
            buf = s["buf"]
            if len(buf) > self.max_buffer:                 # too far behind: skip ahead, stay live
                drop = len(buf) - pre
                del buf[: drop - drop % align]
            if not s["primed"]:
                if len(buf) >= pre:
                    s["primed"] = True
                else:
                    if now - s["last"] > 3:
                        del self._streams[ssrc]
                    continue
            n = min(len(buf), chunk)
            n -= n % align
            if n <= 0:
                s["primed"] = False
                continue
            parts.append(np.frombuffer(bytes(buf[:n]), dtype=np.int16))
            del buf[:n]
        if not parts:
            return bytes(chunk)
        if len(parts) == 1 and len(parts[0]) * 2 == chunk:
            return parts[0].tobytes()
        mix = np.zeros(chunk // 2, dtype=np.int32)
        for p in parts:
            mix[: len(p)] += p
        return np.clip(mix, -32768, 32767).astype(np.int16).tobytes()

    def _tone_chunk(self) -> bytes:
        t = self._tone
        n = self.in_chunk // self.in_align
        x = (np.sin(2 * np.pi * 440 * (t["pos"] + np.arange(n)) / self.in_sr) * 9000).astype(np.int16)
        t["pos"] += n
        t["left"] -= 1
        if t["left"] <= 0:
            self._tone = None
        return np.repeat(x, self.in_ch).tobytes()

    async def _mixer(self) -> None:
        """Constant 20 ms clock: audio is never inserted/removed mid-stream, so no crackle."""
        next_t, ticks = time.monotonic(), 0
        try:
            while True:
                next_t += TICK
                delay = next_t - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                elif delay < -0.25:
                    next_t = time.monotonic()
                chunk = self._mix_chunk()
                if self.rec_content == "source":
                    self._write_rec(chunk)                  # saved file = raw original, never muted
                if self._sample and self._sample["kind"] == "source":
                    self._sample["buf"] += chunk
                feed = bytes(len(chunk)) if self.muted else chunk
                if self._tone:
                    feed = self._tone_chunk()               # /tonetest: playback check, independent of capture
                self._feed(feed)
                ticks += 1
                if ticks % 50 == 0 and self.active:
                    dead = not (self.proc and self.proc.returncode is None)
                    stalled = time.monotonic() - self._last_out > 2.5
                    if dead or stalled:
                        log.warning("effects ffmpeg %s - restarting", "died" if dead else "stalled")
                        self.stats["restarts"] += 1
                        await self._restart_ffmpeg()
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("mixer crashed")

    def _feed(self, data: bytes) -> None:
        proc = self.proc
        if not proc or proc.returncode is not None or proc.stdin is None:
            return
        try:
            if proc.stdin.transport.get_write_buffer_size() > 200_000:
                return
            proc.stdin.write(data)
        except Exception:
            pass

    async def _pump(self, proc: asyncio.subprocess.Process) -> None:
        """Processed audio (with effects) -> every target."""
        try:
            while True:
                data = await proc.stdout.readexactly(CHUNK)
                self._last_out = time.monotonic()
                if self.rec_content == "output":
                    self._write_rec(data)
                if self._sample and self._sample["kind"] == "output":
                    self._sample["buf"] += data
                for chat_id, t in list(self.targets.items()):
                    await self._deliver(chat_id, t, data)
        except (asyncio.IncompleteReadError, asyncio.CancelledError):
            return
        except Exception:
            log.exception("pump crashed")

    async def _deliver(self, chat_id: int, t: dict, data: bytes) -> None:
        if chat_id in self._leaving:
            return
        if t["mode"] == "external":
            try:
                await t["call"].send_frame(chat_id, t["device"], data)
                t["errs"] = 0
            except Exception as exc:
                t["errs"] += 1
                if t["errs"] in (1, 100):
                    log.warning("send_frame(%s) failed: %s", chat_id, exc)
                if t["errs"] == 100:
                    self._schedule_recover(chat_id, "bc", "playback errors")
        else:
            try:
                os.write(t["fd"], data)
            except BlockingIOError:
                self.stats["dropped_out"] += 1
            except OSError:
                pass

    # ---- recording file
    def _write_rec(self, data: bytes) -> None:
        p = self.rec_proc
        if not p or p.returncode is not None or p.stdin is None:
            return
        try:
            p.stdin.write(data)
        except Exception:
            pass

    async def _start_rec_file(self) -> None:
        if self.rec_proc and self.rec_proc.returncode is None:
            return
        os.makedirs(REC_DIR, exist_ok=True)
        self.rec_path = os.path.join(REC_DIR, f"rec_{self.uid}_{int(time.time())}.{FILE_EXT}")
        self.rec_started = time.time()
        sr, ch = (self.in_sr, self.in_ch) if self.rec_content == "source" else (REC_SAMPLE_RATE, REC_CHANNELS)
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "s16le", "-ar", str(sr), "-ac", str(ch), "-i", "pipe:0",
               "-c:a", FILE_CODEC, "-b:a", "96k", self.rec_path]
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
            await self._kill_proc(proc)
        if os.path.exists(path) and os.path.getsize(path) > 2000:
            return {"path": path, "seconds": int(time.time() - self.rec_started), "content": self.rec_content}
        try:
            os.remove(path)
        except OSError:
            pass
        return None

    # ---- diagnostics
    async def capture_sample(self, seconds: float = 5, kind: str = "source") -> Optional[str]:
        """Capture a few seconds of raw (source) or processed (output) audio into a WAV for listening."""
        if not self.recording:
            raise ValueError("Not live. Press Go Live first.")
        self._sample = {"kind": kind, "buf": bytearray()}
        await asyncio.sleep(seconds + 0.3)
        buf, self._sample = self._sample["buf"], None
        if len(buf) < CHUNK * 5:
            return None
        path = os.path.join(REC_DIR, f"sample_{self.uid}_{int(time.time())}.wav")
        os.makedirs(REC_DIR, exist_ok=True)
        sr, ch = (self.in_sr, self.in_ch) if kind == "source" else (REC_SAMPLE_RATE, REC_CHANNELS)
        with wave.open(path, "wb") as w:
            w.setnchannels(ch)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(bytes(buf))
        return path

    def debug(self) -> dict:
        st, now = self.stats, time.monotonic()
        dur = (st["last"] - st["first"]) if st["first"] else 0
        common = st["sizes"].most_common(1)
        return {
            "recording": self.recording, "updates": st["updates"], "frames": st["frames"], "bytes": st["bytes"],
            "speakers_seen": len(st["ssrcs"]), "speakers_now": len(self._streams), "peak": st["peak"],
            "last_age": (now - st["last"]) if st["last"] else None,
            "frame_size": common[0][0] if common else None,
            "rate_bps": (st["bytes"] / dur) if dur > 2 else None,
            "buffer_ms": int(max([len(s["buf"]) for s in self._streams.values()] or [0]) / BPS * 1000),
            "dropped_out": st["dropped_out"], "restarts": st["restarts"], "recoveries": st["recoveries"],
            "ffmpeg": bool(self.proc and self.proc.returncode is None),
            "modes": {c: t["mode"] for c, t in self.targets.items()},
            "in_fmt": (self.in_sr, self.in_ch), "detected": self.fmt_detected, "measured_rate": st["last_rate"],
            "detect_fail": st["detect_fail"], "playback": self.playback_mode,
        }

    # ---- call events / auto-recovery
    async def _on_event(self, update, role) -> None:
        name = type(update).__name__
        chat_id = getattr(update, "chat_id", None)
        if chat_id is None:
            return
        if name == "ChatUpdate":
            status = str(getattr(update, "status", "")).lower()
            if not any(k in status for k in ("kick", "left", "closed", "discard", "ban")):
                return
            reason = status.replace("status.", "")
        elif name == "StreamEnded" and role == "bc":
            reason = "stream ended"
        else:
            return
        self._schedule_recover(chat_id, role, reason)

    def _schedule_recover(self, chat_id: int, role: Optional[str], reason: str) -> None:
        if chat_id in self._leaving or chat_id in self._recovering:
            return
        if role == "rec":
            if not (self.recording and chat_id == self.source):
                return
        elif chat_id not in self.targets:
            return
        self._recovering.add(chat_id)
        asyncio.get_running_loop().create_task(self._recover(chat_id, role, reason))

    async def _safe_leave(self, side, chat_id: int) -> None:
        self._leaving.add(chat_id)
        try:
            await side.call.leave_call(chat_id)
        except Exception as exc:
            log.debug("leave_call(%s): %s", chat_id, exc)
        finally:
            asyncio.get_running_loop().call_later(2.0, self._leaving.discard, chat_id)

    async def _recover(self, chat_id: int, role: str, reason: str) -> None:
        await self._say(f"⚠️ Voice chat <code>{chat_id}</code> disconnected ({reason}). Reconnecting…")
        try:
            for wait in (3, 6, 12, 24, 48):
                await asyncio.sleep(wait)
                try:
                    if role == "rec":
                        if not (self.recording and chat_id == self.source):
                            return
                        side = await self.rt.ensure("rec")
                        await self._safe_leave(side, chat_id)
                        await asyncio.sleep(1)
                        await side.call.record(chat_id, self._record_stream())
                    else:
                        if chat_id not in self.targets:
                            return
                        side = await self.rt.ensure("bc")
                        await self._safe_leave(side, chat_id)
                        self._drop_target(chat_id)
                        await asyncio.sleep(1)
                        async with self._lock:
                            self.targets[chat_id] = await self._open_target(side, chat_id)
                    self.stats["recoveries"] += 1
                    await self._say(f"✅ Reconnected to <code>{chat_id}</code>.")
                    return
                except Exception as exc:
                    log.warning("recover %s attempt failed: %s", chat_id, exc)
            if role == "rec":
                self.recording = False
                info = await self._finish_rec_file()
                if info:
                    try:
                        os.remove(info["path"])
                    except OSError:
                        pass
            else:
                self._drop_target(chat_id)
            await self._say(f"❌ Could not reconnect to <code>{chat_id}</code>. Make sure a voice chat is running "
                            "there, then press 🚀 Go Live.")
        finally:
            self._recovering.discard(chat_id)

    # ---- py-tgcalls glue
    @staticmethod
    def _record_stream():
        from pytgcalls.types import AudioQuality, RecordStream
        try:
            return RecordStream(audio=True, audio_parameters=audio_params())
        except Exception:
            return RecordStream(audio=True, audio_parameters=AudioQuality.HIGH)

    async def _open_target(self, side, chat_id: int) -> dict:
        """Join the target VC. Raw frames if supported (best quality), else WAV FIFO."""
        from pytgcalls.types import MediaStream

        params = audio_params()
        if self.playback_mode in ("auto", "external"):
            joined = False
            api_err = (ImportError, AttributeError, TypeError, NameError)
            try:
                from pytgcalls.types import Device, ExternalMedia
                await side.call.play(chat_id, MediaStream(ExternalMedia.AUDIO, params,
                                                           video_flags=MediaStream.Flags.IGNORE))
                joined = True
                await side.call.send_frame(chat_id, Device.MICROPHONE, bytes(CHUNK))
                return {"mode": "external", "call": side.call, "device": Device.MICROPHONE, "errs": 0}
            except api_err as exc:
                log.info("raw-frame playback unavailable (%s) - using FIFO", exc)
                if self.playback_mode == "external":
                    raise
            except Exception as exc:
                if not joined or self.playback_mode == "external":
                    raise
                log.warning("raw-frame playback failed (%s) - using FIFO", exc)
            if joined:
                await self._safe_leave(side, chat_id)
                await asyncio.sleep(1)

        path = os.path.join(FIFO_DIR, f"{self.uid}_{abs(chat_id)}.wav")
        if os.path.exists(path):
            os.remove(path)
        os.mkfifo(path)
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        os.write(fd, wav_header(REC_SAMPLE_RATE, REC_CHANNELS))
        try:
            await side.call.play(chat_id, MediaStream(path, params, video_flags=MediaStream.Flags.IGNORE))
        except Exception:
            os.close(fd)
            os.remove(path)
            raise
        return {"mode": "fifo", "path": path, "fd": fd}

    def _drop_target(self, chat_id: int) -> None:
        t = self.targets.pop(chat_id, None)
        if t and t["mode"] == "fifo":
            try:
                os.close(t["fd"])
            except OSError:
                pass
            try:
                os.remove(t["path"])
            except OSError:
                pass

    # ---- first-audio watcher + playback tone test
    async def _audio_watch(self) -> None:
        t0, warned = time.monotonic(), False
        try:
            while self.recording and time.monotonic() - t0 < 150:
                await asyncio.sleep(1)
                if self.stats["frames"] > 0:
                    await asyncio.sleep(WATCH_SETTLE)           # let format detection settle
                    if not self.recording:
                        return
                    d = self.debug()
                    kind = "mono" if self.in_ch == 1 else "stereo"
                    out = ("✅ <b>Receiving audio</b> from the SOURCE voice chat\n"
                           f"🎚 Format: {self.in_sr} Hz {kind} ({'confirmed' if self.fmt_detected else 'assumed'}) · "
                           f"speakers: {d['speakers_seen']} · level: {d['peak']}/32767")
                    if d["peak"] < 50:
                        out += "\n⚠️ Only silence so far — is someone actually talking?"
                    out += "\nSounds wrong in the target? Run /tonetest (tests playback only) and /sample (what was captured)."
                    await self._say(out)
                    return
                if not warned and time.monotonic() - t0 > WATCH_WARN:
                    warned = True
                    await self._say(
                        f"⚠️ <b>No audio received</b> from the SOURCE voice chat for {int(WATCH_WARN)} s.\n"
                        "• Is someone talking in that voice chat <b>with a different account</b> (not the Source or "
                        "Target account)? The Source account can't hear itself.\n"
                        "• Is the Source account really in that voice chat? Run /check.\n"
                        "I'll keep listening and tell you when audio arrives.")
        except asyncio.CancelledError:
            return

    async def tone_test(self, seconds: float = 5) -> None:
        """Play a 440 Hz tone through the whole output path (effects included) into the target VC(s)."""
        if not self.targets:
            raise ValueError("No target connected. Press Go Live (or /join) first.")
        self._tone = {"pos": 0, "left": int(seconds * 50)}
        await asyncio.sleep(seconds + 0.5)

    # ---- controls
    async def start_record(self, chat_id: int) -> None:
        async with self._lock:
            if chat_id in self.targets or chat_id in self.saved_targets:
                raise ValueError("The SOURCE and a TARGET can't be the same chat. Remove it from targets first.")
            side = await self.rt.ensure("rec")
            self.source = chat_id
            self.recording = True
            self._streams.clear()
            self._reset_stats()
            await self._start_ffmpeg()
            await self._start_rec_file()
            try:
                await side.call.record(chat_id, self._record_stream())
            except Exception:
                self.recording = False
                await self._finish_rec_file()
                if not self.targets:
                    await self._stop_ffmpeg()
                raise
            if self._watch_task and not self._watch_task.done():
                self._watch_task.cancel()
            self._watch_task = asyncio.get_running_loop().create_task(self._audio_watch())
        await self._changed()

    async def join(self, chat_id: int) -> None:
        async with self._lock:
            if chat_id == self.source:
                raise ValueError("This chat is your SOURCE. The target must be a different group.")
            if chat_id in self.targets:
                raise ValueError("Already forwarding to this chat.")
            side = await self.rt.ensure("bc")
            await self._start_ffmpeg()
            try:
                self.targets[chat_id] = await self._open_target(side, chat_id)
            except Exception:
                if not self.active:
                    await self._stop_ffmpeg()
                raise
            if chat_id not in self.saved_targets:
                self.saved_targets.append(chat_id)
        await self._changed()

    async def leave(self, chat_id: int, forget: bool = True) -> None:
        async with self._lock:
            if chat_id not in self.targets and chat_id not in self.saved_targets:
                raise ValueError("Not forwarding to that chat.")
            if chat_id in self.targets:
                bc = self.rt.sides.get("bc")
                if bc:
                    await self._safe_leave(bc, chat_id)
                self._drop_target(chat_id)
            if forget and chat_id in self.saved_targets:
                self.saved_targets.remove(chat_id)
            if not self.active:
                await self._stop_ffmpeg()
        await self._changed()

    async def leave_record(self) -> Optional[dict]:
        """Stops recording. Returns {"path","seconds","content"} of the saved recording, if any."""
        async with self._lock:
            if not self.recording or self.source is None:
                raise ValueError("Not recording.")
            rec = self.rt.sides.get("rec")
            if self._watch_task and not self._watch_task.done():
                self._watch_task.cancel()
            if rec:
                await self._safe_leave(rec, self.source)
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
        """Reconnect saved source + targets. Returns (ok_labels, [(label, exception)])."""
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

    async def add_target(self, chat_id: int) -> None:
        if chat_id == self.source:
            raise ValueError("This chat is your SOURCE. The target must be a different group.")
        if chat_id not in self.saved_targets:
            self.saved_targets.append(chat_id)
        await self._changed()

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

    async def set_option(self, **opts) -> None:
        for k, v in opts.items():
            setattr(self, k, v)
        await self._changed()

    async def shutdown(self) -> None:
        bc, rec = self.rt.sides.get("bc"), self.rt.sides.get("rec")
        for chat_id in list(self.targets):
            if bc:
                await self._safe_leave(bc, chat_id)
            self._drop_target(chat_id)
        if self.recording and self.source is not None and rec:
            await self._safe_leave(rec, self.source)
        self.recording = False
        await self._finish_rec_file()
        await self._stop_ffmpeg()
