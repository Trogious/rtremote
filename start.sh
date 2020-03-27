#!/bin/sh -
RTR_ROOT=`pwd`
/usr/bin/env \
  RTR_CERT_PATH="$RTR_ROOT/cert/cert.pem" \
  RTR_RETR_INTERVAL=5 \
  RTR_SHORT_CACHE_TTL=5 \
  RTR_LISTEN_HOST="0.0.0.0" \
  RTR_LISTEN_PORT=8765 \
  SOCK_PATH="$RTR_ROOT/.rtorrent.sock" \
  RTR_PID_PATH="$RTR_ROOT/wss_server.pid" \
  RTR_LOG_PATH="$RTR_ROOT/rtr_wss_server.log" \
  RTR_SECRET_KEY_SHA1="6367c48dd193d56ea7b0baad25b19455e529f5ee" \
  RTR_PLUGINS_DISK_USAGE_PATHS="/" \
  python3 $RTR_ROOT/server_wss.py $@
