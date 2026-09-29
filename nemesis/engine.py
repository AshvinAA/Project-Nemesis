"""TCP client for the BuddyDoom AI Director protocol (:31666).

Protocol facts (verified against files/p_ai_llm.c):
- newline-delimited text over loopback TCP; exactly ONE client per listener.
- `observe\n` -> one JSON line (cached <2 tics). Outside a level:
  {"nolevel":true,"monsters":[]}
- `act order=<o> ids=<csv> [x= y= focus= for= after=]\n`, `spawn type=<t>
  count=<n>\n`, `spawn item=medkit|ammo\n`, `director relax\n`, `reset\n`,
  `wake\n`, `nemesis propose=...\n` -> acked `ok\n`.
- Order vocabulary (AI_OrderByName): chase hold fallback flank_left
  flank_right ambush focus_fire use_door.
- Pre-fix note: the engine's listener could wedge after an abrupt client
  disconnect (r<0 path in AI_PollSocket never dropped the dead client).
  Fixed 2026-09-30 in p_ai_llm.c; this client still does clean
  disconnects + reconnects as belt-and-braces.
"""

from __future__ import annotations

import json
import socket
import time
from typing import Any, Optional

from . import config


class DirectorLink:
    """One-client connection to the engine director listener."""

    def __init__(self, host: str = config.HOST, port: int = config.DIRECTOR_PORT) -> None:
        self.host = host
        self.port = port
        self.sock: Optional[socket.socket] = None
        self._buf = b""

    # -- connection lifecycle ------------------------------------------------

    def connect(self) -> bool:
        """Open the socket (non-blocking-ish: short timeouts everywhere)."""
        self.close()
        try:
            s = socket.create_connection((self.host, self.port), timeout=config.SOCKET_TIMEOUT)
        except OSError:
            return False
        s.settimeout(config.SOCKET_TIMEOUT)
        self.sock = s
        self._buf = b""
        return True

    def close(self) -> None:
        if self.sock is not None:
            try:
                # Clean shutdown: half-close then drop, so the engine sees EOF
                # and re-arms its accept loop (pre-fix engines wedge on rude
                # clients; see docs/NEMESIS_RL_PLAN.md Phase 0).
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        self._buf = b""

    @property
    def connected(self) -> bool:
        return self.sock is not None

    def ensure_connected(self) -> bool:
        if self.connected:
            return True
        return self.connect()

    # -- line protocol ---------------------------------------------------------

    def _recv_line(self, deadline: float) -> Optional[str]:
        """Return one newline-terminated reply line, or None on timeout/drop."""
        while True:
            nl = self._buf.find(b"\n")
            if nl >= 0:
                line = self._buf[:nl]
                self._buf = self._buf[nl + 1 :]
                return line.decode("utf-8", errors="replace")
            if time.monotonic() > deadline:
                return None
            try:
                assert self.sock is not None
                chunk = self.sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                self.close()
                return None
            if not chunk:
                self.close()
                return None
            self._buf += chunk

    def request(self, line: str, want_reply: bool = True) -> Optional[str]:
        """Send one protocol line and return the reply line (or None)."""
        if not self.ensure_connected():
            return None
        assert self.sock is not None
        try:
            self.sock.sendall((line.rstrip("\n") + "\n").encode("utf-8"))
        except OSError:
            self.close()
            return None
        if not want_reply:
            return None
        return self._recv_line(time.monotonic() + config.RECV_DEADLINE)

    # -- typed protocol helpers -------------------------------------------------

    def observe(self) -> Optional[dict[str, Any]]:
        raw = self.request("observe")
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def act(self, order: str, ids: list[int], for_tics: int = config.ORDER_FOR_TICS,
            x: Optional[int] = None, y: Optional[int] = None,
            hud: Optional[str] = None) -> Optional[str]:
        parts = [f"act order={order}", f"ids={','.join(str(i) for i in ids)}"]
        if x is not None:
            parts.append(f"x={x}")
        if y is not None:
            parts.append(f"y={y}")
        parts.append(f"for={for_tics}")
        if hud:
            parts.append(f"hud={hud}")   # piggybacked metrics (display only, C-side)
        return self.request(" ".join(parts))

    def spawn(self, mtype: str, count: int = 1,
              hud: Optional[str] = None) -> Optional[str]:
        line = f"spawn type={mtype} count={count}"
        if hud:
            line += f" hud={hud}"
        return self.request(line)

    def nemesis_propose(self, mtype: str, deltas: list[str]) -> Optional[str]:
        return self.request(f"nemesis propose={mtype} " + " ".join(deltas))
