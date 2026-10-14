"""Screen capture for the desktop on DISPLAY :1, run inside the sandbox.

python deskctl.py shot              -> one PNG of the full screen, base64, on stdout
python deskctl.py stream FPS        -> one JPEG frame per line ("F <base64>"), or "S" when
                                       the screen has not changed, until killed
"""

from __future__ import annotations

import base64
import hashlib
import io
import sys
import time

from PIL import ImageGrab

DISPLAY = ":1"


def grab_png() -> bytes:
    buf = io.BytesIO()
    ImageGrab.grab(xdisplay=DISPLAY).save(buf, format="PNG", optimize=False, compress_level=3)
    return buf.getvalue()


def stream(fps: float) -> None:
    period = 1.0 / fps
    last = b""
    while True:
        t0 = time.monotonic()
        img = ImageGrab.grab(xdisplay=DISPLAY).convert("RGB")
        digest = hashlib.blake2b(img.tobytes(), digest_size=16).digest()
        if digest == last:
            sys.stdout.write("S\n")
        else:
            last = digest
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=70)
            sys.stdout.write("F " + base64.b64encode(buf.getvalue()).decode() + "\n")
        sys.stdout.flush()
        time.sleep(max(0.0, period - (time.monotonic() - t0)))


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "shot":
        sys.stdout.write(base64.b64encode(grab_png()).decode())
    elif cmd == "stream":
        stream(float(sys.argv[2]) if len(sys.argv) > 2 else 2.0)
    else:
        sys.exit(f"usage: deskctl.py shot | stream FPS (got {cmd!r})")


if __name__ == "__main__":
    main()
