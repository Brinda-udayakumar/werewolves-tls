import asyncio
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "legacy"))


def reply(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def rss_kb():
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS"):
                return int(line.split()[1])


def run_old(path):
    import werecomm_p1 as old
    server = old.Server(path)
    server.start()
    events = []

    def watch():
        while True:
            names = list(server.connections)
            if not names:
                time.sleep(0.05)
                continue
            name, msg_type, payload = server.recv_any(names, timeout=0.1)
            if name is not None:
                events.append([name, msg_type, str(payload)[:60]])

    threading.Thread(target=watch, daemon=True).start()
    reply({"ready": True, "accept_thread_alive": server._accept_thread.is_alive()})
    for line in sys.stdin:
        cmd = line.strip().split(" ", 2)
        if cmd[0] == "conns":
            reply({"conns": sorted(server.connections), "accept_thread_alive": server._accept_thread.is_alive()})
        elif cmd[0] == "send":
            server.send(cmd[1], "info", cmd[2].strip())
            reply({"ok": True})
        elif cmd[0] == "bcast":
            n, size = int(cmd[1]), int(cmd[2])
            start = time.monotonic()
            for i in range(n):
                server.broadcast("chat", {"from": "x", "text": "%06d" % i + "y" * size})
            reply({"elapsed": time.monotonic() - start})
        elif cmd[0] == "events":
            reply({"events": events[-200:], "count": len(events)})
        elif cmd[0] == "rss":
            reply({"rss_kb": rss_kb()})


async def run_new(port, secrets_dir, names):
    from werecomm import GameServer, load_key
    keys = {n: load_key(os.path.join(secrets_dir, n + ".key")) for n in names}
    server = GameServer("127.0.0.1", port, os.path.join(secrets_dir, "server.pem"),
                        os.path.join(secrets_dir, "server.key"), keys)
    await server.start()
    events = []

    async def watch():
        while True:
            name, msg_type, payload = await server.recv()
            events.append([name, msg_type, str(payload)[:60]])

    asyncio.ensure_future(watch())
    loop = asyncio.get_running_loop()
    lines = asyncio.Queue()
    loop.add_reader(sys.stdin.fileno(), lambda: lines.put_nowait(sys.stdin.readline()))
    reply({"ready": True, "port": server.port})
    while True:
        line = await lines.get()
        if not line:
            break
        cmd = line.strip().split(" ", 2)
        if cmd[0] == "conns":
            reply({"conns": server.connected(), "stats": dict(server.stats)})
        elif cmd[0] == "send":
            await server.send(cmd[1], "info", cmd[2].strip())
            reply({"ok": True})
        elif cmd[0] == "bcast":
            n, size = int(cmd[1]), int(cmd[2])
            start = time.monotonic()
            for i in range(n):
                await server.broadcast("chat", {"from": "x", "text": "%06d" % i + "y" * size})
                if i % 50 == 0:
                    await asyncio.sleep(0)
            reply({"elapsed": time.monotonic() - start})
        elif cmd[0] == "events":
            reply({"events": events[-200:], "count": len(events), "stats": dict(server.stats)})
        elif cmd[0] == "rss":
            reply({"rss_kb": rss_kb()})


if __name__ == "__main__":
    if sys.argv[1] == "old":
        run_old(sys.argv[2])
    else:
        asyncio.run(run_new(int(sys.argv[2]), sys.argv[3], sys.argv[4].split(",")))
