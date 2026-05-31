# rtremote

## Purpose

`rtremote` is an **intermediary WebSocket server** that sits between a running
[`rtorrent`](https://github.com/rakshasa/rtorrent) BitTorrent client and a
proprietary Android app (`RTorrentRemote`, source at `../RTorrentRemote/`) that
provides a graphical view of the rtorrent state — list of torrents, per-torrent
files / peers / trackers, global stats, etc.

```
+----------------+        SCGI / XML-RPC        +-----------+   WSS / JSON-RPC 2.0   +--------------------+
|    rtorrent    | <--------------------------- | rtremote  | <--------------------> |  Android app       |
| (Unix socket)  |       (.rtorrent.sock)       | (Python)  |   (TLS WebSocket)      |  (RTorrentRemote)  |
+----------------+                              +-----------+                        +--------------------+
```

The server lives next to the rtorrent process (talking to it via the local
SCGI socket), and exposes a single TLS WebSocket endpoint that Android clients
connect to. It polls rtorrent on a fixed interval, computes deltas, and pushes
only the changes out to every connected client — so the app gets near-realtime
updates without the cost of re-sending the full state every tick.

## How it works

### Lifecycle

1. `start.sh` sets environment variables (cert path, socket path, secret key
   SHA1, listen host/port, polling interval, plugin config) and execs
   `server_wss.py`.
2. `server_wss.py` daemonizes, writes a PID file, loads the TLS cert, and
   spawns three async tasks on the event loop:
   - **`websockets.serve(...)`** — the TLS WebSocket server (handler
     `on_message`).
   - **`global_data_updater()`** — every `RTR_RETR_INTERVAL` seconds, fetches
     global stats + torrent list from rtorrent, computes diffs against the
     previous snapshot, and broadcasts any changes to all registered clients.
   - **`short_caches_cleaner()`** — periodically wipes the short-lived TTL
     caches used for files / peers / trackers / view-hash lookups.
3. SIGINT / SIGTERM / SIGUSR1 stop the loop cleanly; the PID file is removed.

### Client protocol (JSON-RPC 2.0 over WSS)

The Android app speaks JSON-RPC 2.0 over a single persistent secure WebSocket.

- **`register`** — authentication + initial snapshot. The client sends a SHA1
  secret_key (compared against `RTR_SECRET_KEY_SHA1`) and an optional view
  name. On success, the server replies with the current `version`, `global`
  data, full `torrents` list, and any `plugins` output, filtered by view.
  Subsequent diffs are pushed automatically on the same socket; the client
  does not poll.
- **`get_files` / `get_peers` / `get_trackers`** — request a per-torrent detail
  list, identified by `{"hash": "<info_hash>"}`. These results are served from
  a short TTL cache (`RTR_SHORT_CACHE_TTL`, default 5s) so multiple clients
  hitting the same torrent don't hammer rtorrent.

Unregistered or invalid requests cause the socket to be dropped.

### Views

A view is a named subset/ordering of torrents. `Cached.VIEWS` enumerates the
ones the server knows about:

`main` (default — all torrents), `name` (all torrents sorted by name),
`started`, `stopped`, `complete`, `incomplete`, `hashing`, `seeding`,
`leeching`, `active`.

For non-default views the server asks rtorrent for the view's hash list (also
cached) and filters/reorders the diff payload before sending it to that
client. Each connected client sticks with the view it registered with.

### Diff engine (`diffs.py`)

- `map_diff(old, new)` — shallow dict diff; returns only keys whose value
  changed.
- `map_get_multi_diff(old, new)` — torrent-list diff keyed by `hash`. Produces
  a payload with up to three sections: `new` (full dicts of newly seen
  torrents), `del` (list of hashes that disappeared), and `changed` (per-hash
  dicts of just the fields that moved).

The updater only broadcasts if at least one of these sections is non-empty,
so an idle client receives nothing.

### rtorrent RPC layer

- **`scgi.py`** — minimal SCGI client. Supports both UNIX domain sockets
  (`./.rtorrent.sock` or `unix:`/`local:` prefixes) and TCP (`inet:host:port`).
  Uses `pynetstring` for the SCGI header framing.
- **`rpc.py`** (`RTorrentRpc`) — builds raw XML-RPC method calls, posts them
  over SCGI, parses responses with `xmljson.parker`, and wraps the typed
  multicalls: `d.multicall2` (downloads), `t.multicall` (trackers),
  `p.multicall` (peers), `f.multicall` (files), `system.multicall` (batched
  global getters).
- **`remote.py`** (`Remote`) — domain layer on top of the RPC. Knows which
  rtorrent commands to fetch and adapts the set per detected
  `system.api_version`:
  - `GLOBAL_COMMANDS_PER_API_VERSION` adds extra global fields (e.g.
    `network.total_handshakes`, `throttle.max_unchoked_uploads`) when rtorrent
    is new enough.
  - `TORRENT_COMMANDS_PER_API_VERSION` adds `d.has_active_not_scrape=` on
    API 11+; for older versions the same flag is computed by iterating each
    torrent's trackers and checking `is_busy && latest_event != EVENT_SCRAPE`.
  This is the only place that branches on rtorrent version; the WSS layer is
  version-agnostic.
- **`model.py`** — POPO containers (`Global`, `Torrent`, `Tracker`, `Peer`,
  `File`, `Client`). `add_attribute` reflects XML-RPC keys (`d.bytes_done=` →
  `bytes_done`) onto the object via `__setattr__`. Diff/serialization uses
  `__dict__` directly.

### Plugins (`plugins/`)

Lightweight extensibility. A plugin is any class exposing:

- `name()` → string label used as a JSON key
- `async get(changed_only=True)` → dict (or `None` to suppress when
  `changed_only=True` and nothing changed)

The shipped `DiskUsage` plugin reports total/used/free across the
colon-separated paths in `RTR_PLUGINS_DISK_USAGE_PATHS`. The plugin list is
hardcoded in `Cached.plugins` in `server_wss.py`. Plugin output rides along
inside the regular WSS payload under `result.plugins.<plugin_name>`, both at
register time and in subsequent change broadcasts.

### Caching layers

- **Long-lived state**: `Cached.global_data` and `Cached.torrents` hold the
  last snapshots used to compute diffs. Guarded by `asyncio.Lock`.
- **Short TTL caches**: four `TTLCache(maxsize=4096, ttl=RTR_SHORT_CACHE_TTL)`
  instances, one each for files / peers / trackers / view-hash lookups,
  guarded by `RLock` (sync, because `cachetools.cached` is sync). The
  `short_caches_cleaner` task wipes them periodically.

### Versioning

`RTR_VERSION` in `server_wss.py` is the literal string
`__RTR_VERSION_PLACEHOLDER__` in source. It is substituted at release/build
time and surfaced to the Android client inside the `register` response so the
app can warn on incompatible servers.

## Files at a glance

| File                    | Role                                                       |
| ----------------------- | ---------------------------------------------------------- |
| `server_wss.py`         | Main entry. WSS server, updater loop, client registry.     |
| `remote.py`             | rtorrent command sets per API version; tracker aggregation.|
| `rpc.py`                | XML-RPC builder / response parser over SCGI.               |
| `scgi.py`               | SCGI transport (UNIX + TCP).                               |
| `model.py`              | Data classes for Global/Torrent/Tracker/Peer/File/Client.  |
| `diffs.py`              | Map and torrent-list diff helpers.                         |
| `utils.py`              | Logger (rotating file), SHA1 helper, env-path resolver.    |
| `plugins/`              | Plugin package; ships `DiskUsage`.                         |
| `client_wss.py`         | Minimal local WSS client useful for ad-hoc smoke testing.  |
| `start.sh`              | Env-var wrapper that launches the server.                  |
| `upRtorrent.sh`         | Spins up a local `rtorrent` configured for the test suite. |
| `cert/`                 | TLS material + a Java keystore for the Android client.     |
| `alt/`                  | `.torrent` files used as fixtures.                         |
| `test/`                 | pytest suite (see below).                                  |
| `rtorrent-0.9.{6,7,8}`  | Bundled rtorrent binaries used in CI / local testing.      |
| `x.py`, `x.xml`, `y.xml`| One-off helpers that template Android view fragments/menus from a `_X_` / `_Y_` source — used to keep the Android `RTorrentRemote` app's per-view fragment XML in sync with the view list this server supports. |

## Configuration (environment variables)

All read by `server_wss.py` at startup; `start.sh` is the canonical place to
set them.

| Variable                          | Default                                | Purpose                                            |
| --------------------------------- | -------------------------------------- | -------------------------------------------------- |
| `RTR_CERT_PATH`                   | `./cert/cert.pem`                      | TLS cert + key for the WSS listener.               |
| `RTR_LISTEN_HOST`                 | `127.0.0.1`                            | WSS bind host.                                     |
| `RTR_LISTEN_PORT`                 | `8765`                                 | WSS bind port.                                     |
| `RTR_SCGI_SOCKET_PATH`            | `./.rtorrent.sock`                     | rtorrent SCGI socket (UNIX path or `inet:host:p`). |
| `RTR_SECRET_KEY_SHA1`             | SHA1 of `abc123`                       | Pre-shared auth secret (SHA1 hex).                 |
| `RTR_RETR_INTERVAL`               | `5`                                    | Seconds between rtorrent polls.                    |
| `RTR_SHORT_CACHE_TTL`             | `5`                                    | TTL for files/peers/trackers/view caches.          |
| `RTR_PID_PATH`                    | `./wss_server.pid`                     | PID file written after daemonize.                  |
| `RTR_LOG_PATH`                    | `./rtr_wss_server.log`                 | Rotating log file (4 × 200 KiB).                   |
| `RTR_PLUGINS_DISK_USAGE_PATHS`    | `/`                                    | Colon-separated paths for the disk-usage plugin.   |

## Testing

The `test/` package is pytest-driven and exercises two layers:

- **`api_version_test.py` / `api_main_test.py`** — direct tests against the
  `Remote` / `RTorrentRpc` layer, asserting known field values for fixtures in
  `alt/`.
- **`wss_server_test.py`** — end-to-end. Spawns a real `rtorrent`
  (`upRtorrent.sh`), connects over WSS, registers, and verifies the JSON
  payloads for `register` (per view), `get_files`, `get_peers`,
  `get_trackers`, the `disk_usage` plugin, and live update propagation for
  both global settings and per-torrent attributes.
- **`plugins_test.py`** — direct unit tests for plugins.

GitHub Actions runs two workflows: `basic_rpc.yml` (minimum API version
sanity) and `main_suite.yml` (full WSS suite).

## Companion Android app (`../RTorrentRemote/`)

The Android client is a standard Gradle project
(`net.swmud.trog.rtorrentremote`) that talks to this server exclusively over
the WSS / JSON-RPC 2.0 protocol described above. Its source lives at
`/t/workspace/RTorrentRemote/` and is **not** part of this repo. When
modifying the protocol — adding fields, views, or methods — both sides need to
move together:

- New rtorrent fields → add to the command list in `remote.py`, then surface
  the corresponding attribute in the Android model.
- New views → add to `Cached.VIEWS` here, then add a navigation entry on the
  Android side (the `x.py` + `x.xml` / `y.xml` templates exist to generate the
  Android fragment + menu XML for each view name in lockstep).
- New plugins → drop a class in `plugins/`, register it in `Cached.plugins`,
  and add UI for `result.plugins.<name>` in the app.

The `cert/rtr_keystore.jks` is the Android trust store companion to
`cert/cert.pem` — a self-signed cert pinned on the client side.
