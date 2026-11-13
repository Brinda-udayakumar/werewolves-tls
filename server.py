import argparse
import asyncio
import collections
import datetime
import os
import random
import sys

from werecomm import GameServer, load_key


def env_int(name, default):
    return int(os.environ.get(name, default))


WOLF_TIME = env_int("WW_WOLF_TIME", 45)
SEER_TIME = env_int("WW_SEER_TIME", 20)
WITCH_TIME = env_int("WW_WITCH_TIME", 20)
DAY_TALK_TIME = env_int("WW_DAY_TALK_TIME", 90)
DAY_VOTE_TIME = env_int("WW_DAY_VOTE_TIME", 30)
CONNECT_TIMEOUT = env_int("WW_CONNECT_TIMEOUT", 600)
MIN_PLAYERS = 4


class Game:
    def __init__(self, server, players, num_wolves, logfile):
        self.server = server
        self.players = list(players)
        self.num_wolves = num_wolves
        self.logfile = logfile
        self.alive = []
        self.roles = {}
        self.potions = {"heal": 1, "poison": 1}
        self.chat_rooms = {}
        self.lobby = True
        self.slots = {}
        self.round = 0
        self.over = False
        self.joined = set()
        self.lobby_changed = asyncio.Event()

    def log(self, text):
        line = "%s %s" % (datetime.datetime.now().strftime("%H:%M:%S"), text)
        print(line, flush=True)
        os.write(self.logfile, (line + "\n").encode())

    async def tell(self, name, text):
        await self.server.send(name, "info", text)

    async def announce(self, text, to=None):
        await self.server.broadcast("info", text, to=to)

    def wolves(self):
        return [p for p in self.alive if self.roles.get(p) == "wolf"]

    def alive_with(self, role):
        found = [p for p in self.alive if self.roles.get(p) == role]
        return found[0] if found else None

    def status_for(self, name):
        status = {"round": self.round, "alive": sorted(self.alive),
                  "dead": sorted(p for p in self.roles if p not in self.alive),
                  "connected": self.server.connected()}
        if name in self.roles:
            status["role"] = self.roles[name]
            if self.roles[name] == "wolf":
                status["pack"] = [w for w in self.roles if self.roles[w] == "wolf"]
            if self.roles[name] == "witch":
                status["potions"] = dict(self.potions)
        return status

    async def send_role(self, name):
        role = self.roles[name]
        payload = {"role": role}
        if role == "wolf":
            payload["pack"] = [w for w in self.roles if self.roles[w] == "wolf"]
        await self.server.send(name, "role", payload)

    async def router(self):
        while True:
            name, msg_type, payload = await self.server.recv()
            try:
                if msg_type == "__join__":
                    await self.on_join(name, payload)
                elif msg_type == "__leave__":
                    await self.on_leave(name, payload)
                elif msg_type == "chat":
                    await self.on_chat(name, payload)
                elif msg_type == "action":
                    await self.on_action(name, payload)
                elif msg_type == "who":
                    await self.server.send(name, "status", self.status_for(name))
                else:
                    await self.tell(name, "Unknown request %r." % msg_type)
            except Exception as error:
                self.log("ignored bad request from %s: %r" % (name, error))
                await self.tell(name, "That request was malformed and was ignored.")

    async def on_join(self, name, info):
        if self.lobby:
            self.joined.add(name)
            self.lobby_changed.set()
            count = len(self.joined)
            self.log("%s joined from %s (%d/%d)" % (name, info["address"], count, len(self.players)))
            await self.announce("%s joined the lobby (%d/%d players)." % (name, count, len(self.players)))
            return
        if name in self.roles:
            self.log("%s reconnected from %s" % (name, info["address"]))
            await self.tell(name, "Welcome back, %s. You rejoined the game in progress." % name)
            await self.send_role(name)
            await self.server.send(name, "status", self.status_for(name))
            await self.announce("%s reconnected." % name, to=[p for p in self.server.connected() if p != name])
        else:
            self.log("%s joined late from %s and is only watching" % (name, info["address"]))
            await self.tell(name, "A game is already in progress. You can watch, but you are not playing.")

    async def on_leave(self, name, info):
        self.log("%s disconnected (%s)" % (name, info.get("reason")))
        if self.lobby:
            self.joined.discard(name)
            return
        if not self.lobby and not self.over and name in self.alive:
            await self.announce("%s lost their connection. The game continues; they can rejoin." % name)

    async def on_chat(self, name, payload):
        text = str((payload or {}).get("text", ""))[:300].strip()
        if not text:
            return
        if self.lobby:
            await self.server.broadcast("chat", {"from": name, "text": text, "room": "lobby"})
            return
        room = self.chat_rooms.get(name)
        if room is None:
            await self.tell(name, "You can't talk right now.")
            return
        label, audience = room
        await self.server.broadcast("chat", {"from": name, "text": text, "room": label}, to=audience)

    async def on_action(self, name, payload):
        payload = payload or {}
        kind = payload.get("kind")
        slot_name = {"vote": "vote", "inspect": "inspect", "heal": "witch", "poison": "witch", "pass": "witch"}.get(kind)
        slot = self.slots.get(slot_name)
        if slot is None or name not in slot["voters"]:
            await self.tell(name, "There is nothing for you to do right now.")
            return
        await slot["queue"].put((name, kind, payload.get("target")))

    async def collect(self, slot_name, voters, seconds, validate, done):
        queue = asyncio.Queue()
        self.slots[slot_name] = {"voters": set(voters), "queue": queue}
        choices = {}
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        try:
            while not done(choices):
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    name, kind, target = await asyncio.wait_for(queue.get(), remaining)
                except asyncio.TimeoutError:
                    break
                ok, value, message = validate(name, kind, target)
                if ok:
                    choices[name] = value
                await self.tell(name, message)
        finally:
            del self.slots[slot_name]
        return choices

    @staticmethod
    def tally(votes):
        if not votes:
            return None, {}
        counts = collections.Counter(votes.values())
        top = max(counts.values())
        leaders = sorted(t for t, c in counts.items() if c == top)
        return (leaders[0] if len(leaders) == 1 else None), dict(counts)

    def vote_validator(self, valid):
        def validate(name, kind, target):
            if kind != "vote" or target not in valid:
                return False, None, "Invalid vote. Valid choices: %s" % ", ".join(valid)
            return True, target, "Your vote for %s is recorded." % target
        return validate

    async def wolf_phase(self):
        pack = self.wolves()
        targets = sorted(p for p in self.alive if self.roles[p] != "wolf")
        for wolf in pack:
            self.chat_rooms[wolf] = ("wolves", list(pack))
        others = [p for p in self.server.connected() if p not in pack]
        await self.announce("The werewolves open their eyes and choose a victim...", to=others)
        for wolf in pack:
            await self.server.send(wolf, "prompt", {
                "action": "vote", "valid": targets, "seconds": WOLF_TIME,
                "text": "Werewolves %s: talk privately (just type) and agree on a victim with /vote NAME. "
                        "You have %d seconds. Victims: %s" % (", ".join(pack), WOLF_TIME, ", ".join(targets))})
        self.log("night %d: wolves %s are choosing" % (self.round, pack))
        votes = await self.collect("vote", pack, WOLF_TIME, self.vote_validator(targets),
                                   lambda v: len(v) == len(pack) and len(set(v.values())) == 1)
        for wolf in pack:
            self.chat_rooms.pop(wolf, None)
        victim, counts = self.tally(votes)
        if victim:
            await self.announce("The pack chose to eat %s." % victim, to=pack)
        else:
            await self.announce("The pack could not agree (%s). Nobody is eaten." % (counts or "no votes"), to=pack)
        self.log("night %d: wolves voted %s -> %s" % (self.round, votes, victim))
        return victim

    async def seer_phase(self):
        seer = self.alive_with("seer")
        if seer is None:
            await asyncio.sleep(random.uniform(3, SEER_TIME / 2))
            return
        valid = sorted(p for p in self.alive if p != seer)
        await self.server.send(seer, "prompt", {
            "action": "inspect", "valid": valid, "seconds": SEER_TIME,
            "text": "Seer, open your eyes. Use /inspect NAME to learn whether someone is a werewolf. "
                    "You have %d seconds. Choices: %s" % (SEER_TIME, ", ".join(valid))})

        def validate(name, kind, target):
            if kind != "inspect" or target not in valid:
                return False, None, "Invalid choice. Valid choices: %s" % ", ".join(valid)
            verdict = "IS a werewolf" if self.roles[target] == "wolf" else "is NOT a werewolf"
            return True, target, "Your vision shows that %s %s." % (target, verdict)

        seen = await self.collect("inspect", [seer], SEER_TIME, validate, lambda v: len(v) == 1)
        self.log("night %d: seer %s inspected %s" % (self.round, seer, seen.get(seer)))

    async def witch_phase(self, victim):
        witch = self.alive_with("witch")
        if witch is None or not (self.potions["heal"] or self.potions["poison"]):
            await asyncio.sleep(random.uniform(3, WITCH_TIME / 2))
            return False, None
        can_heal = bool(victim and self.potions["heal"])
        poison_targets = sorted(p for p in self.alive if p != witch) if self.potions["poison"] else []
        moves = (["heal"] if can_heal else []) + ["pass"] + ["poison " + p for p in poison_targets]
        news = "The wolves attacked %s tonight." % victim if victim else "The wolves did not feed tonight."
        await self.server.send(witch, "prompt", {
            "action": "witch", "valid": moves, "seconds": WITCH_TIME,
            "text": "Witch, open your eyes. %s Potions left: %d heal, %d poison. Use /heal, /poison NAME or /pass. "
                    "You have %d seconds." % (news, self.potions["heal"], self.potions["poison"], WITCH_TIME)})
        await self.announce("The witch is awake...", to=[p for p in self.server.connected() if p != witch])

        def validate(name, kind, target):
            if kind == "heal" and can_heal:
                return True, ("heal", None), "You used your healing potion on %s." % victim
            if kind == "poison" and target in poison_targets:
                return True, ("poison", target), "You poisoned %s." % target
            if kind == "pass":
                return True, ("pass", None), "You keep your potions."
            return False, None, "Invalid move. Valid moves: %s" % ", ".join(moves)

        choice = (await self.collect("witch", [witch], WITCH_TIME, validate, lambda v: len(v) == 1)).get(witch)
        self.log("night %d: witch %s chose %s" % (self.round, witch, choice))
        if not choice or choice[0] == "pass":
            return False, None
        if choice[0] == "heal":
            self.potions["heal"] -= 1
            return True, None
        self.potions["poison"] -= 1
        return False, choice[1]

    async def kill(self, name, how):
        self.alive.remove(name)
        self.chat_rooms.pop(name, None)
        self.log("%s died (%s); role was %s" % (name, how, self.roles[name]))
        await self.tell(name, "YOU ARE DEAD. You will keep receiving game updates but can no longer talk or vote.")

    def winner(self):
        wolves = len(self.wolves())
        if wolves == 0:
            return "The village"
        if wolves >= len(self.alive) - wolves:
            return "The werewolves"
        return None

    async def night(self):
        await self.announce("Night %d falls and the village sleeps. Everyone close your eyes." % self.round)
        victim, _ = await asyncio.gather(self.wolf_phase(), self.seer_phase())
        healed, poisoned = await self.witch_phase(victim)
        deaths = []
        if victim and not healed:
            deaths.append((victim, "eaten by werewolves"))
        if poisoned and poisoned not in [d for d, _ in deaths]:
            deaths.append((poisoned, "poisoned by the witch"))
        if not deaths:
            await self.announce("Morning comes. Nobody died last night.%s" % (" The witch saved someone!" if healed else ""))
        for name, how in deaths:
            await self.announce("Morning comes. %s was found dead, %s." % (name, how))
            await self.kill(name, how)

    async def day(self):
        town = sorted(self.alive)
        for name in town:
            self.chat_rooms[name] = ("town", list(self.players))
        await self.announce("It is day %d. Alive: %s. You have %d seconds to discuss who the werewolves are. Just type to talk."
                            % (self.round, ", ".join(town), DAY_TALK_TIME))
        await asyncio.sleep(DAY_TALK_TIME)
        for name in town:
            await self.server.send(name, "prompt", {
                "action": "vote", "valid": town, "seconds": DAY_VOTE_TIME,
                "text": "Vote now with /vote NAME. You have %d seconds. Choices: %s" % (DAY_VOTE_TIME, ", ".join(town))})
        votes = await self.collect("vote", town, DAY_VOTE_TIME, self.vote_validator(town), lambda v: len(v) == len(town))
        for name in town:
            self.chat_rooms.pop(name, None)
        hanged, counts = self.tally(votes)
        summary = ", ".join("%s->%s" % (v, t) for v, t in sorted(votes.items())) or "nobody voted"
        self.log("day %d: votes %s -> %s" % (self.round, votes, hanged))
        if hanged:
            await self.announce("Votes: %s. The village hangs %s." % (summary, hanged))
            await self.kill(hanged, "hanged by the village")
        else:
            await self.announce("Votes: %s. No majority, so nobody is hanged today." % summary)

    async def play(self):
        router = asyncio.ensure_future(self.router())
        self.log("waiting up to %ds for %s" % (CONNECT_TIMEOUT, ", ".join(self.players)))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CONNECT_TIMEOUT
        while not set(self.players) <= self.joined and loop.time() < deadline:
            self.lobby_changed.clear()
            try:
                await asyncio.wait_for(self.lobby_changed.wait(), deadline - loop.time())
            except asyncio.TimeoutError:
                break
        present = [p for p in self.players if p in self.joined]
        if len(present) < MIN_PLAYERS:
            await self.announce("Not enough players joined (%d/%d). Game cancelled." % (len(present), MIN_PLAYERS))
            self.log("cancelled: only %s joined" % present)
            router.cancel()
            return
        self.lobby = False
        self.alive = list(present)
        wolves = max(1, min(self.num_wolves, (len(present) - 1) // 2))
        deck = ["wolf"] * wolves + ["seer", "witch"]
        deck += ["villager"] * (len(present) - len(deck))
        random.shuffle(deck)
        self.roles = dict(zip(present, deck))
        self.log("roles: %s" % self.roles)
        await self.announce("The game begins with %d players: %s. Roles are being dealt." % (len(present), ", ".join(present)))
        for name in present:
            await self.send_role(name)
        await asyncio.sleep(2)
        result = None
        while result is None:
            self.round += 1
            await self.night()
            result = self.winner()
            if result:
                break
            await self.day()
            result = self.winner()
        self.over = True
        self.log("game over: %s win" % result)
        await self.server.broadcast("gameover", {"winner": result, "roles": self.roles})
        await asyncio.sleep(1)
        router.cancel()


async def moderator_console(game):
    loop = asyncio.get_running_loop()
    if not sys.stdin.isatty():
        return
    lines = asyncio.Queue()
    loop.add_reader(sys.stdin.fileno(), lambda: lines.put_nowait(sys.stdin.readline()))
    while True:
        line = (await lines.get()).strip()
        if line == "status":
            print(game.status_for(None), dict(game.server.stats), flush=True)
        elif line.startswith("say "):
            await game.announce("Moderator: " + line[4:])
        elif line.startswith("kick "):
            game.server.disconnect(line[5:].strip())
        elif line:
            print("commands: status | say TEXT | kick NAME", flush=True)


async def main():
    parser = argparse.ArgumentParser(description="Werewolves moderator (werecomm/2: TLS over TCP)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5555)
    parser.add_argument("--players", default="", help="comma-separated player names")
    parser.add_argument("--num-players", type=int, default=6)
    parser.add_argument("--wolves", type=int, default=2)
    parser.add_argument("--secrets", default="secrets", help="directory with server.pem, server.key and PLAYER.key files")
    parser.add_argument("--logs", default="logs")
    args = parser.parse_args()

    players = [p for p in args.players.split(",") if p] or ["player%d" % i for i in range(args.num_players)]
    keys = {p: load_key(os.path.join(args.secrets, p + ".key")) for p in players}
    os.makedirs(args.logs, mode=0o700, exist_ok=True)
    log_path = os.path.join(args.logs, "game-%s.log" % datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    logfile = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

    holder = {}
    server = GameServer(args.host, args.port, os.path.join(args.secrets, "server.pem"),
                        os.path.join(args.secrets, "server.key"), keys,
                        log=lambda text: holder["game"].log("[net] " + text))
    game = Game(server, players, args.wolves, logfile)
    holder["game"] = game
    await server.start()
    console = asyncio.ensure_future(moderator_console(game))
    try:
        await game.play()
    finally:
        console.cancel()
        game.log("network stats: %s" % dict(server.stats))
        await server.close()
        os.close(logfile)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
