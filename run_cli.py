#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx"]
# ///
"""Start a run on a deployed demo server and follow it from the terminal.

./run_cli.py https://<web_url> --preset budget [--out out]
./run_cli.py https://<web_url> --prompt "A packing list spreadsheet with a total weight row"
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--preset")
    group.add_argument("--prompt")
    parser.add_argument("--out", type=Path, default=Path("out"))
    args = parser.parse_args()
    base = args.url.rstrip("/")
    body = {"preset": args.preset} if args.preset else {"prompt": args.prompt}
    with httpx.Client(timeout=httpx.Timeout(30.0, read=None)) as client:
        r = client.post(f"{base}/api/runs", json=body)
        if r.status_code != 200:
            sys.exit(f"{r.status_code}: {r.text}")
        run = r.json()
        print("run", run, flush=True)
        t0 = time.monotonic()
        with client.stream("GET", f"{base}/api/runs/{run['id']}/events") as events:
            for line in events.iter_lines():
                if not line.startswith("data: "):
                    continue
                ev = json.loads(line[6:])
                ev.pop("thumb", None)
                kind = ev.get("t")
                stamp = f"{time.monotonic() - t0:6.1f}s"
                if kind == "action":
                    print(
                        stamp,
                        f"  #{ev['n']:>2} {ev['label']}" + (f"  ERROR {ev['error']}" if ev.get("error") else ""),
                        flush=True,
                    )
                elif kind == "turn":
                    print(
                        stamp,
                        f"turn {ev['turn']} ({ev['model_secs']}s, in={ev['input_tokens']} cache={ev['cache_read_tokens']} out={ev['output_tokens']}) {ev['note'][:200]}",
                        flush=True,
                    )
                else:
                    print(stamp, json.dumps(ev)[:400], flush=True)
                if kind == "end":
                    break
        args.out.mkdir(parents=True, exist_ok=True)
        frame = client.get(f"{base}/api/runs/{run['id']}/frame")
        if frame.status_code == 200:
            (args.out / "last_frame.jpg").write_bytes(frame.content)
        for name in ("preview.png", "preview.pdf"):
            res = client.get(f"{base}/api/runs/{run['id']}/{name}")
            if res.status_code == 200:
                (args.out / name).write_bytes(res.content)
        doc = client.get(f"{base}/api/runs/{run['id']}/document")
        if doc.status_code == 200:
            fname = doc.headers.get("content-disposition", "document").split("filename=")[-1].strip('"')
            (args.out / fname).write_bytes(doc.content)
            print("saved", args.out / fname, len(doc.content), "bytes")


if __name__ == "__main__":
    main()
