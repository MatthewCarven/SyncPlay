"""Generate short test tracks into ./music — no copyrighted audio needed.

Sharp-attack pulse trains are ideal for sync testing: easy to hear echo on,
easy to align in a waveform editor when measuring node-to-node skew.

Usage: python tools/make_test_tones.py [outdir]
"""

import math
import struct
import sys
import wave
from pathlib import Path

RATE = 44100


def render(path: Path, seconds: float, freq: float, beep_every: float,
           beep_len: float = 0.1, amp: float = 0.5) -> None:
    frames = bytearray()
    for i in range(int(seconds * RATE)):
        t = i / RATE
        p = t % beep_every
        if p < beep_len:
            env = min(1.0, p / 0.002) * math.exp(-p / 0.03)  # 2ms attack, 30ms decay
            s = amp * env * math.sin(2 * math.pi * freq * t)
        else:
            s = 0.0
        v = int(max(-1.0, min(1.0, s)) * 32767)
        frames += struct.pack("<hh", v, v)  # stereo
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(frames))
    print(f"wrote {path} ({seconds:.0f}s)")


def main() -> None:
    outdir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("music")
    outdir.mkdir(parents=True, exist_ok=True)
    render(outdir / "Test Pulse A (440Hz).wav", 8.0, 440.0, beep_every=0.5)
    render(outdir / "Test Pulse B (660Hz).wav", 8.0, 660.0, beep_every=0.4)


if __name__ == "__main__":
    main()
