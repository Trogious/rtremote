# rtremote
![minimum tests (API version)](https://github.com/Trogious/rtremote/workflows/minimum%20tests%20(API%20version)/badge.svg) ![main test suite](https://github.com/Trogious/rtremote/workflows/main%20test%20suite/badge.svg)

`rtremote` is a intermediary server that manages the traffic between your running `rtorrent` client and `rtorrent remote` Android app. The `rtremote` is Open Source and public. The mobile app is not.

## HOWTO
How to run `rtremote` Web Sockets server next to your running rtorrent.

### Downloading
Do not clone this repo, rather get a release from [here](https://github.com/Trogious/rtremote/releases). The repo contains test and test dependencies you don't need.

### Requirements
- rtorrent compile in with `--with-xmlrpc-c` flag
- rtorrent socket file, ususally `.rtorrent.sock`
- Python 3.7+ (3.6.1+ might work as well, but `rtremote` was only tested on 3.7+)
- Python dependencies, installed with `pip install -r requirements.txt`

### Running
- unpack and get into the rtremote directory
- make sure `start.sh` has correct permissions, it needs `+x` to execute
- make sure that `start.sh` (last line) executes correct Python version
- edit the `start.sh` file, makig sure you set all parameters correctly
- secret key needs to be in SHA1 encrypted format, you can get it e.g. whis way: `echo 'abc123' |sha1sum`, where `abc123` is your secret key

### Using
Get `rtorrent remote` app from Google Play store, configure it to point to this server, have fun.

### Troubleshooting
In case of issues, you can increase logging level from INFO (default) to DEBUG by editing `utils.py`.
