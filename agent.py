"""The computer-use agent loop: Claude's computer toolset, executed in the desktop sandbox.

The loop runs in the server, never in the sandbox. Each `tool_use` block from the computer
toolset is one action (its `name` is the member: screenshot, left_click, type, ...); actions
become xdotool commands run inside the sandbox, and screenshots come back as PNGs.
"""

from __future__ import annotations

import base64
import io
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import anthropic
from chalkcompute import Sandbox
from PIL import Image as PILImage

from desktop_sandbox import SCREEN, screenshot_png, xdotool

TOOLSET = {"type": "computer_toolset_20260801"}
NOT_EXECUTED = "Not executed: an earlier computer action in this turn failed."
DOCUMENTS = "/root/Documents"

SYSTEM_PROMPT = f"""\
You are operating a Linux desktop (GNOME, {SCREEN[0]}x{SCREEN[1]}) inside an isolated sandbox \
with no internet access. Apache OpenOffice 4 is open at its Start Center: Text Document (Writer), \
Spreadsheet (Calc), Presentation (Impress) and Drawing are one click away.

Complete the user's task with OpenOffice, working the way a careful person would at this desktop.
- Save the finished document in {DOCUMENTS} with a short, descriptive file name. In the Save As \
dialog, type the full path (for example {DOCUMENTS}/budget.ods) into the file name box and press \
Enter. Keep the default OpenOffice format unless the task asks for another one; if it asks for a \
PDF, also use File > Export as PDF into {DOCUMENTS}.
- If a dialog asks whether to keep the current format, keep it.
- This OpenOffice build shows "Error saving the document ...: General Error" after the first save of a new document even though the file was written. Click OK and carry on; there is no need to save again.
- Stay on the task: don't open other applications or browse menus you don't need.
- End each group of actions with a screenshot so you can check the result before continuing.
- When the document is saved and correct, reply with one or two sentences describing what you \
made, and stop using tools.
"""

# Claude's key names follow xdotool's keysyms except for a few common aliases.
KEY_ALIASES = {
    "enter": "Return",
    "return": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "cmd": "super",
    "command": "super",
    "win": "super",
    "meta": "super",
    "backspace": "BackSpace",
    "del": "Delete",
    "pgup": "Prior",
    "pgdn": "Next",
    "page_up": "Prior",
    "page_down": "Next",
    "control": "ctrl",
}
SCROLL_BUTTONS = {"up": "4", "down": "5", "left": "6", "right": "7"}
MAX_WAIT_SECS = 5.0


class ActionError(Exception):
    """An action Claude asked for that could not be carried out; reported back as is_error."""


def xdo_keys(combo: str) -> str:
    parts = [KEY_ALIASES.get(p.strip().lower(), p.strip()) for p in combo.split("+") if p.strip()]
    if not parts:
        raise ActionError("empty key")
    return "+".join(parts)


def coordinate(value: Any, name: str = "coordinate") -> tuple[str, str]:
    if not (isinstance(value, list) and len(value) == 2 and all(isinstance(v, int | float) for v in value)):
        raise ActionError(f"{name} must be [x, y]")
    x, y = int(value[0]), int(value[1])
    if not (0 <= x < SCREEN[0] and 0 <= y < SCREEN[1]):
        raise ActionError(f"{name} {x},{y} is outside the {SCREEN[0]}x{SCREEN[1]} screen")
    return str(x), str(y)


def modifiers(text: Any) -> list[str]:
    if not text:
        return []
    return [xdo_keys(m) for m in str(text).split("+")]


def png_block(png: bytes) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(png).decode()},
    }


def zoom_png(png: bytes, region: Any) -> bytes:
    if not (isinstance(region, list) and len(region) == 4):
        raise ActionError("region must be [x0, y0, x1, y1]")
    x0, y0, x1, y1 = (int(v) for v in region)
    if not (0 <= x0 < x1 <= SCREEN[0] and 0 <= y0 < y1 <= SCREEN[1]):
        raise ActionError("region is outside the screen or empty")
    crop = PILImage.open(io.BytesIO(png)).crop((x0, y0, x1, y1))
    scale = min(SCREEN[0] / crop.width, SCREEN[1] / crop.height)
    crop = crop.resize((max(1, int(crop.width * scale)), max(1, int(crop.height * scale))), PILImage.LANCZOS)
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    return buf.getvalue()


def execute(sandbox: Sandbox, name: str, inp: dict[str, Any]) -> list[dict[str, Any]] | str:
    """Runs one computer-toolset member. Returns tool_result content."""
    if name == "screenshot":
        return [png_block(screenshot_png(sandbox))]
    if name == "zoom":
        return [png_block(zoom_png(screenshot_png(sandbox), inp.get("region")))]
    if name in ("left_click", "right_click", "middle_click", "double_click", "triple_click"):
        button = {"right_click": "3", "middle_click": "2"}.get(name, "1")
        repeat = {"double_click": "2", "triple_click": "3"}.get(name, "1")
        mods = modifiers(inp.get("text"))
        args: list[str] = []
        if inp.get("coordinate") is not None:
            args += ["mousemove", *coordinate(inp["coordinate"])]
        for m in mods:
            args += ["keydown", m]
        args += ["click", "--repeat", repeat, "--delay", "80", button]
        for m in reversed(mods):
            args += ["keyup", m]
        # Let focus settle before the next action, which is often typing into what was clicked.
        args += ["sleep", "0.2"]
        xdotool(sandbox, *args)
        return "OK"
    if name == "left_click_drag":
        mods = modifiers(inp.get("text"))
        args = ["mousemove", *coordinate(inp.get("start_coordinate"), "start_coordinate")]
        for m in mods:
            args += ["keydown", m]
        args += ["mousedown", "1", "mousemove", *coordinate(inp.get("coordinate")), "mouseup", "1"]
        for m in reversed(mods):
            args += ["keyup", m]
        xdotool(sandbox, *args)
        return "OK"
    if name == "mouse_move":
        xdotool(sandbox, "mousemove", *coordinate(inp.get("coordinate")))
        return "OK"
    if name == "left_mouse_down":
        xdotool(sandbox, "mousedown", "1")
        return "OK"
    if name == "left_mouse_up":
        xdotool(sandbox, "mouseup", "1")
        return "OK"
    if name == "cursor_position":
        out = sandbox.exec("xdotool", "getmouselocation", "--shell", env={"DISPLAY": ":1"}, timeout_secs=10)
        loc = dict(line.split("=", 1) for line in out.stdout_text.split() if "=" in line)
        return f"X={loc.get('X', '?')}, Y={loc.get('Y', '?')}"
    if name == "scroll":
        direction = inp.get("scroll_direction")
        if direction not in SCROLL_BUTTONS:
            raise ActionError("scroll_direction must be up, down, left or right")
        amount = max(1, min(int(inp.get("scroll_amount") or 3), 30))
        mods = modifiers(inp.get("text"))
        args = []
        if inp.get("coordinate") is not None:
            args += ["mousemove", *coordinate(inp["coordinate"])]
        for m in mods:
            args += ["keydown", m]
        args += ["click", "--repeat", str(amount), "--delay", "40", SCROLL_BUTTONS[direction]]
        for m in reversed(mods):
            args += ["keyup", m]
        xdotool(sandbox, *args)
        return "OK"
    if name == "type":
        text = inp.get("text")
        if not isinstance(text, str) or not text:
            raise ActionError("text must be a non-empty string")
        # Newlines go through `key Return` so multi-line text works in every widget.
        for i, line in enumerate(text.split("\n")):
            if i:
                xdotool(sandbox, "key", "Return")
            if line:
                xdotool(sandbox, "type", "--delay", "12", "--", line, timeout_secs=120)
        return "OK"
    if name == "key":
        repeat = max(1, min(int(inp.get("repeat") or 1), 100))
        xdotool(sandbox, "key", "--repeat", str(repeat), "--delay", "40", xdo_keys(str(inp.get("text", ""))))
        return "OK"
    if name == "hold_key":
        keys = xdo_keys(str(inp.get("text", "")))
        duration = max(0.0, min(float(inp.get("duration") or 0.5), MAX_WAIT_SECS))
        xdotool(sandbox, "keydown", keys, "sleep", f"{duration:.2f}", "keyup", keys)
        return "OK"
    if name == "wait":
        time.sleep(max(0.0, min(float(inp.get("duration") or 1.0), MAX_WAIT_SECS)))
        return "OK"
    raise ActionError(f"unsupported computer action: {name}")


@dataclass
class AgentConfig:
    model: str
    effort: str
    max_turns: int
    deadline: float  # time.monotonic() value after which the run stops


def describe(name: str, inp: dict[str, Any]) -> str:
    """A short, display-safe description of an action for the step panel."""
    if "coordinate" in inp and isinstance(inp.get("coordinate"), list):
        where = f" at ({inp['coordinate'][0]}, {inp['coordinate'][1]})"
    else:
        where = ""
    if name == "type":
        text = str(inp.get("text", ""))
        return f"type “{text[:60]}{'…' if len(text) > 60 else ''}”"
    if name in ("key", "hold_key"):
        return f"{name.replace('_', ' ')} {inp.get('text', '')}"
    if name == "scroll":
        return f"scroll {inp.get('scroll_direction', '')} ×{inp.get('scroll_amount', '')}{where}"
    if name == "left_click_drag":
        return f"drag {inp.get('start_coordinate')} → {inp.get('coordinate')}"
    return name.replace("_", " ") + where


def thumbnail(png: bytes, width: int = 320) -> str:
    img = PILImage.open(io.BytesIO(png)).convert("RGB")
    img = img.resize((width, int(img.height * width / img.width)), PILImage.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return base64.b64encode(buf.getvalue()).decode()


def run_agent(
    client: anthropic.Anthropic,
    sandbox: Sandbox,
    task: str,
    cfg: AgentConfig,
    should_stop: Callable[[], bool],
) -> Iterator[dict[str, Any]]:
    """Drives Claude against the sandbox desktop, yielding viewer events."""
    messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
    action_count = 0
    for turn in range(1, cfg.max_turns + 1):
        if should_stop():
            yield {"t": "agent_end", "reason": "stopped"}
            return
        if time.monotonic() > cfg.deadline:
            yield {"t": "agent_end", "reason": "time_limit"}
            return
        t0 = time.monotonic()
        response = client.messages.create(
            model=cfg.model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            tools=[TOOLSET],
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": cfg.effort},
            # Screenshots pile up in the history; caching keeps each turn's re-read cheap.
            cache_control={"type": "ephemeral"},
            messages=messages,
        )
        model_secs = time.monotonic() - t0
        content = [block.model_dump(exclude_none=True) for block in response.content]
        messages.append({"role": "assistant", "content": content})

        notes = [b.get("thinking", "") for b in content if b["type"] == "thinking" and b.get("thinking")]
        notes += [b["text"] for b in content if b["type"] == "text" and b.get("text")]
        usage = response.usage
        yield {
            "t": "turn",
            "turn": turn,
            "model_secs": round(model_secs, 1),
            "note": " ".join(n.strip() for n in notes)[-600:],
            "input_tokens": usage.input_tokens,
            "cache_read_tokens": usage.cache_read_input_tokens or 0,
            "output_tokens": usage.output_tokens,
        }
        if response.stop_reason == "refusal":
            yield {"t": "agent_end", "reason": "refusal"}
            return

        tool_uses = [b for b in content if b["type"] == "tool_use"]
        if not tool_uses:
            final = " ".join(b["text"] for b in content if b["type"] == "text").strip()
            yield {"t": "agent_end", "reason": "done", "summary": final[:600]}
            return

        results: list[dict[str, Any]] = []
        failed = False
        for block in tool_uses:
            result: dict[str, Any] = {"type": "tool_result", "tool_use_id": block["id"], "toolset_name": "computer"}
            name, inp = block["name"], block.get("input") or {}
            if block.get("toolset_name") != "computer":
                result.pop("toolset_name")
                result.update(is_error=True, content=f"Unknown tool {name}")
                results.append(result)
                continue
            if failed:
                result.update(is_error=True, content=NOT_EXECUTED)
                results.append(result)
                continue
            action_count += 1
            event: dict[str, Any] = {
                "t": "action",
                "n": action_count,
                "turn": turn,
                "name": name,
                "label": describe(name, inp),
            }
            try:
                out = execute(sandbox, name, inp)
                result["content"] = out
                if isinstance(out, list):
                    data = base64.b64decode(out[0]["source"]["data"])
                    event["thumb"] = thumbnail(data)
            except (ActionError, RuntimeError, ValueError, TypeError) as e:
                failed = True
                result.update(is_error=True, content=f"Error: {e}")
                event["error"] = str(e)[:200]
            results.append(result)
            yield event
        messages.append({"role": "user", "content": results})
    yield {"t": "agent_end", "reason": "turn_limit"}
