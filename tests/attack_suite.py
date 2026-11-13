import asyncio
import hashlib
import hmac
import json
import os
import select
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "legacy"))

import werecomm as new
import werecomm_p1 as old

NAMES = ["player0", "player1", "player2", "player3"]
RESULTS = []


def frame(obj):
    data = json.dumps(obj).encode()
    return struct.pack(">I", len(data)) + data


def read_frame_sock(sock, timeout=2.0):
    sock.settimeout(timeout)
    try:
        header = b""
        while len(header) < 4:
            chunk = sock.recv(4 - len(header))
            if not chunk:
                return None
            header += chunk
        n = struct.unpack(">I", header)[0]
        data = b""
        while len(data) < n:
            chunk = sock.recv(n - len(data))
            if not chunk:
                return None
            data += chunk
        return json.loads(data)
    except (socket.timeout, ssl.SSLError, OSError):
        return None


def closed_within(sock, seconds):
    sock.settimeout(seconds)
    start = time.monotonic()
    try:
        while True:
            data = sock.recv(65536)
            if not data:
                return time.monotonic() - start
    except socket.timeout:
        return None
    except (ssl.SSLError, OSError):
        return time.monotonic() - start


class Harness:
    def __init__(self, kind, *args):
        self.proc = subprocess.Popen([sys.executable, os.path.join(HERE, "harness.py"), kind] + list(args),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        self.ready = self.read(10)

    def read(self, timeout):
        ready, _, _ = select.select([self.proc.stdout], [], [], timeout)
        if not ready:
            return None
        line = self.proc.stdout.readline()
        return json.loads(line) if line else None

    def cmd(self, text, timeout=5):
        self.proc.stdin.write(text + "\n")
        self.proc.stdin.flush()
        return self.read(timeout)

    def rss_kb(self):
        with open("/proc/%d/status" % self.proc.pid) as f:
            for line in f:
                if line.startswith("VmRSS"):
                    return int(line.split()[1])

    def cpu_seconds(self):
        with open("/proc/%d/stat" % self.proc.pid) as f:
            fields = f.read().rsplit(")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")

    def kill(self):
        self.proc.kill()
        self.proc.wait()


def wait_registered(harness, name, seconds):
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        reply = harness.cmd("conns", 2)
        if reply and name in reply["conns"]:
            return time.monotonic() - start
        time.sleep(0.05)
    return None


class OldPlayer:
    def __init__(self, path, name):
        self.client = old.Client(path, name)
        self.client.connect()
        self.received = []
        self.alive = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while self.alive:
            msg_type, payload = self.client.recv(timeout=0.2)
            if msg_type is not None:
                self.received.append((msg_type, payload))

    def close(self):
        self.alive = False
        self.client.close()


class NewPlayer:
    def __init__(self, port, secrets, name, key=None):
        self.received = []
        self.join_time = None
        self.error = None
        self.ready = threading.Event()
        self.stop = False
        key = key or new.load_key(os.path.join(secrets, name + ".key"))
        self.client = new.GameClient("127.0.0.1", port, name, key, os.path.join(secrets, "server.pem"))
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_until_complete, args=(self._main(),), daemon=True).start()
        self.ready.wait(15)

    async def _main(self):
        start = time.monotonic()
        try:
            await self.client.connect()
            self.join_time = time.monotonic() - start
        except Exception as error:
            self.error = "%s: %s" % (type(error).__name__, error)
            self.ready.set()
            return
        self.ready.set()
        while not self.stop:
            msg_type, payload = await self.client.recv(timeout=0.2)
            if msg_type is not None:
                self.received.append((msg_type, payload))

    def close(self):
        self.stop = True


def tls_raw(port):
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection(("127.0.0.1", port), timeout=5)
    return context.wrap_socket(raw, server_hostname=new.SERVER_NAME)


def tls_insider(port, secrets, name):
    sock = tls_raw(port)
    challenge = read_frame_sock(sock)
    key = new.load_key(os.path.join(secrets, name + ".key"))
    mac = hmac.new(key, bytes.fromhex(challenge["payload"]["nonce"]) + name.encode(), hashlib.sha256).hexdigest()
    sock.sendall(frame({"type": "hello", "payload": {"name": name, "mac": mac}}))
    welcome = read_frame_sock(sock)
    assert welcome and welcome["type"] == "welcome", welcome
    return sock


def make_secrets():
    folder = tempfile.mkdtemp(prefix="ww-secrets-")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                    "-subj", "/CN=" + new.SERVER_NAME, "-addext", "subjectAltName=DNS:" + new.SERVER_NAME,
                    "-keyout", os.path.join(folder, "server.key"), "-out", os.path.join(folder, "server.pem")],
                   check=True, capture_output=True)
    for name in NAMES:
        path = os.path.join(folder, name + ".key")
        with open(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600), "w") as f:
            f.write(new.new_key().hex())
    return folder


def record(test, old_result, new_result, verdict_old, verdict_new):
    RESULTS.append({"test": test, "old": old_result, "new": new_result,
                    "old_verdict": verdict_old, "new_verdict": verdict_new})
    print("\n### %s\n  P1 (UNIX socket): %s\n     %s\n  P2/3 (TLS+asyncio): %s\n     %s" % (
        test, verdict_old, json.dumps(old_result), verdict_new, json.dumps(new_result)), flush=True)


def old_path():
    return os.path.join(tempfile.mkdtemp(prefix="ww-old-"), "game.sock")


def next_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_impersonation(secrets):
    path = old_path()
    h = Harness("old", path)
    victim = OldPlayer(path, "player0")
    wait_registered(h, "player0", 3)
    attacker = socket.socket(socket.AF_UNIX)
    attacker.connect(path)
    attacker.sendall(frame({"type": "hello", "payload": "player0", "sender": "player0"}))
    time.sleep(0.5)
    h.cmd("send player0 SECRET: you are a WOLF, your pack is player2")
    stolen = read_frame_sock(attacker, 2)
    attacker.sendall(frame({"type": "action", "payload": {"kind": "vote", "target": "player1"}, "sender": "player0"}))
    time.sleep(0.5)
    events = h.cmd("events")["events"]
    old_result = {"attacker_got_private_message": bool(stolen and "SECRET" in str(stolen.get("payload"))),
                  "real_player0_got_it": any("SECRET" in str(p) for _, p in victim.received),
                  "forged_vote_accepted_as_player0": any(e[0] == "player0" and e[1] == "action" for e in events)}
    victim.close()
    h.kill()

    port = next_port()
    h = Harness("new", str(port), secrets, ",".join(NAMES))
    victim = NewPlayer(port, secrets, "player0")
    attacker = tls_raw(port)
    challenge = read_frame_sock(attacker)
    attacker.sendall(frame({"type": "hello", "payload": {"name": "player0", "mac": "00" * 32}}))
    answer = read_frame_sock(attacker)
    wrong_key = NewPlayer(port, secrets, "player0", key=new.new_key())
    h.cmd("send player0 SECRET: you are a WOLF, your pack is player2")
    time.sleep(0.5)
    new_result = {"attacker_reply": answer and answer.get("type"),
                  "attacker_with_random_key": wrong_key.error,
                  "real_player0_got_it": any("SECRET" in str(p) for _, p in victim.received),
                  "auth_failed_count": h.cmd("conns")["stats"].get("auth_failed", 0)}
    victim.close()
    h.kill()
    record("Impersonation: attacker logs in as player0", old_result, new_result,
           "HIJACKED: attacker received player0's private role message and voted as player0",
           "BLOCKED: login denied without player0's key; real player0 unaffected")


def test_handshake_stall(secrets):
    path = old_path()
    h = Harness("old", path)
    staller = socket.socket(socket.AF_UNIX)
    staller.connect(path)
    time.sleep(0.2)
    honest = OldPlayer(path, "player1")
    joined = wait_registered(h, "player1", 10)
    staller.close()
    after = wait_registered(h, "player1", 3)
    old_result = {"honest_join_seconds_while_stalled": joined, "waited": 10,
                  "joined_after_attacker_left": after is not None}
    honest.close()
    h.kill()

    port = next_port()
    h = Harness("new", str(port), secrets, ",".join(NAMES))
    tcp_stallers = [socket.create_connection(("127.0.0.1", port)) for _ in range(10)]
    tls_stallers = [tls_raw(port) for _ in range(5)]
    time.sleep(0.2)
    honest = NewPlayer(port, secrets, "player1")
    tls_closed = [closed_within(s, 8) for s in tls_stallers]
    tcp_closed = [closed_within(s, 8) for s in tcp_stallers]
    stats = h.cmd("conns")["stats"]
    new_result = {"stalled_connections": 15, "honest_join_seconds": round(honest.join_time, 3),
                  "stalled_tls_dropped_after_s": max(t for t in tls_closed if t) if all(tls_closed) else None,
                  "stalled_tcp_dropped": all(t is not None for t in tcp_closed),
                  "handshake_timeouts": stats.get("handshake_timeout", 0)}
    honest.close()
    h.kill()
    record("Handshake stall (connect and send nothing)", old_result, new_result,
           "FROZEN: one idle connection blocks the accept loop; nobody can join until it leaves",
           "OK: honest player joins in milliseconds; stalled connections are dropped after the 5 s timeout")


def test_malformed_handshake(secrets):
    path = old_path()
    h = Harness("old", path)
    attacker = socket.socket(socket.AF_UNIX)
    attacker.connect(path)
    attacker.sendall(struct.pack(">I", 5) + b"{{{{{")
    time.sleep(0.5)
    alive = h.cmd("conns")["accept_thread_alive"]
    honest = OldPlayer(path, "player2")
    joined = wait_registered(h, "player2", 5)
    old_result = {"accept_thread_alive_after_attack": alive, "honest_player_joined": joined is not None}
    honest.close()
    h.kill()

    port = next_port()
    h = Harness("new", str(port), secrets, ",".join(NAMES))
    attacker = tls_raw(port)
    read_frame_sock(attacker)
    attacker.sendall(struct.pack(">I", 5) + b"{{{{{")
    dropped = closed_within(attacker, 3)
    honest = NewPlayer(port, secrets, "player2")
    new_result = {"attacker_dropped_after_s": dropped and round(dropped, 3),
                  "honest_player_joined": honest.error is None,
                  "protocol_errors": h.cmd("conns")["stats"].get("protocol_error", 0)}
    honest.close()
    h.kill()
    record("Malformed JSON during login", old_result, new_result,
           "CRASHED: the accept thread dies on a JSON error, so no player can ever join again",
           "OK: bad frame rejected and the connection dropped; honest players still join")


def test_malformed_ingame(secrets):
    path = old_path()
    h = Harness("old", path)
    insider = socket.socket(socket.AF_UNIX)
    insider.connect(path)
    insider.sendall(frame({"type": "hello", "payload": "player3", "sender": "player3"}))
    wait_registered(h, "player3", 3)
    insider.sendall(struct.pack(">I", 3) + b"\xff\xfe\x00")
    time.sleep(1)
    events = h.cmd("events")["events"]
    conns = h.cmd("conns")["conns"]
    old_result = {"server_notified_of_disconnect": any(e[0] == "player3" and e[1] == "__disconnect__" for e in events),
                  "player3_still_listed_as_connected": "player3" in conns}
    h.kill()

    port = next_port()
    h = Harness("new", str(port), secrets, ",".join(NAMES))
    insider = tls_insider(port, secrets, "player3")
    insider.sendall(struct.pack(">I", 3) + b"\xff\xfe\x00")
    error = read_frame_sock(insider, 2)
    time.sleep(0.5)
    events = h.cmd("events")["events"]
    new_result = {"server_sent": error and [error["type"], error["payload"]],
                  "leave_event": [e for e in events if e[1] == "__leave__"][:1],
                  "player3_still_listed": "player3" in h.cmd("conns")["conns"]}
    h.kill()
    record("Malformed frame from a logged-in player", old_result, new_result,
           "ZOMBIE: reader thread dies silently; server still thinks player3 is connected and waits on it",
           "OK: player gets an error, is disconnected, and the game receives a __leave__ event")


def test_giant_frame(secrets):
    path = old_path()
    h = Harness("old", path)
    time.sleep(0.3)
    rss_before, cpu_before = h.rss_kb(), h.cpu_seconds()
    attacker = socket.socket(socket.AF_UNIX)
    attacker.connect(path)
    attacker.sendall(struct.pack(">I", 0x7FFFFFF0))
    attacker.settimeout(0.5)
    block = b"A" * (1 << 20)
    sent = 0
    start = time.monotonic()
    while time.monotonic() - start < 8:
        try:
            attacker.send(block)
            sent += len(block)
        except socket.timeout:
            pass
    rss_after, cpu_after = h.rss_kb(), h.cpu_seconds()
    honest = OldPlayer(path, "player1")
    joined = wait_registered(h, "player1", 2)
    old_result = {"declared_frame_bytes": 0x7FFFFFF0, "attacker_sent_mb": sent >> 20,
                  "server_rss_mb_before": rss_before // 1024, "server_rss_mb_after": rss_after // 1024,
                  "server_cpu_seconds_burned": round(cpu_after - cpu_before, 2),
                  "honest_player_joined": joined is not None}
    honest.close()
    attacker.close()
    h.kill()

    port = next_port()
    h = Harness("new", str(port), secrets, ",".join(NAMES))
    time.sleep(0.3)
    rss_before = h.rss_kb()
    attacker = tls_raw(port)
    read_frame_sock(attacker)
    attacker.sendall(struct.pack(">I", 0x7FFFFFF0) + b"A" * 4096)
    dropped = closed_within(attacker, 3)
    insider = tls_insider(port, secrets, "player3")
    insider.sendall(struct.pack(">I", 1 << 20) + b"B" * 65536)
    insider_dropped = closed_within(insider, 3)
    honest = NewPlayer(port, secrets, "player1")
    new_result = {"attacker_dropped_after_s": dropped and round(dropped, 3),
                  "logged_in_player_sending_1MB_dropped_after_s": insider_dropped and round(insider_dropped, 3),
                  "server_rss_mb_before": rss_before // 1024, "server_rss_mb_after": h.rss_kb() // 1024,
                  "honest_player_joined": honest.error is None}
    honest.close()
    h.kill()
    record("Giant length header (claims a 2 GB message)", old_result, new_result,
           "DoS: server buffers everything (memory and CPU climb) and the lobby is blocked",
           "OK: frames over 64 KiB are rejected at the header, nothing is buffered")


def test_slow_consumer(secrets):
    count, size = 3000, 4096
    path = old_path()
    h = Harness("old", path)
    honest = OldPlayer(path, "player0")
    lazy = socket.socket(socket.AF_UNIX)
    lazy.connect(path)
    lazy.sendall(frame({"type": "hello", "payload": "player1", "sender": "player1"}))
    wait_registered(h, "player0", 3)
    wait_registered(h, "player1", 3)
    reply = h.cmd("bcast %d %d" % (count, size), timeout=15)
    old_result = {"broadcasts": count, "finished": reply is not None,
                  "honest_player_received": len([m for m in honest.received if m[0] == "chat"]), "waited_s": 15}
    honest.close()
    h.kill()

    port = next_port()
    h = Harness("new", str(port), secrets, ",".join(NAMES))
    honest = NewPlayer(port, secrets, "player0")
    lazy = tls_insider(port, secrets, "player1")
    time.sleep(0.3)
    reply = h.cmd("bcast %d %d" % (count, size), timeout=15)
    time.sleep(2)
    conns = h.cmd("conns")
    new_result = {"broadcasts": count, "finished_in_s": reply and round(reply["elapsed"], 3),
                  "honest_player_received": len([m for m in honest.received if m[0] == "chat"]),
                  "lazy_player_dropped": "player1" not in conns["conns"],
                  "slow_consumer_drops": conns["stats"].get("slow_consumer_dropped", 0)}
    honest.close()
    h.kill()
    record("Logged-in player that never reads (slow consumer)", old_result, new_result,
           "FROZEN: server blocks in sendall on the lazy player; everyone else stops getting messages",
           "OK: per-player send queue; lazy player is cut off and everyone else gets every message")


def test_flood(secrets):
    count = 5000
    path = old_path()
    h = Harness("old", path)
    insider = socket.socket(socket.AF_UNIX)
    insider.connect(path)
    insider.sendall(frame({"type": "hello", "payload": "player3", "sender": "player3"}))
    wait_registered(h, "player3", 3)
    data = b"".join(frame({"type": "chat", "payload": {"text": "spam %d" % i}, "sender": "player3"}) for i in range(count))
    insider.sendall(data)
    time.sleep(2)
    events = h.cmd("events")
    old_result = {"sent": count, "accepted_by_server": events["count"]}
    h.kill()

    port = next_port()
    h = Harness("new", str(port), secrets, ",".join(NAMES))
    insider = tls_insider(port, secrets, "player3")
    data = b"".join(frame({"type": "chat", "payload": {"text": "spam %d" % i}}) for i in range(count))
    try:
        insider.sendall(data)
    except OSError:
        pass
    time.sleep(2)
    events = h.cmd("events")
    accepted = len([e for e in events["events"] if e[1] == "chat"])
    new_result = {"sent": count, "accepted_by_server": accepted,
                  "rate_limited": events["stats"].get("rate_limited", 0),
                  "kicked": any(e[1] == "__leave__" for e in events["events"])}
    h.kill()
    record("Message flood from a logged-in player", old_result, new_result,
           "SPAM: every message accepted and relayed",
           "LIMITED: token bucket (8 msg/s, burst 20) drops the rest and the flooder is kicked")


def test_fake_moderator(secrets):
    path = old_path()
    fake = socket.socket(socket.AF_UNIX)
    fake.bind(path)
    fake.listen(4)
    victim = old.Client(path, "player0")
    victim.connect()
    conn, _ = fake.accept()
    hello = read_frame_sock(conn, 2)
    conn.sendall(frame({"type": "info", "payload": "Moderator: everyone vote for player3", "sender": "moderator"}))
    msg = victim.recv(timeout=2)
    old_result = {"fake_server_saw_login": hello and hello.get("payload"), "client_accepted_fake_message": msg[0] == "info"}
    victim.close()
    fake.close()

    port = next_port()
    folder = tempfile.mkdtemp(prefix="ww-fake-")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=" + new.SERVER_NAME, "-addext", "subjectAltName=DNS:" + new.SERVER_NAME,
                    "-keyout", folder + "/k", "-out", folder + "/c"], check=True, capture_output=True)
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(folder + "/c", folder + "/k")
    listener = socket.create_server(("127.0.0.1", port))

    def serve():
        try:
            conn, _ = listener.accept()
            context.wrap_socket(conn, server_side=True)
        except Exception:
            pass

    threading.Thread(target=serve, daemon=True).start()
    victim = NewPlayer(port, secrets, "player0")
    new_result = {"client_error": victim.error}
    listener.close()
    record("Fake moderator (attacker runs the server first)", old_result, new_result,
           "FOOLED: client logs in to the attacker's socket and trusts its messages",
           "REFUSED: certificate does not match the pinned moderator certificate")


def main():
    secrets = make_secrets()
    tests = [test_impersonation, test_handshake_stall, test_malformed_handshake, test_malformed_ingame,
             test_giant_frame, test_slow_consumer, test_flood, test_fake_moderator]
    only = sys.argv[1:]
    for test in tests:
        if only and test.__name__ not in only:
            continue
        try:
            test(secrets)
        except Exception as error:
            RESULTS.append({"test": test.__name__, "error": repr(error)})
            print("!!! %s failed: %r" % (test.__name__, error), flush=True)
    out = os.path.join(HERE, "results.json")
    with open(out, "w") as f:
        json.dump(RESULTS, f, indent=2)
    print("\nwrote " + out)


if __name__ == "__main__":
    main()
