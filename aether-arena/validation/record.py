#!/usr/bin/env python3
"""ADR-151 P1 — capture the live RuView inference layer for offline scoring.

Subscribes to the sensing-server WebSocket (`/ws/sensing`) and appends every
`sensing_update` and `semantic_event` message, stamped with a local receive
time, to a JSONL session log. Pure stdlib (incl. a minimal WebSocket client) —
runs on any Python 3, no pip install.

    python record.py --url ws://127.0.0.1:8080/ws/sensing --out session.jsonl
    python record.py --duration 600          # stop after 10 minutes
    # Ctrl-C to stop early; the log is flushed line-by-line.
"""
import argparse, base64, json, os, socket, ssl, struct, sys, time


# ── minimal stdlib WebSocket client (RFC 6455, client side, text frames) ──────
def ws_connect(url):
    secure = url.startswith("wss://")
    rest = url.split("://", 1)[1]
    hostport, _, path = rest.partition("/")
    path = "/" + path
    host, _, port = hostport.partition(":")
    port = int(port) if port else (443 if secure else 80)
    sock = socket.create_connection((host, port), timeout=10)
    if secure:
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("server closed during handshake")
        buf += chunk
    status = buf.split(b"\r\n", 1)[0]
    if b" 101 " not in status:
        raise RuntimeError(f"WebSocket handshake failed: {status.decode('latin1')}")
    return sock, buf.split(b"\r\n\r\n", 1)[1]


def _send_frame(sock, opcode, data=b""):
    m = os.urandom(4)
    masked = bytes(b ^ m[i % 4] for i, b in enumerate(data))
    n = len(data)
    header = bytes([0x80 | opcode])
    if n < 126:
        header += bytes([0x80 | n])
    elif n < 65536:
        header += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        header += bytes([0x80 | 127]) + struct.pack(">Q", n)
    sock.sendall(header + m + masked)


def ws_messages(sock, initial=b""):
    buf = bytearray(initial)

    def recvn(n):
        while len(buf) < n:
            d = sock.recv(65536)
            if not d:
                raise ConnectionError("closed")
            buf.extend(d)
        out = bytes(buf[:n])
        del buf[:n]
        return out

    while True:
        h = recvn(2)
        opcode = h[0] & 0x0F
        masked = h[1] & 0x80
        ln = h[1] & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", recvn(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", recvn(8))[0]
        mask = recvn(4) if masked else b""
        payload = recvn(ln)
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if opcode == 0x8:            # close
            return
        elif opcode == 0x9:          # ping → pong
            _send_frame(sock, 0xA, payload)
        elif opcode in (0x1, 0x2):   # text / binary
            yield payload.decode("utf-8", "replace")


# ── recorder ──────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Record the RuView WS sensing stream to JSONL.")
    ap.add_argument("--url", default="ws://127.0.0.1:8080/ws/sensing")
    ap.add_argument("--out", default="session.jsonl")
    ap.add_argument("--duration", type=float, default=0, help="seconds (0 = until Ctrl-C)")
    args = ap.parse_args()

    print(f"[record] connecting to {args.url}", file=sys.stderr)
    sock, leftover = ws_connect(args.url)
    deadline = time.time() + args.duration if args.duration else None
    n_update = n_event = 0
    with open(args.out, "w", buffering=1) as f:
        try:
            for text in ws_messages(sock, leftover):
                recv_ts = time.time()
                try:
                    msg = json.loads(text)
                except ValueError:
                    continue
                kind = msg.get("type", "?")
                if kind == "semantic_event":
                    n_event += 1
                else:
                    n_update += 1
                f.write(json.dumps({"recv_ts": recv_ts, "msg": msg}) + "\n")
                if deadline and recv_ts >= deadline:
                    break
        except KeyboardInterrupt:
            pass
        except ConnectionError as e:
            print(f"[record] connection ended: {e}", file=sys.stderr)
    print(f"[record] wrote {n_update} sensing_update + {n_event} semantic_event → {args.out}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
