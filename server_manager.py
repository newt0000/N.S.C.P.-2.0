# server_manager.py
import threading
import subprocess
import time
from collections import deque
from typing import Optional, List, Dict
import psutil
import os
import re
import socket
import struct


class SimpleRCON:
    """
    Minimal RCON client implementation that does NOT use signal/alarm,
    so it's safe to use from any thread.
    """
    SERVERDATA_AUTH = 3
    SERVERDATA_EXECCOMMAND = 2
    SERVERDATA_RESPONSE_VALUE = 0

    def __init__(self, host: str, port: int, password: str, timeout: float = 3.0):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self.req_id = 0

    def _connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect((self.host, self.port))

    def _close(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None

    def _next_id(self) -> int:
        self.req_id += 1
        return self.req_id

    def _send_packet(self, req_id: int, p_type: int, body: str):
        if self.sock is None:
            raise RuntimeError("RCON socket not connected")

        payload = body.encode("utf-8") + b"\x00\x00"
        length = 4 + 4 + len(payload)  # requestId + type + payload
        packet = struct.pack("<iii", length, req_id, p_type) + payload
        self.sock.sendall(packet)

    def _recv_exact(self, size: int) -> bytes:
        buf = b""
        while len(buf) < size:
            chunk = self.sock.recv(size - len(buf))
            if not chunk:
                raise ConnectionError("RCON connection closed")
            buf += chunk
        return buf

    def _read_packet(self):
        if self.sock is None:
            raise RuntimeError("RCON socket not connected")

        # Read length (4 bytes)
        raw_len = self._recv_exact(4)
        if not raw_len:
            return None, None, None
        (length,) = struct.unpack("<i", raw_len)

        # Then the rest of the packet
        data = self._recv_exact(length)
        if not data or len(data) < 8:
            return None, None, None

        req_id, p_type = struct.unpack("<ii", data[:8])
        body = data[8:-2].decode("utf-8", errors="replace")  # strip two nulls
        return req_id, p_type, body

    def auth(self):
        rid = self._next_id()
        self._send_packet(rid, self.SERVERDATA_AUTH, self.password)

        # The server should send back two packets; we only care about the auth result
        _rid, _ptype, _body = self._read_packet()
        rid2, ptype2, _body2 = self._read_packet()

        # If auth fails, id will be -1
        if rid2 == -1 or ptype2 != self.SERVERDATA_RESPONSE_VALUE:
            raise PermissionError("RCON auth failed")

    def command(self, cmd: str) -> str:
        if self.sock is None:
            self._connect()
            self.auth()

        rid = self._next_id()
        self._send_packet(rid, self.SERVERDATA_EXECCOMMAND, cmd)

        responses = []
        while True:
            resp_id, ptype, body = self._read_packet()
            if resp_id is None:
                break
            responses.append(body)
            if resp_id == rid:
                break

        return "\n".join(responses)

    def close(self):
        self._close()

    def __enter__(self):
        self._connect()
        self.auth()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._close()


class MinecraftServerManager:
    def __init__(self, start_command: str, server_dir: str, auto_restart: bool = True):
        self.start_command = start_command
        self.server_dir = server_dir
        self.auto_restart = auto_restart

        self.process: Optional[subprocess.Popen] = None
        self.process_lock = threading.Lock()
        self.console_log = deque(maxlen=2000)
        self.log_lock = threading.Lock()
        self.running = False
        self._log_id = 0

        self._reader_thread: Optional[threading.Thread] = None
        self._watcher_thread: Optional[threading.Thread] = None

        # Manual stop flag
        self.user_requested_stop = False

        # Player tracking
        self.players_online = set()
        self.player_last_seen: Dict[str, float] = {}
        self.player_lock = threading.Lock()

        # Precompiled regex for join/leave (for history)
        # Matches: "[22:07:59 INFO]: Kai joined the game"
        # Precompiled regex for join/leave (for history)
        # Join example:
        #   [14:08:06 INFO]: CafeinatedLizard[/24.198.181.134:25162] logged in with entity id 687 at (...)
        self._join_re = re.compile(
            r"]:\s*([A-Za-z0-9_]+)\[/[0-9A-Fa-f\.:]+:\d+\]\s+logged in with entity id",
            re.IGNORECASE,
        )
        
        # Common quit patterns (Paper/Spigot):
        #   [INFO]: Name lost connection: Disconnected
        #   [INFO]: Name left the game
        self._quit_re = re.compile(
            r"]:\s*([A-Za-z0-9_]+)\s+(?:lost connection|disconnected|left the game|timed out)",
            re.IGNORECASE,
        )


        # RCON settings from environment (optional)
        self.rcon_host = os.getenv("RCON_HOST", "127.0.0.1")
        self.rcon_port = int(os.getenv("RCON_PORT", "25575"))
        self.rcon_pass = os.getenv("RCON_PASS", "")
        self.rcon_enabled = bool(self.rcon_pass)
        self._rcon_fail_count = 0
        self._rcon_disabled_reason: Optional[str] = None

    # ------------------------------------------------------------------
    # RCON helpers (SimpleRCON only, optional)
    # ------------------------------------------------------------------
    def _rcon_list(self) -> Optional[List[str]]:
        """
        Run 'list' via RCON and return a list of player names.
        Returns None if RCON is disabled or permanently failed.
        """
        if not self.rcon_enabled or not self.rcon_pass:
            return None
        if self._rcon_disabled_reason is not None:
            # Already permanently disabled due to repeated failures
            return None

        try:
            with SimpleRCON(self.rcon_host, self.rcon_port, self.rcon_pass, timeout=3.0) as r:
                resp = r.command("list")
            return self._parse_list_response(resp)
        except Exception as e:
            self._rcon_fail_count += 1
            msg = str(e)
            self._add_log_line(f"[PANEL] RCON /list failed (attempt {self._rcon_fail_count}): {msg}")
            # If we keep getting connection refused / timeouts, disable RCON to avoid log spam
            if self._rcon_fail_count >= 3:
                self._rcon_disabled_reason = msg
                self._add_log_line(
                    f"[PANEL] RCON permanently disabled after repeated failures: {msg}. "
                    f"Falling back to log-based player tracking only."
                )
            return None

    @staticmethod
    def _parse_list_response(resp: str) -> List[str]:
        """
        Parse a typical Minecraft /list response into a list of player names.
        Expected format:
            "There are X of a max Y players online: name1, name2"
        """
        if not resp:
            return []
        names_part = ""
        if ":" in resp:
            names_part = resp.split(":", 1)[1].strip()
        if not names_part:
            return []
        return [n.strip() for n in names_part.split(",") if n.strip()]

    # -------------------------------------------------
    # Player Log Parsing (for history + fallback)
    # -------------------------------------------------
    def _process_player_events(self, line: str):
        txt = line.strip()
        name = None
        joined = False
        left = False

        m = self._join_re.search(txt)
        if m:
            name = m.group(1)
            joined = True
        else:
            m = self._quit_re.search(txt)
            if m:
                name = m.group(1)
                left = True

        if not name:
            return

        now = time.time()
        with self.player_lock:
            if joined:
                self.players_online.add(name)
                self.player_last_seen[name] = now
            elif left:
                self.players_online.discard(name)
                self.player_last_seen[name] = now

    def get_online_players(self) -> List[Dict]:
        """
        Main entry for the dashboard "online players" panel.

        1) Try live data from RCON 'list' (if enabled and working).
        2) On success, update players_online + last_seen.
        3) On failure, fall back to our last-known online set (from logs).
        """
        now = time.time()
        names = self._rcon_list()

        if names is not None:
            with self.player_lock:
                self.players_online = set(names)
                for n in names:
                    self.player_last_seen[n] = now

        with self.player_lock:
            return [
                {"name": n, "last_seen": self.player_last_seen.get(n)}
                for n in sorted(self.players_online)
            ]

    def get_recent_players(self, limit: int = 20) -> List[Dict]:
        with self.player_lock:
            items = list(self.player_last_seen.items())

        items.sort(key=lambda kv: kv[1], reverse=True)

        return [
            {"name": name, "last_seen": ts}
            for name, ts in items[:limit]
        ]

    # -------------------------------------------------
    # Console Log Handling
    # -------------------------------------------------
    def _add_log_line(self, line: str):
        with self.log_lock:
            self._log_id += 1
            self.console_log.append({
                "id": self._log_id,
                "line": line.rstrip("\n")
            })

        self._process_player_events(line)

    def get_logs_since(self, last_id: int) -> List[Dict]:
        with self.log_lock:
            return [entry for entry in self.console_log if entry["id"] > last_id]

    # -------------------------------------------------
    # Server Process Management
    # -------------------------------------------------
    def start(self) -> bool:
        with self.process_lock:
            if self.process and self.process.poll() is None:
                return False

            self.user_requested_stop = False

            self.process = subprocess.Popen(
                self.start_command,
                shell=True,
                cwd=self.server_dir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            self.running = True
            self._add_log_line("[PANEL] Server started.")

            self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
            self._reader_thread.start()

            self._watcher_thread = threading.Thread(target=self._watcher_loop, daemon=True)
            self._watcher_thread.start()

            if not self.rcon_pass:
                self._add_log_line("[PANEL] RCON disabled: RCON_PASS env var is empty or not set.")
            else:
                self._add_log_line(
                    f"[PANEL] RCON configured on {self.rcon_host}:{self.rcon_port} "
                    f"(SimpleRCON; will auto-disable on repeated failures)."
                )

            return True

    def _reader_loop(self):
        with self.process_lock:
            proc = self.process

        if not proc or not proc.stdout:
            return

        for line in proc.stdout:
            self._add_log_line(line)

        self._add_log_line("[PANEL] Console reader stopped.")

    def _watcher_loop(self):
        while True:
            time.sleep(3)
            with self.process_lock:
                if not self.process:
                    break

                ret = self.process.poll()

                if ret is not None:
                    self.running = False
                    self._add_log_line(f"[PANEL] Server stopped with code {ret}.")

                    if self.auto_restart and not self.user_requested_stop:
                        self._add_log_line("[PANEL] Auto-restart enabled, restarting server...")
                        self._do_restart_locked()
                    else:
                        if self.user_requested_stop:
                            self._add_log_line("[PANEL] Server stopped by user request.")
                        self.user_requested_stop = False
                    break

    def _do_restart_locked(self):
        self.process = subprocess.Popen(
            self.start_command,
            shell=True,
            cwd=self.server_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.running = True

        self._add_log_line("[PANEL] Server restarted (auto-restart).")

        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

        self._watcher_thread = threading.Thread(target=self._watcher_loop, daemon=True)
        self._watcher_thread.start()

    # -------------------------------------------------
    # NON-BLOCKING STOP
    # -------------------------------------------------
    def stop(self) -> bool:
        with self.process_lock:
            if not self.process or self.process.poll() is not None:
                return False

            self.user_requested_stop = True
            proc = self.process

        t = threading.Thread(target=self._stop_worker, args=(proc,), daemon=True)
        t.start()

        self._add_log_line("[PANEL] Stop requested.")
        return True

    def _stop_worker(self, proc: subprocess.Popen):
        try:
            self.send_command("stop")
        except Exception:
            pass

        self._add_log_line("[PANEL] Sent 'stop' command. Waiting for shutdown...")

        for _ in range(30):
            time.sleep(1)
            if proc.poll() is not None:
                break

        if proc.poll() is None:
            self._add_log_line("[PANEL] Forcing server kill.")
            proc.kill()

        with self.process_lock:
            if self.process is proc:
                self.running = False
                self.process = None

    # -------------------------------------------------
    # User Commands
    # -------------------------------------------------
    def send_command(self, cmd: str) -> bool:
        with self.process_lock:
            if (
                not self.process or
                self.process.poll() is not None or
                not self.process.stdin
            ):
                return False

            self.process.stdin.write(cmd + "\n")
            self.process.stdin.flush()

        self._add_log_line(f"> {cmd}")
        return True

    def is_running(self) -> bool:
        with self.process_lock:
            return bool(self.process and self.process.poll() is None)

    # -------------------------------------------------
    # Hardware Stats
    # -------------------------------------------------
    def get_stats(self) -> dict:
        cpu = psutil.cpu_percent(interval=0.2)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage(self.server_dir)

        return {
            "cpu_percent": cpu,
            "mem_total": mem.total,
            "mem_used": mem.used,
            "mem_percent": mem.percent,
            "disk_total": disk.total,
            "disk_used": disk.used,
            "disk_percent": disk.percent,
            "server_running": self.is_running(),
        }
