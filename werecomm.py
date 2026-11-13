import asyncio
import collections
import hashlib
import hmac
import json
import os
import secrets
import ssl
import stat
import struct
import time

PROTOCOL = "werecomm/2"
MAX_FRAME = 64 * 1024
SERVER_NAME = "werewolves-moderator"
_HEADER = struct.Struct(">I")


class ProtocolError(Exception):
    pass


class AuthError(Exception):
    pass


def load_key(path):
    info = os.stat(path)
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise PermissionError(path + " must not be readable by group or others (chmod 600)")
    with open(path) as f:
        key = bytes.fromhex(f.read().strip())
    if len(key) < 16:
        raise ValueError(path + " does not contain a valid key")
    return key


def new_key():
    return secrets.token_bytes(32)


def encode_frame(msg_type, payload=None):
    data = json.dumps({"type": msg_type, "payload": payload}, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_FRAME:
        raise ValueError("message larger than MAX_FRAME")
    return _HEADER.pack(len(data)) + data


async def read_frame(reader, max_frame=MAX_FRAME):
    header = await reader.readexactly(_HEADER.size)
    (length,) = _HEADER.unpack(header)
    if length == 0 or length > max_frame:
        raise ProtocolError("frame length %d rejected" % length)
    data = await reader.readexactly(length)
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ProtocolError("frame is not valid UTF-8 JSON")
    if not isinstance(obj, dict) or not isinstance(obj.get("type"), str) or not 0 < len(obj["type"]) <= 32:
        raise ProtocolError("frame has no valid type field")
    return obj["type"], obj.get("payload")


def _mac(key, nonce, name):
    return hmac.new(key, nonce + name.encode("utf-8"), hashlib.sha256).hexdigest()


class _TokenBucket:
    def __init__(self, rate, burst):
        self.rate = rate
        self.burst = burst
        self.tokens = float(burst)
        self.stamp = time.monotonic()

    def take(self):
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.stamp) * self.rate)
        self.stamp = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


class _Peer:
    def __init__(self, server, name, reader, writer, address):
        self.server = server
        self.name = name
        self.reader = reader
        self.writer = writer
        self.address = address
        self.outbox = asyncio.Queue(maxsize=server.outbox_limit)
        self.bucket = _TokenBucket(server.rate, server.burst)
        self.violations = 0
        self.closed = False
        self.close_reason = None
        self.writer_task = asyncio.ensure_future(self._write_loop())

    def queue(self, msg_type, payload):
        if self.closed:
            return False
        try:
            self.outbox.put_nowait(encode_frame(msg_type, payload))
            return True
        except asyncio.QueueFull:
            self.server.stats["slow_consumer_dropped"] += 1
            self.close("not reading its messages", abort=True)
            return False

    async def _write_loop(self):
        try:
            while True:
                frame = await self.outbox.get()
                self.writer.write(frame)
                await asyncio.wait_for(self.writer.drain(), self.server.write_timeout)
        except asyncio.TimeoutError:
            self.server.stats["slow_consumer_dropped"] += 1
            self.close("write timed out", abort=True)
        except (ConnectionError, OSError, asyncio.CancelledError):
            self.close(self.close_reason or "connection lost")

    def close(self, reason, abort=False):
        if self.closed:
            return
        self.closed = True
        self.close_reason = reason
        try:
            if abort:
                self.writer.transport.abort()
            else:
                self.writer.close()
        except Exception:
            pass
        if self.writer_task is not asyncio.current_task():
            self.writer_task.cancel()


class GameServer:
    def __init__(self, host, port, certfile, keyfile, player_keys, *, max_frame=MAX_FRAME,
                 handshake_timeout=5.0, max_pending=16, rate=8.0, burst=20, max_violations=40,
                 heartbeat=10.0, idle_timeout=35.0, outbox_limit=512, write_timeout=10.0, log=None):
        self.host = host
        self.port = port
        self.certfile = certfile
        self.keyfile = keyfile
        self.player_keys = dict(player_keys)
        self.max_frame = max_frame
        self.handshake_timeout = handshake_timeout
        self.max_pending = max_pending
        self.rate = rate
        self.burst = burst
        self.max_violations = max_violations
        self.heartbeat = heartbeat
        self.idle_timeout = idle_timeout
        self.outbox_limit = outbox_limit
        self.write_timeout = write_timeout
        self.log = log or (lambda text: None)
        self.stats = collections.Counter()
        self.inbox = asyncio.Queue()
        self._peers = {}
        self._pending = 0
        self._server = None
        self._heartbeat_task = None
        self._changed = asyncio.Condition()

    async def start(self):
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self.certfile, self.keyfile)
        self._server = await asyncio.start_server(
            self._handle, self.host, self.port, ssl=context,
            ssl_handshake_timeout=self.handshake_timeout)
        self.port = self._server.sockets[0].getsockname()[1]
        self._heartbeat_task = asyncio.ensure_future(self._heartbeat_loop())
        self.log("listening on %s:%d (TLS)" % (self.host, self.port))

    def connected(self):
        return sorted(self._peers)

    def address_of(self, name):
        peer = self._peers.get(name)
        return peer.address if peer else None

    async def wait_for_players(self, names, timeout=None):
        wanted = set(names)
        async with self._changed:
            try:
                await asyncio.wait_for(self._changed.wait_for(lambda: wanted <= set(self._peers)), timeout)
            except asyncio.TimeoutError:
                pass
        return sorted(wanted & set(self._peers))

    async def send(self, name, msg_type, payload=None):
        peer = self._peers.get(name)
        if peer is None:
            return False
        return peer.queue(msg_type, payload)

    async def broadcast(self, msg_type, payload=None, to=None, exclude=()):
        names = self._peers if to is None else to
        sent = []
        for name in list(names):
            if name in exclude:
                continue
            peer = self._peers.get(name)
            if peer is not None and peer.queue(msg_type, payload):
                sent.append(name)
        return sent

    async def recv(self, timeout=None):
        try:
            return await asyncio.wait_for(self.inbox.get(), timeout)
        except asyncio.TimeoutError:
            return None

    def disconnect(self, name, reason="removed by moderator"):
        peer = self._peers.get(name)
        if peer is not None:
            peer.close(reason)

    async def close(self):
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        for peer in list(self._peers.values()):
            peer.queue("bye", "server shutting down")
        await asyncio.sleep(0.2)
        for peer in list(self._peers.values()):
            peer.close("server shutting down")
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _heartbeat_loop(self):
        while True:
            await asyncio.sleep(self.heartbeat)
            for peer in list(self._peers.values()):
                peer.queue("ping", None)

    async def _authenticate(self, reader, writer):
        nonce = secrets.token_bytes(32)
        writer.write(encode_frame("challenge", {"nonce": nonce.hex(), "protocol": PROTOCOL}))
        await writer.drain()
        msg_type, payload = await read_frame(reader, 1024)
        if msg_type != "hello" or not isinstance(payload, dict):
            raise ProtocolError("expected hello")
        name, mac = payload.get("name"), payload.get("mac")
        if not isinstance(name, str) or not isinstance(mac, str):
            raise ProtocolError("hello is missing name or mac")
        key = self.player_keys.get(name)
        if key is None or not hmac.compare_digest(_mac(key, nonce, name), mac):
            raise AuthError("bad credentials for %r" % name[:32])
        return name

    async def _handle(self, reader, writer):
        address = "%s:%s" % (writer.get_extra_info("peername") or ("?", "?"))[:2]
        if self._pending >= self.max_pending:
            self.stats["rejected_too_many_pending"] += 1
            writer.close()
            return
        self._pending += 1
        try:
            name = await asyncio.wait_for(self._authenticate(reader, writer), self.handshake_timeout)
        except AuthError as error:
            self.stats["auth_failed"] += 1
            self.log("rejected %s: %s" % (address, error))
            writer.write(encode_frame("denied", "authentication failed"))
            await self._safe_close(writer)
            return
        except asyncio.TimeoutError:
            self.stats["handshake_timeout"] += 1
            self.log("dropped %s: handshake timed out" % address)
            await self._safe_close(writer)
            return
        except ProtocolError as error:
            self.stats["protocol_error"] += 1
            self.log("dropped %s: %s" % (address, error))
            await self._safe_close(writer)
            return
        except (asyncio.IncompleteReadError, ConnectionError, OSError, ssl.SSLError):
            self.stats["handshake_aborted"] += 1
            await self._safe_close(writer)
            return
        finally:
            self._pending -= 1

        old = self._peers.get(name)
        peer = _Peer(self, name, reader, writer, address)
        async with self._changed:
            self._peers[name] = peer
            self._changed.notify_all()
        if old is not None:
            old.queue("bye", "you logged in from somewhere else")
            old.close("replaced by a newer login")
        self.stats["logins"] += 1
        self.log("%s authenticated from %s%s" % (name, address, " (rejoin)" if old else ""))
        peer.queue("welcome", {"name": name, "protocol": PROTOCOL})
        await self.inbox.put((name, "__join__", {"address": address, "replaced": old is not None}))
        try:
            await self._read_loop(peer)
        except ProtocolError as error:
            self.stats["protocol_error"] += 1
            self.log("dropped %s: %s" % (name, error))
            peer.queue("error", str(error))
            await asyncio.sleep(0.05)
            peer.close(str(error))
        except asyncio.TimeoutError:
            self.stats["idle_timeout"] += 1
            peer.close("no traffic for %ds" % self.idle_timeout)
        except (asyncio.IncompleteReadError, ConnectionError, OSError, ssl.SSLError, asyncio.CancelledError):
            peer.close(peer.close_reason or "connection closed")
        finally:
            peer.close(peer.close_reason or "connection closed")
            if self._peers.get(name) is peer:
                async with self._changed:
                    del self._peers[name]
                    self._changed.notify_all()
                await self.inbox.put((name, "__leave__", {"reason": peer.close_reason}))
                self.log("%s left: %s" % (name, peer.close_reason))

    async def _read_loop(self, peer):
        while not peer.closed:
            msg_type, payload = await asyncio.wait_for(read_frame(peer.reader, self.max_frame), self.idle_timeout)
            if msg_type == "pong":
                continue
            if msg_type == "ping":
                peer.queue("pong", None)
                continue
            if not peer.bucket.take():
                self.stats["rate_limited"] += 1
                peer.violations += 1
                if peer.violations >= self.max_violations:
                    raise ProtocolError("flooding")
                continue
            await self.inbox.put((peer.name, msg_type, payload))

    @staticmethod
    async def _safe_close(writer):
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), 1)
        except Exception:
            pass


class GameClient:
    def __init__(self, host, port, name, key, cafile, server_hostname=SERVER_NAME, max_frame=MAX_FRAME):
        self.host = host
        self.port = port
        self.name = name
        self.key = key
        self.cafile = cafile
        self.server_hostname = server_hostname
        self.max_frame = max_frame
        self.inbox = asyncio.Queue()
        self._reader = None
        self._writer = None
        self._task = None
        self.connected = False

    async def connect(self, timeout=10.0):
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=self.cafile)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = True
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port, ssl=context, server_hostname=self.server_hostname),
            timeout)
        msg_type, payload = await asyncio.wait_for(read_frame(self._reader, 1024), timeout)
        if msg_type != "challenge" or payload.get("protocol") != PROTOCOL:
            raise ProtocolError("server did not send a werecomm/2 challenge")
        nonce = bytes.fromhex(payload["nonce"])
        self._writer.write(encode_frame("hello", {"name": self.name, "mac": _mac(self.key, nonce, self.name)}))
        await self._writer.drain()
        msg_type, payload = await asyncio.wait_for(read_frame(self._reader, self.max_frame), timeout)
        if msg_type != "welcome":
            self._writer.close()
            raise AuthError(str(payload))
        self.inbox = asyncio.Queue()
        self.connected = True
        self._task = asyncio.ensure_future(self._read_loop())
        return payload

    async def _read_loop(self):
        reason = "server closed the connection"
        try:
            while True:
                msg_type, payload = await read_frame(self._reader, self.max_frame)
                if msg_type == "ping":
                    self._writer.write(encode_frame("pong"))
                    continue
                if msg_type == "bye":
                    reason = str(payload)
                await self.inbox.put((msg_type, payload))
        except ProtocolError as error:
            reason = "bad message from server: %s" % error
        except (asyncio.IncompleteReadError, ConnectionError, OSError, ssl.SSLError):
            pass
        except asyncio.CancelledError:
            reason = "closed"
        self.connected = False
        await self.inbox.put(("__disconnect__", reason))

    async def send(self, msg_type, payload=None):
        if not self.connected:
            raise ConnectionError("not connected")
        self._writer.write(encode_frame(msg_type, payload))
        await self._writer.drain()

    async def recv(self, timeout=None):
        try:
            return await asyncio.wait_for(self.inbox.get(), timeout)
        except asyncio.TimeoutError:
            return (None, None)

    async def close(self):
        self.connected = False
        if self._task:
            self._task.cancel()
        if self._writer:
            try:
                self._writer.close()
                await asyncio.wait_for(self._writer.wait_closed(), 1)
            except Exception:
                pass
