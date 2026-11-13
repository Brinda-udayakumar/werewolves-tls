#!/bin/bash
set -e
if [ "$(id -u)" -ne 0 ]; then echo "Run with sudo: sudo ./setup.sh [NUM_PLAYERS] [PASSWORD]"; exit 1; fi
N=${1:-6}
PASS=${2:-werewolf}
DIR="$(cd "$(dirname "$0")" && pwd)"
OWNER=${SUDO_USER:-root}
SECRETS="$DIR/secrets"
install -d -m 700 "$SECRETS" "$DIR/logs"
if [ ! -f "$SECRETS/server.pem" ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=werewolves-moderator" \
    -addext "subjectAltName=DNS:werewolves-moderator" \
    -keyout "$SECRETS/server.key" -out "$SECRETS/server.pem" 2>/dev/null
  chmod 600 "$SECRETS/server.key"
fi
for i in $(seq 0 $((N-1))); do
  P=player$i
  if ! id "$P" >/dev/null 2>&1; then
    useradd -m -s /bin/bash "$P"
  fi
  echo "$P:$PASS" | chpasswd
  if [ ! -f "$SECRETS/$P.key" ]; then
    (umask 077; python3 -c "import secrets; print(secrets.token_hex(32))" > "$SECRETS/$P.key")
  fi
  H=$(getent passwd "$P" | cut -d: -f6)
  chmod 750 "$H"
  install -d -m 700 -o "$P" -g "$P" "$H/.werewolves"
  install -m 600 -o "$P" -g "$P" "$SECRETS/$P.key" "$H/.werewolves/$P.key"
  install -m 644 -o "$P" -g "$P" "$SECRETS/server.pem" "$H/.werewolves/ca.pem"
  install -m 700 -o "$P" -g "$P" "$DIR/client.py" "$DIR/werecomm.py" "$H/"
  echo "ready: $P (home $H)"
done
chown -R "$OWNER": "$SECRETS" "$DIR/logs"
echo "Secrets are in $SECRETS (mode 700, owner $OWNER). Start the moderator with: python3 server.py --num-players $N"
