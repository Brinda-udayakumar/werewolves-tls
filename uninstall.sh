#!/bin/bash
if [ "$(id -u)" -ne 0 ]; then echo "Run with sudo"; exit 1; fi
N=${1:-6}
for i in $(seq 0 $((N-1))); do userdel -r "player$i" 2>/dev/null && echo "removed player$i"; done
