#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["chalkcompute==2.13.5"]
# ///
"""Deploy the computer-use agent demo as a Chalk ScalingGroup.

The server creates the desktop sandboxes with its own workload identity (`chalk_identity=True`),
so no client secret is baked into the image. The model gateway key reaches the server as a
Chalk secret; the desktop sandboxes get no credentials and no network at all.

Requirements on the target environment:
  - the Chalk model gateway (router) is enabled
  - a secret holds a gateway API key, CHALK_ROUTER_API_KEY by default

Credentials for the deploy itself come from `chalk config` in the current directory (or the
CHALK_* env vars):
    ./deploy.py [--env-id <environment>] [--name computer-use-agent]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_FILES = [
    "server.py",
    "agent.py",
    "desktop_sandbox.py",
    "index.html",
    "desktop/start-desktop.sh",
    "desktop/deskctl.py",
    "desktop/docconv.py",
]


def load_chalk_env(env_id: str | None) -> None:
    if "CHALK_CLIENT_ID" not in os.environ:
        cfg = json.loads(subprocess.check_output(["chalk", "config", "--json"]))
        os.environ["CHALK_API_SERVER"] = cfg["apiServer"]["value"]
        os.environ["CHALK_CLIENT_ID"] = cfg["clientId"]["value"]
        os.environ["CHALK_CLIENT_SECRET"] = cfg["clientSecret"]["value"]
        os.environ.setdefault("CHALK_ENVIRONMENT_ID", cfg["environment"]["value"])
    if env_id:
        os.environ["CHALK_ENVIRONMENT_ID"] = env_id


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="computer-use-agent")
    # Defaults to the environment in `chalk config` (or CHALK_ENVIRONMENT_ID).
    parser.add_argument("--env-id", default=None)
    parser.add_argument("--model", default="anthropic/claude-opus-5-5")
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--key-secret", default="CHALK_ROUTER_API_KEY")
    parser.add_argument("--max-concurrent", default="2")
    parser.add_argument("--max-per-hour", default="20")
    parser.add_argument("--warm-pool", default="1")
    parser.add_argument("--ready-timeout", type=float, default=900.0)
    args = parser.parse_args()

    load_chalk_env(args.env_id)
    from chalkcompute import Image, ScalingGroup, Secret

    image = Image.debian_slim(python_version="3.12").pip_install(
        ["chalkcompute==2.13.5", "anthropic==1.14.0", "pillow==11.3.0", "fastapi==0.116.1", "uvicorn[standard]==0.35.0"]
    )
    # The app files total more than the 32 KiB inline `copy` budget, so they ride a volume
    # mounted at /app when the replica starts. Nothing at build time needs them.
    for name in APP_FILES:
        image = image.add_local_file(str(HERE / name), f"/app/{name}", strategy="volume")
    image = image.workdir("/app")

    sg = ScalingGroup(
        image=image,
        name=args.name,
        cpu="1",
        memory="2Gi",
        env={
            # Not secrets: which API the workload identity talks to, and where the gateway lives.
            "CHALK_API_SERVER": os.environ["CHALK_API_SERVER"],
            "CHALK_ENVIRONMENT_ID": os.environ["CHALK_ENVIRONMENT_ID"],
            "PORT": "8080",
            "AGENT_MODEL": args.model,
            "AGENT_EFFORT": args.effort,
            "CU_MAX_CONCURRENT": args.max_concurrent,
            "CU_MAX_PER_HOUR": args.max_per_hour,
            "CU_WARM_POOL": args.warm_pool,
            "ROUTER_API_KEY": Secret.from_chalk_env(args.key_secret),
        },
        entrypoint=["python", "/app/server.py"],
        port=8080,
        chalk_identity=True,
        # Runs, frames, the warm pool and the rate limits live in the replica, and each viewer
        # must reach the replica running its run, so this stays at one.
        min_replicas=1,
        max_replicas=1,
    )
    t0 = time.monotonic()
    try:
        sg.deploy(build_timeout=1200.0, ready_timeout=args.ready_timeout)
    finally:
        print(f"deploy returned after {time.monotonic() - t0:.0f}s")
    print("web_url:", sg.web_url)


if __name__ == "__main__":
    main()
