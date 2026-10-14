#!/bin/bash
# Boots the sandbox desktop: an X display, a GNOME Flashback session (GNOME's X11 session,
# which needs no systemd-logind), and Apache OpenOffice maximized at its Start Center.
# Runs detached; progress goes to /tmp/desktop.log and readiness to /tmp/desktop.ready.
set -u
export DISPLAY=:1
export XDG_SESSION_TYPE=x11 XDG_CURRENT_DESKTOP=GNOME-Flashback:GNOME XDG_RUNTIME_DIR=/tmp/xdg-runtime
export HOME=/root NO_AT_BRIDGE=1
mkdir -p "$XDG_RUNTIME_DIR" /root/Documents && chmod 700 "$XDG_RUNTIME_DIR"

Xvfb :1 -screen 0 1024x768x24 -nolisten tcp +extension RANDR >/tmp/xvfb.log 2>&1 &
for _ in $(seq 100); do xdpyinfo -display :1 >/dev/null 2>&1 && break; sleep 0.1; done

exec dbus-run-session -- bash -c '
  # A screensaver lock or idle blank would hide the desktop from the agent and the viewer.
  gsettings set org.gnome.desktop.session idle-delay 0
  gsettings set org.gnome.desktop.screensaver lock-enabled false
  gsettings set org.gnome.desktop.screensaver idle-activation-enabled false
  metacity --replace >/tmp/metacity.log 2>&1 &
  gnome-flashback --replace >/tmp/flashback.log 2>&1 &
  sleep 1
  gnome-panel --replace >/tmp/panel.log 2>&1 &
  sleep 1
  openoffice4 -nofirststartwizard >/tmp/openoffice.log 2>&1 &
  start_center=$(xdotool search --sync --onlyvisible --name "^OpenOffice$" | head -1)
  sleep 1
  wmctrl -i -r "$start_center" -b add,maximized_vert,maximized_horz
  # Keep document windows ("Untitled 1 - OpenOffice Calc") maximized so the agent always sees
  # the whole document. Dialogs and message boxes never carry " - OpenOffice" in their title.
  ( while true; do
      wmctrl -l | awk "/ - OpenOffice/ {print \$1}" | while read -r id; do
        wmctrl -i -r "$id" -b add,maximized_vert,maximized_horz
      done
      sleep 1
    done ) &
  sleep 2
  touch /tmp/desktop.ready
  wait'
