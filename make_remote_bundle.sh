#!/bin/bash
set -e
P=${1:?usage: ./make_remote_bundle.sh PLAYER_NAME}
DIR="$(cd "$(dirname "$0")" && pwd)"
OUT="werewolves-$P"
TMP=$(mktemp -d)
mkdir -p "$TMP/$OUT"
cp "$DIR/client.py" "$DIR/werecomm.py" "$TMP/$OUT/"
install -m 600 "$DIR/secrets/$P.key" "$TMP/$OUT/$P.key"
install -m 644 "$DIR/secrets/server.pem" "$TMP/$OUT/ca.pem"
cat > "$TMP/$OUT/RUN.txt" <<TXT
On the remote machine:
  tar -xzf $OUT.tgz && cd $OUT
  python3 client.py --host MODERATOR_IP --port 5555 --name $P --key $P.key --ca ca.pem
TXT
tar -czf "$DIR/$OUT.tgz" -C "$TMP" "$OUT"
rm -rf "$TMP"
chmod 600 "$DIR/$OUT.tgz"
echo "Wrote $DIR/$OUT.tgz (contains $P's secret key: copy it only to $P's machine)"
