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

Requires **Python 3.9+** and **rtorrent >= 0.16** (`system.api_version` >= 26).
rtorrent 0.9.x is not supported: 0.16 renamed several XML-RPC commands this
server depends on, and v2 dropped the old-name support entirely - rtremote
v1.5.0 is the last release compatible with rtorrent 0.9.x. Pinned dependencies
are `websockets` (15.x, the current `asyncio` API), `cachetools` and `xmljson`.

## How it works

### Lifecycle

1. `start.sh` sets environment variables (cert path, socket path, secret key
   SHA1, listen host/port, polling interval, plugin config) and execs
   `server_wss.py`.
2. `server_wss.py` daemonizes (double fork, stdio redirected to `/dev/null`;
   pass `-f`/`--foreground` to skip this — handy for debugging and
   containers), writes a PID file, then runs `asyncio.run(amain())`, which:
   - creates the asyncio locks (`Cached.init_async()`) — they must be created
     **inside** the running loop because the module is imported before the
     daemon fork, and a pre-fork event loop does not survive `fork()` on
     kqueue platforms (macOS/BSD);
   - starts **`global_data_updater()`** — every `RTR_RETR_INTERVAL` seconds,
     fetches global stats + torrent list from rtorrent, computes diffs against
     the previous snapshot, and broadcasts changes to registered clients. A
     failed poll (rtorrent restarting, socket gone) is logged and retried on
     the next tick; it never kills the server;
   - starts **`short_caches_cleaner()`** — periodically wipes the short-lived
     TTL caches used for files / peers / trackers / view-hash lookups;
   - waits for the **first successful rtorrent snapshot**, then opens the TLS
     WebSocket listener (handler `on_message`). Clients can never register
     against an empty state; if rtorrent is down at startup the server waits
     (retrying) before it starts listening.
3. SIGINT / SIGTERM / SIGUSR1 stop the loop cleanly; the PID file is removed.

All rtorrent RPC calls are synchronous socket I/O and are executed via
`asyncio.to_thread(...)` so they never block the event loop; each SCGI socket
carries an `RTR_SCGI_TIMEOUT` (default 30 s) timeout as a hang guard.

### Client protocol (JSON-RPC 2.0 over WSS)

The Android app speaks JSON-RPC 2.0 over a single persistent secure WebSocket.

- **`register`** — authentication + initial snapshot. The client sends the
  plaintext `secret_key` (inside TLS) and an optional `view` name. The server
  SHA1-hashes the key and compares it (constant-time) against
  `RTR_SECRET_KEY_SHA1`. On success, the server replies with the current
  `version`, `global` data, full `torrents` list, and any `plugins` output,
  filtered/ordered by view. Subsequent diffs are pushed automatically on the
  same socket **re-using the `id` of the client's latest `register` request**;
  the client does not poll.
  - Re-sending `register` on the same socket switches the client's view (the
    app does this on fragment changes) and returns a fresh snapshot under the
    new request id; later pushes use the new id.
  - A push may occasionally be delivered *before* the register response on
    re-register (both directions are async); the app's merge logic tolerates
    this.
- **`rtremote_protocol_version`** — integer sent at the top level of the
  `register` response next to `version` (constant `RTR_PROTOCOL_VERSION` in
  `server_wss.py`, not substituted at build time). Bumped **only** when the
  wire contract changes (new method, new field, changed shape). The app treats
  an absent field as 1 (today's contract) and hides controls whose minimum
  level the server does not meet. It is not a capability list and not
  configuration: **rtremote never gates** — it is an intermediary and forwards
  whatever rtorrent accepts. Monetization (in-app purchases) lives entirely in
  the app; nothing about entitlements is ever on the wire. Planned levels:
  2 = global setters, 3 = per-torrent actions, 4 = add torrent / file
  priorities, 5 = per-torrent tuning and peers, 6 = erase with data, add
  tracker, add-torrent `directory` / `label` options, 7 = scheduled caps
  (rtremote-owned, fixed-name `schedule` entries built only from validated
  numbers) and named throttle groups, 8 = custom views and move data (the
  only feature that makes rtremote touch user files; restricted to a
  configured root).
- **`get_files` / `get_peers` / `get_trackers`** — request a per-torrent
  detail list, identified by `{"hash": "<info_hash>"}`. The hash must be a
  40-char hex string (it is embedded into an XML-RPC call; anything else is
  rejected with a JSON-RPC error). Results are served from a short TTL cache
  (`RTR_SHORT_CACHE_TTL`, default 5 s) so multiple clients hitting the same
  torrent don't hammer rtorrent.

Error handling, by client state:
- **Unauthenticated** sockets get no feedback: invalid JSON, malformed
  JSON-RPC, wrong secret, or any method before `register` ⇒ the socket is
  dropped (close code 1000). This is deliberate — no auth oracle.
- **Registered** clients get proper JSON-RPC `error` objects (`-32601` unknown
  method, `-32602` invalid hash, `-32603` rtorrent fault/internal) and the
  connection stays usable. The app routes these to `onError` and logs them.

### Views

A view is a named subset/ordering of torrents. `Cached.VIEWS` enumerates the
ones the server knows about:

`main` (default — all torrents), `name` (all torrents sorted by name),
`started`, `stopped`, `complete`, `incomplete`, `hashing`, `seeding`,
`leeching`, `active`. An unknown view name falls back to `main`.

Per-view behavior (verified against the Android app's merge logic in
`DataManager.java` / `TorrentsParser.java`):
- **Register snapshot**: for non-default views the server fetches the view's
  hash list from rtorrent (cached), filters the torrent list to view members
  (except `name`, which has all torrents) and applies the view's ordering.
- **Broadcast diffs**: only the `new` section is filtered per view — the app
  blindly `putAll`s every `new` torrent into whatever view it is showing, so
  unfiltered broadcasts would corrupt filtered views. `changed` and `del` are
  deliberately **not** filtered: the app ignores `changed`/`del` entries for
  hashes it doesn't display, and filtering `changed` would leave stale rows
  when a torrent migrates between views mid-session. A client whose filtered
  payload ends up empty receives nothing that tick.

### Diff engine (`diffs.py`)

- `map_diff(old, new)` — shallow dict diff; returns only keys whose value
  changed.
- `map_get_multi_diff(old, new)` — torrent-list diff keyed by `hash`. Produces
  a payload with up to three sections: `new` (full dicts of newly seen
  torrents), `del` (list of hashes that disappeared), and `changed` (per-hash
  dicts of just the fields that moved).

The updater only broadcasts if at least one section (or a plugin) reports a
change, so an idle client receives nothing.

### rtorrent RPC layer

- **`scgi.py`** — minimal SCGI client. Supports UNIX domain sockets
  (`./.rtorrent.sock`, optionally `unix:`/`local:` prefixed) and TCP
  (`inet:host:port`; IPv4/hostname only, no bracketed IPv6). `CONTENT_LENGTH`
  is sent first (rtorrent requires that), counted in **bytes**, and sockets
  are timeout-guarded and always closed. Netstring framing is inlined (no
  dependency).
- **`rpc.py`** (`RTorrentRpc`) — builds raw XML-RPC method calls, posts them
  over SCGI, parses responses with `xmljson.parker`, and wraps the typed
  multicalls: `d.multicall` (downloads), `t.multicall` (trackers),
  `p.multicall` (peers), `f.multicall` (files), `system.multicall` (batched
  global getters). Important details:
  - All string parameters are XML-escaped (`xml.sax.saxutils.escape`) — raw
    interpolation would let a crafted "hash" inject extra XML-RPC parameters.
  - Multicall arguments always go out as `<string>` (`string_params`); the
    type-guessing `extract_params` is only for ad-hoc `call()` users (tests) —
    guessing would corrupt an all-digit info hash into an integer.
  - XML-RPC **faults raise `RpcError`**, which the WSS layer converts into a
    JSON-RPC error for the client.
  - Empty multicall results are a childless `<data/>` in rtorrent 0.16's
    compact tinyxml2 output and parse to `None`; the guard also tolerates the
    whitespace text nodes older pretty-printing produced. Relying on one
    specific style was a known historical trap.
  - A command the running rtorrent does not know shows up as a per-command
    fault struct inside the `system.multicall` response; `Global` parsing
    turns that into an `RpcError` naming the command instead of a cryptic
    `KeyError`, and the updater logs it and retries.
- **`remote.py`** (`Remote`) — domain layer on top of the RPC. Holds the flat
  command lists for the global snapshot and the torrent list (no rtorrent
  version branching: 0.16+ has everything unconditionally). Crucially, it
  keeps the **wire protocol stable across rtorrent renames**: the JSON field
  names are part of the Android app contract, so commands renamed in rtorrent
  0.16 are requested under their new names and aliased back:
  - `network.listen.port.range` → field `network_port_range`
  - `network.http.max_total_connections` → field `network_http_max_open`
  - `system.sockets.size` → field `network_open_sockets`
  - `system.sockets.max_size` → field `network_max_open_sockets`
  - `d.tracker.has_active_not_scrape=` → field `has_active_not_scrape`
  When that flag is 1, the torrent's tracker digest (group, url,
  `is_busy_not_scrape`) is embedded under `trackers` for the app's announce
  indicator. Add new aliases in `GLOBAL_ALIASES` / `TORRENT_ALIASES` if
  rtorrent renames more commands.
- **`model.py`** — POPO containers (`Global`, `Torrent`, `Tracker`, `Peer`,
  `File`, `Client`). `add_attribute` reflects XML-RPC keys (`d.bytes_done=` →
  `bytes_done`) onto the object via `__setattr__`. Diff/serialization uses
  `__dict__` directly. Parser quirks to be aware of: `xmljson.parker` coerces
  numeric-looking strings to ints (a torrent named `12345` arrives as an int)
  and empty XML elements to `None` (an empty `d.message` becomes JSON `null`;
  the app's `optString` copes with both).

### Plugins (`plugins/`)

Lightweight extensibility. A plugin is any class exposing:

- `name()` → string label used as a JSON key
- `async get(changed_only=True)` → dict or `None`

The `changed_only` contract matters:
- the **updater** calls `get()` (i.e. `changed_only=True`): return the current
  data **only if it changed** since the last changed-only call, else `None`
  (so idle ticks stay silent), and remember what was reported;
- the **register handler** calls `get(False)`: always return current data, but
  **never update the change tracking** — otherwise a client registering
  between two ticks would swallow a change broadcast meant for everyone else.

The shipped `DiskUsage` plugin reports total/used/free summed across the
colon-separated paths in `RTR_PLUGINS_DISK_USAGE_PATHS` (nonexistent paths
contribute zero). The plugin list is hardcoded in `Cached.plugins` in
`server_wss.py`. Plugin output rides along inside the regular WSS payload
under `result.plugins.<plugin_name>`, both at register time and in change
broadcasts.

### Caching layers

- **Long-lived state**: `Cached.global_data` and `Cached.torrents` hold the
  last snapshots used to compute diffs. Guarded by `asyncio.Lock` (created in
  `Cached.init_async()`).
- **Short TTL caches**: four `TTLCache(maxsize=4096, ttl=RTR_SHORT_CACHE_TTL)`
  instances, one each for files / peers / trackers / view-hash lookups,
  guarded by `RLock` (sync, because `cachetools.cached` is sync and the
  lookups run in worker threads). The `short_caches_cleaner` task wipes them
  periodically.

### Broadcast mechanics

`Cached.notify_clients` snapshots the client set, builds one payload per view
(cached per tick), and sends to all clients **concurrently**
(`asyncio.gather`). A single send is capped at `RTR_SEND_TIMEOUT` (10 s); on
timeout the client's transport is aborted so one stuck client cannot stall
the updater or other clients. Failed sends are logged; the client registry is
cleaned up when the handler's `finally` runs.

### Deprecated rtorrent names (migrated)

rtorrent master keeps `d.multicall2`, `network.open_sockets` and
`network.max_open_sockets` only as deprecated redirects marked for removal
(`src/main.cc`). rtremote now calls `d.multicall`, `system.sockets.size` and
`system.sockets.max_size`; the wire field names (`network_open_sockets`,
`network_max_open_sockets`) are preserved via `GLOBAL_ALIASES`, and the fake
rtorrent faults on the old names so a regression is caught by the smoke
tests. `network.max_open_files.set` and
`network.http.max_total_connections.set` are no-op stubs in master: never
expose them as setters.

### Versioning

`RTR_VERSION` in `server_wss.py` is the literal string
`__RTR_VERSION_PLACEHOLDER__` in source. It is substituted at release/build
time (see the `deploy` job in `main_suite.yml`) and surfaced to the Android
client inside the `register` response. The app only treats it as a real
version if it starts with `v` — which is why release tags are `v*`.

## Files at a glance

| File                    | Role                                                       |
| ----------------------- | ---------------------------------------------------------- |
| `server_wss.py`         | Main entry. WSS server, updater loop, client registry.     |
| `remote.py`             | rtorrent command sets per API version; tracker aggregation.|
| `rpc.py`                | XML-RPC builder / response parser over SCGI; `RpcError`.   |
| `scgi.py`               | SCGI transport (UNIX + TCP), timeouts, netstring framing.  |
| `model.py`              | Data classes for Global/Torrent/Tracker/Peer/File/Client.  |
| `diffs.py`              | Map and torrent-list diff helpers.                         |
| `utils.py`              | Logger (rotating file), SHA1 helper, env-path resolver.    |
| `plugins/`              | Plugin package; ships `DiskUsage`.                         |
| `client_wss.py`         | Minimal local WSS client useful for ad-hoc smoke testing.  |
| `start.sh`              | Env-var wrapper that launches the server.                  |
| `push.sh`               | One-liner `git commit -am ... && git push` helper.         |
| `cert/`                 | TLS material + a Java keystore for the Android client. **Test/demo material — the private keys are public.** |
| `test/`                 | pytest suite incl. a fake rtorrent (see below).            |
| `test/deps/`            | Fixture torrents and a sample rtorrent.rc (0.16 syntax). |

## Configuration (environment variables)

All read at startup; `start.sh` is the canonical place to set them.

| Variable                          | Default                                | Purpose                                            |
| --------------------------------- | -------------------------------------- | -------------------------------------------------- |
| `RTR_CERT_PATH`                   | `./cert/cert.pem`                      | TLS cert + key for the WSS listener.               |
| `RTR_LISTEN_HOST`                 | `127.0.0.1`                            | WSS bind host.                                     |
| `RTR_LISTEN_PORT`                 | `8765`                                 | WSS bind port.                                     |
| `RTR_SCGI_SOCKET_PATH`            | `./.rtorrent.sock`                     | rtorrent SCGI socket (UNIX path or `inet:host:p`). |
| `RTR_SECRET_KEY_SHA1`             | SHA1 of `abc123`                       | Pre-shared auth secret (SHA1 hex). Server warns loudly when left at the default. |
| `RTR_RETR_INTERVAL`               | `5`                                    | Seconds between rtorrent polls.                    |
| `RTR_SHORT_CACHE_TTL`             | `5`                                    | TTL for files/peers/trackers/view caches.          |
| `RTR_SCGI_TIMEOUT`                | `30`                                   | Per-operation SCGI socket timeout (seconds).       |
| `RTR_PID_PATH`                    | `./wss_server.pid`                     | PID file written after daemonize.                  |
| `RTR_LOG_PATH`                    | `./rtr_wss_server.log`                 | Rotating log file (4 × 200 KiB).                   |
| `RTR_PLUGINS_DISK_USAGE_PATHS`    | `/`                                    | Colon-separated paths for the disk-usage plugin.   |

CLI flags: `-f` / `--foreground` — do not daemonize.

## Security model

- Transport is TLS-only; the Android side pins the CA via
  `cert/rtr_keystore.jks` (or optionally trusts everything if the user enables
  "accept self-signed" in the app). **Everything under `cert/` is committed,
  public test material** (including private keys and the CA passphrase) —
  fine for CI, never for a real deployment; users must generate their own
  (`cert/howto.txt`).
- Auth is a single pre-shared secret: the app sends it in plaintext inside
  TLS; the server compares `sha1(secret)` against `RTR_SECRET_KEY_SHA1` using
  `hmac.compare_digest`. SHA1 here is a **wire-protocol constant** — changing
  the algorithm breaks every existing client/config pair, so improvements
  must be coordinated with the app.
- The server is read-only toward rtorrent today: no client input reaches
  rtorrent except a strictly validated 40-hex info hash (and even that is
  XML-escaped). When write methods land, keep the same shape: every request
  is typed and validated server-side (hashes, integers, magnet / http URIs,
  labels, paths under a configured root), rtremote builds every rtorrent
  command string itself, and no client-supplied command text is ever
  forwarded (`execute.*`, `system.shutdown.*`, `session.path.set` and raw
  `schedule` strings stay unreachable).

## Testing

The `test/` package is pytest-driven:

- **`api_version_test.py` / `api_main_test.py`** — direct tests against the
  `Remote` / `RTorrentRpc` layer, asserting known field values for the fixture
  torrents in `test/deps/torrents/`. Need a real rtorrent on the SCGI socket.
- **`wss_server_test.py`** — end-to-end against a real rtorrent + the server
  started via `start.sh` (as in CI): `register` per view, `get_files`,
  `get_peers`, `get_trackers`, the `disk_usage` plugin, and live update
  propagation for global settings and per-torrent attributes.
- **`fake_rtorrent.py`** — an in-process fake rtorrent SCGI/XML-RPC server
  mimicking rtorrent 0.16 (current command names, api_version 26, compact
  tinyxml2 XML, per-command fault structs for unknown commands in
  `system.multicall`; global getters/setters, all four multicalls,
  `fake.add_torrent` / `fake.remove_torrent` control methods).
- **`wss_smoke_test.py`** — full end-to-end protocol suite against the fake,
  **no rtorrent or Linux required**; runs the real daemonized server in a
  temp dir and covers registration per view, details, diff pushes, per-view
  `new` filtering, wire-name aliasing, all error paths, view re-registration,
  and updater resilience across an rtorrent outage.
- **`plugins_test.py`** — direct unit tests for plugins.

Local quick run (any OS): `PYTHONPATH=. pytest test/plugins_test.py test/wss_smoke_test.py`

GitHub Actions runs two workflows on ubuntu-latest / Python 3.12, both of
which download a **prebuilt static rtorrent 0.16.22** from
[Trogious/rtorrent-static](https://github.com/Trogious/rtorrent-static)
(URL in the workflows' `RTORRENT_STATIC_URL` env - update it there to test
against a newer rtorrent): `basic_rpc.yml` (API-version sanity) and
`main_suite.yml` (full suite, plus the tag-triggered `deploy` job that
minifies the sources, substitutes `__RTR_VERSION_PLACEHOLDER__`, builds a
`.pyz` zipapp and uploads a GitHub release).

## Companion Android app (`../RTorrentRemote/`)

The Android client is a standard Gradle project
(`net.swmud.trog.rtorrentremote`) that talks to this server exclusively over
the WSS / JSON-RPC 2.0 protocol described above; its source is **not** part of
this repo. Facts about the app that constrain server changes (verified in its
source):

- `WssClient.java` keeps one socket and re-sends `register` (new id) when the
  user switches views; responses are routed by id (`ResponseRouter`), and a
  response must contain either `result` or `error`.
- `DataManager.onData` merges pushes: `torrents` as an **array** replaces the
  list; as an **object** it is treated as a diff with `changed` (merged only
  for known hashes), `del` (removed if present), `new` (**always added** —
  which is why the server must filter `new` per view). New torrents also
  trigger a phone notification.
- The `version` field is only honored when it starts with `v`.
- TLS: the app requests a `TLSv1.2` context; the server must keep TLS 1.2
  enabled (it currently allows 1.2+).

When modifying the protocol — adding fields, views, or methods — both sides
move together:

- New rtorrent fields → add to the command list in `remote.py`, then surface
  the corresponding attribute in the Android model. If rtorrent ever renames
  a command, keep the old wire field name via the alias maps in `remote.py` —
  the app looks fields up by exact key (see `GlobalViewModel.java`).
- New views → add to `Cached.VIEWS` here, then add the matching fragment +
  navigation entry on the Android side (`Constants.RtorrentView`).
- New plugins → drop a class in `plugins/`, register it in `Cached.plugins`,
  and add UI for `result.plugins.<name>` in the app.

The `cert/rtr_keystore.jks` is the Android trust-store companion to
`cert/cert.pem` — a self-signed CA pinned on the client side.
