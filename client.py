import argparse
import asyncio
import getpass
import os
import random
import sys

from werecomm import AuthError, GameClient, load_key

HELP = """Commands:
  just type          talk (only when the moderator lets you)
  /vote NAME         vote (wolves at night, everyone during the day)
  /inspect NAME      seer: check whether NAME is a werewolf
  /heal              witch: save tonight's victim
  /poison NAME       witch: poison NAME
  /pass              witch: do nothing
  /who               show alive/dead players and your role
  /quit              leave"""

BOT_LINES = ["I was asleep all night, honestly.", "%s has been very quiet...", "I trust %s for now.",
             "Something about %s feels off.", "Let's not rush this vote.", "I'm with the village, I promise."]


def show(text):
    print(text, flush=True)


class Player:
    def __init__(self, args):
        self.args = args
        key = load_key(args.key)
        self.client = GameClient(args.host, args.port, args.name, key, args.ca)
        self.done = False
        self.others = []

    async def connect(self):
        delay = 1
        for attempt in range(1, self.args.retries + 1):
            try:
                await self.client.connect()
                show("Connected to %s:%d as %s (TLS, authenticated)." % (self.args.host, self.args.port, self.args.name))
                return True
            except AuthError as error:
                show("Server refused login: %s" % error)
                return False
            except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as error:
                show("Connect attempt %d/%d failed: %s" % (attempt, self.args.retries, error or type(error).__name__))
                await asyncio.sleep(delay)
                delay = min(delay * 2, 8)
        return False

    async def handle_line(self, line):
        line = line.strip()
        if not line:
            return
        parts = line.split(maxsplit=1)
        command, rest = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else None)
        try:
            if command == "/help":
                show(HELP)
            elif command == "/quit":
                self.done = True
                await self.client.close()
            elif command == "/who":
                await self.client.send("who")
            elif command in ("/vote", "/inspect", "/poison"):
                if not rest:
                    show("Usage: %s NAME" % command)
                else:
                    await self.client.send("action", {"kind": command[1:], "target": rest})
            elif command in ("/heal", "/pass"):
                await self.client.send("action", {"kind": command[1:]})
            elif command.startswith("/"):
                show("Unknown command. Type /help.")
            else:
                await self.client.send("chat", {"text": line})
        except ConnectionError:
            show("[not connected right now]")

    def render(self, msg_type, payload):
        if msg_type == "role":
            show("\n~~~~~ YOU ARE A %s ~~~~~" % payload["role"].upper())
            if payload.get("pack"):
                show("Your pack: %s" % ", ".join(payload["pack"]))
        elif msg_type == "info":
            show("[MODERATOR] %s" % payload)
        elif msg_type == "chat":
            room = payload.get("room")
            prefix = "[wolves] " if room == "wolves" else ""
            show("%s%s: %s" % (prefix, payload["from"], payload["text"]))
        elif msg_type == "prompt":
            show("[ACTION] %s" % payload["text"])
        elif msg_type == "status":
            show("[STATUS] round %s | alive: %s | dead: %s%s" % (
                payload.get("round"), ", ".join(payload.get("alive", [])), ", ".join(payload.get("dead", [])) or "-",
                " | you are the %s" % payload["role"] if payload.get("role") else ""))
        elif msg_type == "gameover":
            show("\n===== GAME OVER: %s win! =====" % payload["winner"])
            for name, role in sorted(payload["roles"].items()):
                show("  %-10s %s" % (name, role))
            self.done = True
        elif msg_type in ("error", "denied", "bye"):
            show("[SERVER] %s" % payload)
        elif msg_type != "welcome":
            show("[%s] %s" % (msg_type, payload))

    async def bot_act(self, msg_type, payload):
        if msg_type == "info" and "joined the lobby" in str(payload):
            self.others = [w.strip(".,") for w in str(payload).split() if w.startswith("player")]
        if msg_type == "prompt":
            await asyncio.sleep(random.uniform(1.0, min(4.0, payload["seconds"] / 3)))
            valid = [v for v in payload["valid"] if v != self.args.name]
            if payload["action"] == "witch":
                choice = random.choice(["heal"] if "heal" in valid else ["pass", "pass", random.choice(valid)])
                line = "/" + choice
            else:
                target = random.choice(valid)
                if payload["action"] == "vote" and "talk privately" in payload["text"]:
                    await self.say("Let's take %s tonight." % target)
                    await asyncio.sleep(random.uniform(0.5, 1.5))
                line = "/%s %s" % (payload["action"], target)
            show("> " + line)
            await self.handle_line(line)
        elif msg_type == "info" and "seconds to discuss" in str(payload):
            names = [n.strip(",.") for n in str(payload).split("Alive: ")[1].split(". ")[0].split(", ")]
            names = [n for n in names if n != self.args.name] or ["someone"]
            await asyncio.sleep(random.uniform(1, 4))
            line = random.choice(BOT_LINES)
            await self.say(line % random.choice(names) if "%s" in line else line)

    async def say(self, text):
        show("> " + text)
        await self.handle_line(text)

    async def run(self):
        if not await self.connect():
            return 1
        loop = asyncio.get_running_loop()
        if not self.args.bot and sys.stdin.isatty():
            loop.add_reader(sys.stdin.fileno(), lambda: asyncio.ensure_future(self.handle_line(sys.stdin.readline())))
            show("Type /help for commands.")
        while not self.done:
            msg_type, payload = await self.client.recv()
            if msg_type == "__disconnect__":
                if self.done:
                    break
                show("[connection lost: %s] reconnecting..." % payload)
                if not await self.connect():
                    show("Could not reconnect. Bye.")
                    return 1
                continue
            self.render(msg_type, payload)
            if self.args.bot:
                asyncio.ensure_future(self.bot_act(msg_type, payload))
        await self.client.close()
        return 0


def main():
    user = getpass.getuser()
    parser = argparse.ArgumentParser(description="Werewolves player client (werecomm/2: TLS over TCP)")
    parser.add_argument("--host", default=os.environ.get("WW_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("WW_PORT", 5555)))
    parser.add_argument("--name", default=user)
    parser.add_argument("--key", default=None)
    parser.add_argument("--ca", default=os.path.expanduser("~/.werewolves/ca.pem"))
    parser.add_argument("--retries", type=int, default=6)
    parser.add_argument("--bot", action="store_true", help="play automatically (for testing)")
    args = parser.parse_args()
    args.key = args.key or os.path.expanduser("~/.werewolves/%s.key" % args.name)
    try:
        sys.exit(asyncio.run(Player(args).run()))
    except KeyboardInterrupt:
        pass
    except PermissionError as error:
        show(str(error))
        sys.exit(1)


if __name__ == "__main__":
    main()
