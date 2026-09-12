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
In case of issues, you can increase the logging level from INFO (default) to DEBUG by editing `utils.py`. The rotating log file location is set by `RTR_LOG_PATH`.

### Testing locally
The test suite in `test/` includes a fake rtorrent (`test/fake_rtorrent.py`), so the end-to-end smoke tests run on any OS without a real rtorrent:

```
pip install -r requirements.txt pytest
PYTHONPATH=. pytest test/plugins_test.py test/wss_smoke_test.py
```

The full suite (`pytest test`) additionally needs a running rtorrent configured like in `.github/workflows/main_suite.yml`.
