"""The desktop sandbox: GNOME (Flashback session) + Apache OpenOffice on Xvfb, under gVisor.

The sandbox has no network access at all and holds no credentials. The agent loop runs in the
server; the sandbox only executes xdotool actions and returns screenshots.
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path

from chalkcompute import ContainerSecurityPolicy, Image, KernelPolicy, NetworkPolicy, Sandbox

HERE = Path(__file__).resolve().parent
OPENOFFICE_VERSION = "4.1.16"
OPENOFFICE_DEBS = (
    f"https://downloads.apache.org/openoffice/{OPENOFFICE_VERSION}/binaries/en-US/"
    + f"Apache_OpenOffice_{OPENOFFICE_VERSION}_Linux_x86-64_install-deb_en-US.tar.gz"
)
DISPLAY_ENV = {"DISPLAY": ":1", "HOME": "/root"}
SCREEN = (1024, 768)


def desktop_image() -> Image:
    return (
        Image.debian_slim(python_version="3.12")
        .apt_install(
            [
                "xvfb",
                "xauth",
                "dbus",
                "dbus-x11",
                "x11-utils",
                "xdotool",
                "gnome-flashback",
                "gnome-panel",
                "metacity",
                "gnome-session",
                "gnome-terminal",
                "nautilus",
                "gnome-themes-extra",
                "adwaita-icon-theme",
                "fonts-dejavu",
                "fonts-liberation",
                "poppler-utils",
                "wmctrl",
                "wget",
                "ca-certificates",
                "procps",
            ]
        )
        .run_commands(
            f"wget -q -O /tmp/aoo.tgz {OPENOFFICE_DEBS}"
            + " && mkdir -p /tmp/aoo && tar xzf /tmp/aoo.tgz -C /tmp/aoo"
            + " && dpkg -i /tmp/aoo/en-US/DEBS/*.deb /tmp/aoo/en-US/DEBS/desktop-integration/*.deb"
            + " && rm -rf /tmp/aoo /tmp/aoo.tgz"
        )
        .pip_install(["pillow==11.3.0"])
        .add_local_file(str(HERE / "desktop" / "start-desktop.sh"), "/opt/desk/start-desktop.sh", strategy="copy")
        .add_local_file(str(HERE / "desktop" / "deskctl.py"), "/opt/desk/deskctl.py", strategy="copy")
        .add_local_file(str(HERE / "desktop" / "docconv.py"), "/opt/desk/docconv.py", strategy="copy")
    )


def make_desktop_sandbox(lifetime_secs: int) -> Sandbox:
    # An empty allowlist is no egress at all: nothing in the sandbox needs the network.
    return Sandbox(
        image=desktop_image(),
        name=f"cu-desktop-{os.urandom(4).hex()}",
        cpu="2",
        memory="4Gi",
        network_policy=NetworkPolicy(allowed_hosts=[]),
        security_policy=ContainerSecurityPolicy(kernel_policy=KernelPolicy.RESTRICTED),
        lifetime=f"{lifetime_secs}s",
    )


def boot_desktop(sandbox: Sandbox, timeout_secs: float = 90.0) -> float:
    """Starts the desktop detached and waits until OpenOffice is up. Returns seconds taken."""
    t0 = time.monotonic()
    # setsid detaches the session so this exec returns while the desktop keeps running.
    sandbox.exec(
        "bash",
        "-c",
        "setsid -f bash /opt/desk/start-desktop.sh >/tmp/desktop.log 2>&1 </dev/null",
        timeout_secs=20,
    )
    while time.monotonic() - t0 < timeout_secs:
        if sandbox.exec("test", "-f", "/tmp/desktop.ready", timeout_secs=10).exit_code == 0:
            return time.monotonic() - t0
        time.sleep(1.0)
    raise TimeoutError("desktop did not become ready")


def screenshot_png(sandbox: Sandbox) -> bytes:
    # A grab can fail transiently while a window is being mapped; one retry covers it.
    for attempt in range(3):
        out = sandbox.exec("python", "/opt/desk/deskctl.py", "shot", env=DISPLAY_ENV, timeout_secs=20)
        if out.exit_code == 0 and out.stdout_text:
            return base64.b64decode(out.stdout_text)
        time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(f"screenshot failed: {out.stderr_text.strip()[-300:] or f'exit {out.exit_code}'}")


def xdotool(sandbox: Sandbox, *args: str, timeout_secs: int = 30) -> None:
    out = sandbox.exec("xdotool", *args, env=DISPLAY_ENV, timeout_secs=timeout_secs)
    if out.exit_code != 0:
        raise RuntimeError(f"xdotool {args[0]} failed: {out.stderr_text.strip()[-300:]}")
