# rtremote
![minimum tests (API version)](https://github.com/Trogious/rtremote/workflows/minimum%20tests%20(API%20version)/badge.svg) ![main test suite](https://github.com/Trogious/rtremote/workflows/main%20test%20suite/badge.svg)

`rtremote` is an intermediary server that manages the traffic between your running `rtorrent` client and the `rtorrent remote` Android app. The `rtremote` is Open Source and public. The mobile app is not.

## HOWTO
How to run the `rtremote` WebSocket server next to your running rtorrent.

### Downloading
Do not clone this repo, rather get a release from [here](https://github.com/Trogious/rtremote/releases). The repo contains tests and test dependencies you don't need.

### Requirements
- **rtorrent 0.16.x or newer**, compiled with XML-RPC support (`--with-xmlrpc-tinyxml2`)
- rtorrent SCGI socket file, usually `.rtorrent.sock`
- Python 3.9+
- Python dependencies, installed with `pip install -r requirements.txt`

**rtorrent 0.9.x is NOT supported by this version.** rtorrent 0.16 renamed several
XML-RPC commands this server relies on; if you run rtorrent 0.9.x, use
[rtremote v1.5.0](https://github.com/Trogious/rtremote/releases/tag/v1.5.0) - the
last release compatible with it.

### Running
- unpack and get into the rtremote directory
- make sure `start.sh` has correct permissions, it needs `+x` to execute
- make sure that `start.sh` (last line) executes the correct Python version
- edit the `start.sh` file, making sure you set all parameters correctly
- the secret key needs to be in SHA1 hash format; you can generate it e.g. this way: `printf '%s' 'abc123' | sha1sum`, where `abc123` is your secret key (do **not** use `echo` without `-n`: it appends a newline and produces a different hash)
- pass `-f` (or `--foreground`) to `server_wss.py` to keep it in the foreground instead of daemonizing (useful for debugging and containers)

### Security notes
- **Generate your own TLS certificate** (see `cert/howto.txt`). The certificate and keys shipped in `cert/` are public test material: anyone on the network can decrypt or impersonate a server that uses them.
- **Set your own `RTR_SECRET_KEY_SHA1`.** The server logs a warning at startup if it is running with the default (publicly known) secret.
- `RTR_LISTEN_HOST=0.0.0.0` exposes the server on all interfaces; prefer binding to a specific address if you can.

### Using
Get the `rtorrent remote` app from the Google Play store, configure it to point to this server, have fun.

### Troubleshooting
The log file (`RTR_LOG_PATH`, rotating) is written so that you can tell **which component a problem is in** without reading code. Every line has the form

```
2026-09-13 17:13:31|WARNING|app|390|registration refused for ('192.168.1.20', 51234): secret key mismatch -> the secret key entered in the app is not the one whose SHA1 is RTR_SECRET_KEY_SHA1; ...
```

The third column is the component to look at:

| Column value | Meaning | Typical lines |
| ------------ | ------- | ------------- |
| `rtorrent`   | rtorrent is down, unreachable, hung, too old, or rejected a command | SCGI socket not found / connection refused / no answer within `RTR_SCGI_TIMEOUT`; "does not know the command" (rtorrent < 0.16); "rtorrent is back after Ns" |
| `app`        | what arrived from the phone is the problem | secret key mismatch; the app rejected the TLS certificate (keystore / "accept self-signed"); the app asked for a method this rtremote does not have (update rtremote); a torrent the app acted on no longer exists; the phone stopped reading |
| `rtremote`   | this server: configuration, deployment, or a bug | certificate file missing or unreadable; listen port already in use; `RTR_DATA_ROOT` unset or not writable; anything with a traceback (please report it) |

Everything after `->` is what to check. Errors the app displays are prefixed the same way (`rtorrent: ...`, `rtremote: ...`, `app: ...`).

Startup writes a banner (`starting rtremote ...`, `config: ...`, `connected: rtorrent ...`, `listening on wss://...`); paste those lines into a bug report. An rtorrent outage is logged once when it starts and once when rtorrent is back, not on every failed poll.

`RTR_LOG_LEVEL` (default `INFO`) can be set to `DEBUG` in `start.sh` for per-message detail.

### Testing locally
The test suite in `test/` includes a fake rtorrent (`test/fake_rtorrent.py`), so the end-to-end smoke tests run on any OS without a real rtorrent:

```
pip install -r requirements.txt pytest
PYTHONPATH=. pytest test/plugins_test.py test/wss_smoke_test.py
```

The full suite (`pytest test`) additionally needs a running rtorrent configured like in `.github/workflows/main_suite.yml`.
