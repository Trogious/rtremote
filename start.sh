#!/bin/sh -
# NOTE: for any real deployment set your own RTR_SECRET_KEY_SHA1 (printf '%s' 'yoursecret' | sha1sum)
# and generate your own certificate (cert/howto.txt) - the bundled cert/ material is public test data.
RTR_ROOT=`pwd`
/usr/bin/env \
  RTR_CERT_PATH="$RTR_ROOT/cert/cert.pem" \
  RTR_RETR_INTERVAL=5 \
  RTR_SHORT_CACHE_TTL=5 \
  RTR_LISTEN_HOST="0.0.0.0" \
  RTR_LISTEN_PORT=8765 \
  RTR_SCGI_SOCKET_PATH="$RTR_ROOT/.rtorrent.sock" \
  RTR_PID_PATH="$RTR_ROOT/wss_server.pid" \
  RTR_LOG_PATH="$RTR_ROOT/rtr_wss_server.log" \
  RTR_LOG_LEVEL="INFO" \
  RTR_SECRET_KEY_SHA1="6367c48dd193d56ea7b0baad25b19455e529f5ee" \
  RTR_PLUGINS_DISK_USAGE_PATHS="/" \
  python3 $RTR_ROOT/server_wss.py $@
