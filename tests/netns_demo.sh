#!/bin/bash
# Emulates two machines on one Linux box with network namespaces:
#   modhost    10.20.0.1  moderator + player0..player2
#   remotehost 10.20.0.2  player3..player5 (connect over the "network")
# Captures the wire with tcpdump, kills and restarts player4 mid-game to test rejoin.
set -e
if [ "$(id -u)" -ne 0 ]; then echo "run with sudo"; exit 1; fi
DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT=${1:-$DIR/demo_run}
mkdir -p "$OUT"
export WW_WOLF_TIME=${WW_WOLF_TIME:-20} WW_SEER_TIME=${WW_SEER_TIME:-12} WW_WITCH_TIME=${WW_WITCH_TIME:-12}
export WW_DAY_TALK_TIME=${WW_DAY_TALK_TIME:-15} WW_DAY_VOTE_TIME=${WW_DAY_VOTE_TIME:-15} WW_CONNECT_TIMEOUT=60
ip netns del modhost 2>/dev/null || true
ip netns del remotehost 2>/dev/null || true
ip netns add modhost
ip netns add remotehost
ip link add veth-mod type veth peer name veth-remote
ip link set veth-mod netns modhost
ip link set veth-remote netns remotehost
ip -n modhost addr add 10.20.0.1/24 dev veth-mod
ip -n remotehost addr add 10.20.0.2/24 dev veth-remote
for ns in modhost remotehost; do ip -n $ns link set lo up; done
ip -n modhost link set veth-mod up
ip -n remotehost link set veth-remote up
ip netns exec modhost tcpdump -i veth-mod -w "$OUT/wire.pcap" -U tcp port 5555 >/dev/null 2>&1 &
TCPDUMP=$!
sleep 1
cd "$DIR"
ip netns exec modhost python3 server.py --host 10.20.0.1 --num-players 6 > "$OUT/moderator.txt" 2>&1 &
SERVER=$!
sleep 1
start_player() {
  local i=$1 ns=$2
  ip netns exec $ns su player$i -c "cd ~ && python3 client.py --bot --host 10.20.0.1" >> "$OUT/player$i.txt" 2>&1 &
  echo $!
}
for i in 0 1 2; do start_player $i modhost > "$OUT/.pid$i"; sleep 0.4; done
for i in 3 4 5; do start_player $i remotehost > "$OUT/.pid$i"; sleep 0.4; done
sleep ${KILL_AFTER:-40}
echo "=== demo: killing player4's client (simulated laptop crash) ===" | tee -a "$OUT/player4.txt"
pkill -9 -u player4 -f client.py || true
sleep 5
echo "=== demo: player4 starts the client again ===" | tee -a "$OUT/player4.txt"
start_player 4 remotehost > "$OUT/.pid4"
wait $SERVER || true
sleep 2
pkill -u player0 -f client.py 2>/dev/null || true
for i in 1 2 3 4 5; do pkill -u player$i -f client.py 2>/dev/null || true; done
kill $TCPDUMP 2>/dev/null || true
sleep 1
ip netns del modhost
ip netns del remotehost
rm -f "$OUT"/.pid*
echo "done: transcripts and wire.pcap in $OUT"
