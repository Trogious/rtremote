import hashlib
import json
import logging
import os
from logging.handlers import RotatingFileHandler

ENCODING = 'utf8'
RTR_LOGGER_NAME = 'rtr_logger'

# Every log line carries a "where" column naming the component the problem is
# in, so a user reading the log can tell rtorrent, rtremote and the Android app
# apart without knowing the internals:
#   rtorrent  - rtorrent is down, unreachable, too old, or rejected a command
#   app       - what arrived over the WebSocket is wrong (bad secret, malformed
#               or unsupported request, TLS trust, dead phone connection)
#   rtremote  - this server: configuration, deployment, or a bug (traceback)
WHERE_RTORRENT = 'rtorrent'
WHERE_APP = 'app'
WHERE_RTREMOTE = 'rtremote'


def get_sha1(s):
    return hashlib.sha1(s.encode(ENCODING)).hexdigest()


def getenv_path(name, default=None):
    value = os.getenv(name, default)
    if value and value.startswith('./'):
        return os.path.join(os.getcwd(), value[2:])
    return value


def get_log_level():
    name = os.getenv('RTR_LOG_LEVEL', 'INFO').strip().upper()
    return logging.getLevelName(name) if isinstance(logging.getLevelName(name), int) else logging.INFO


class WhereFilter(logging.Filter):
    # assigns the "where" column to records that did not set it explicitly:
    # third-party loggers routed into our file get a default by origin
    def filter(self, record):
        if not hasattr(record, 'where'):
            msg = str(record.getMessage())
            if record.name.startswith('websockets'):
                # "server listening/closing/closed" is about us; the rest is per client
                record.where = WHERE_RTREMOTE if msg.startswith('server ') else WHERE_APP
            elif record.name == 'asyncio' and ('SSL' in msg or 'handshake' in msg.lower()):
                # TLS handshake failures surface through asyncio's transport
                record.where = WHERE_APP
                record.msg = '%s -> a client failed the TLS handshake: the app does not trust this ' \
                             'server certificate (rtr_keystore.jks / "accept self-signed"), or the ' \
                             'client is not the app' % record.getMessage()
                record.args = ()
            else:
                record.where = WHERE_RTREMOTE
        return True


class Logger:
    logger = None

    @staticmethod
    def get_logger(path=getenv_path('RTR_LOG_PATH', './rtr_wss_server.log'), level=None, max_bytes=204800,
                   backup_count=4):
        if Logger.logger is None:
            if level is None:
                level = get_log_level()
            Logger.logger = logging.getLogger(RTR_LOGGER_NAME)
            Logger.logger.setLevel(level)
            handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backup_count)
            handler.setFormatter(logging.Formatter(
                '%(asctime)s|%(levelname)s|%(where)s|%(lineno)d|%(message)s', '%Y-%m-%d %H:%M:%S'))
            handler.addFilter(WhereFilter())
            Logger.logger.addHandler(handler)
            # third-party loggers whose failures users need to see in the same file
            for name in ('asyncio', 'websockets.server'):
                third = logging.getLogger(name)
                third.addHandler(handler)
                if third.level == logging.NOTSET or third.level > logging.INFO:
                    third.setLevel(logging.INFO)
        return Logger.logger


class Diag:
    """Component-attributed logging: diag.rtorrent(...) / diag.app(...) / diag.rtremote(...).

    Each call takes a message and an optional hint (what the reader should check);
    the hint is appended after ' -> '. Line numbers point at the caller.
    """

    def __init__(self, logger):
        self.logger = logger

    def log(self, level, where, msg, hint=None, exc_info=None):
        if hint:
            msg = '%s -> %s' % (msg, hint)
        self.logger.log(level, msg, extra={'where': where}, exc_info=exc_info, stacklevel=3)

    def rtorrent(self, msg, hint=None, level=logging.ERROR, exc_info=None):
        self.log(level, WHERE_RTORRENT, msg, hint, exc_info)

    def app(self, msg, hint=None, level=logging.WARNING, exc_info=None):
        self.log(level, WHERE_APP, msg, hint, exc_info)

    def rtremote(self, msg, hint=None, level=logging.ERROR, exc_info=None):
        self.log(level, WHERE_RTREMOTE, msg, hint, exc_info)


def jl(json_obj):
    Logger.get_logger().error('\n' + json.dumps(json_obj, indent=2, sort_keys=True), extra={'where': WHERE_RTREMOTE})
