# Werewolves, Projects #2/#3 (Brinda Udayakumar)

A text Werewolves game for 4 to 8 players. Every player is their own process and their
own Linux user. A moderator process runs the game: 2 werewolves, a seer, a witch and villagers.

**Communications library: `werecomm.py` (top level of this folder, one file).**
Project 1 used UNIX domain stream sockets. Projects #2/#3 replace that with
**TLS over TCP using Python asyncio**, so the game can run across several machines, and
every player must log in with their own secret key.

## Files

| File | What it is |
|---|---|
| `werecomm.py` | **The communications library** (TLS + TCP + asyncio, login, limits) |
| `server.py` | The moderator: lobby, roles, night/day phases, voting |
| `client.py` | The player program (`--bot` plays automatically for testing) |
| `setup.sh` | Creates the Linux users player0..N-1, their keys and the TLS certificate |
| `uninstall.sh` | Removes the player users |
| `make_remote_bundle.sh` | Packs one player's files for another machine |
| `legacy/werecomm_p1.py` | The old Project 1 library (kept for the before/after tests) |
| `tests/attack_suite.py` | Runs 8 attacks against the old and the new library |
| `tests/harness.py` | Helper used by the attack suite |
| `tests/netns_demo.sh` | Emulates two machines with Linux network namespaces |

## Setup (Ubuntu, Python 3.9+, no extra packages)

```
tar -xzf brinda_werewolves.tgz
cd brinda_werewolves
sudo ./setup.sh 6            # creates player0..player5, password: werewolf
```

## Play on one machine

Terminal 1 (moderator, your normal user):
```
python3 server.py --num-players 6
```
Terminals 2 to 7, one per player:
```
su - player0                 # password: werewolf (or: sudo su - player0)
python3 client.py
```
Repeat with player1 ... player5. The game starts when everyone has joined.

Player commands: just type to talk, `/vote NAME`, `/inspect NAME` (seer),
`/heal`, `/poison NAME`, `/pass` (witch), `/who`, `/quit`.

Timers (seconds) can be changed with environment variables, for example
`WW_WOLF_TIME=120 WW_SEER_TIME=120 WW_WITCH_TIME=120 WW_DAY_TALK_TIME=60 WW_DAY_VOTE_TIME=120 python3 server.py`.
The moderator waits up to 10 minutes (`WW_CONNECT_TIMEOUT`) for everyone to join; late players can only watch.

The report PDF and poster PDF are in this folder.

## Play across machines

On the moderator machine: `python3 server.py --num-players 6` (listens on port 5555).
For each remote player: `sudo ./make_remote_bundle.sh player3`, copy
`werewolves-player3.tgz` to that machine, then:
```
tar -xzf werewolves-player3.tgz && cd werewolves-player3
python3 client.py --host MODERATOR_IP --name player3 --key player3.key --ca ca.pem
```

## Communications library API (`werecomm.py`)

Moderator side:
```
server = GameServer(host, port, certfile, keyfile, player_keys)
await server.start()                                   # TLS listener on host:port
await server.wait_for_players(names, timeout)          # -> names that are connected
await server.send(name, msg_type, payload)             # one player (unicast)
await server.broadcast(msg_type, payload, to=None, exclude=())  # everyone or a group
await server.recv(timeout)     # -> (name, msg_type, payload); also "__join__" / "__leave__"
server.connected()             # names logged in right now
server.disconnect(name)
await server.close()
```
Player side:
```
client = GameClient(host, port, name, key, cafile)
await client.connect()                     # TLS + challenge/response login
await client.send(msg_type, payload)
await client.recv(timeout)                 # -> (msg_type, payload); "__disconnect__" if lost
await client.close()
```
Helpers: `load_key(path)` (refuses keys readable by others), `new_key()`.

Wire format: 4-byte big-endian length, then UTF-8 JSON `{"type": str, "payload": any}`,
all inside TLS. Frames over 64 KiB are rejected.

Login: the server sends a random 32-byte nonce; the player replies with its name and
HMAC-SHA256(key, nonce + name). The client only accepts the moderator's own certificate.

Protection: 5 s login timeout, per-player send queue (a player that stops reading is
dropped instead of freezing the game), 8 messages/s rate limit, heartbeats, and
reconnecting to a game in progress.

## Tests

```
python3 tests/attack_suite.py 2>/dev/null   # old vs new, results in tests/results.json
sudo bash tests/netns_demo.sh          # two emulated machines, captures the network traffic
```
