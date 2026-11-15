#!/bin/sh
set -e
exec python -m coway_bridge --config /data/options.json --data-dir /data
