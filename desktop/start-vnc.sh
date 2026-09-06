#!/bin/bash
set -euo pipefail
rm -f /tmp/.X1-lock /tmp/.X11-unix/X1
mkdir -p /home/cua/.vnc
printf '%s\n%s\n' "${VNC_PW:?VNC_PW is required}" "${VNC_VIEW_PW:?VNC_VIEW_PW is required}" | vncpasswd -f > /home/cua/.vnc/passwd
chmod 0600 /home/cua/.vnc/passwd
unset VNC_PW VNC_VIEW_PW
vncserver :1 -geometry "${VNC_RESOLUTION:-1280x800}" -depth "${VNC_COL_DEPTH:-24}" \
  -rfbport "${VNC_PORT:-5901}" -localhost yes -SecurityTypes VncAuth \
  -rfbauth /home/cua/.vnc/passwd -AlwaysShared -AcceptPointerEvents=0 -AcceptKeyEvents=0 \
  -AllowOverride AcceptKeyEvents,AcceptPointerEvents,AcceptCutText \
  -AcceptCutText=0 -SendCutText -xstartup /usr/local/bin/xstartup.sh
exec tail -F /home/cua/.vnc/*.log
