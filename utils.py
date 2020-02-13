import hashlib
import json
import logging
import os
from logging.handlers import RotatingFileHandler

RTR_LOGGER_NAME = 'rtr_logger'


def get_sha1(s):
    return hashlib.sha1(s.encode(ENCODING)).hexdigest()


def getenv_path(name, default=None):
    value = os.getenv(name, default)
    if value and value.startswith('./'):
        return os.path.join(os.getcwd(), value[2:])
    return value


class Logger:
    logger = None

    @staticmethod
    def get_logger(path=getenv_path('RTR_LOG_PATH', './rtr_wss_server.log'), level=logging.INFO, max_bytes=204800,
                   backup_count=4):
        if Logger.logger is None:
            Logger.logger = logging.getLogger(RTR_LOGGER_NAME)
            Logger.logger.setLevel(level)
            handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backup_count)
            handler.setFormatter(logging.Formatter(
                '%(asctime)s|%(levelname)s|%(lineno)d|%(message)s', '%Y-%m-%d %H:%M:%S'))
            Logger.logger.addHandler(handler)
            logging.getLogger("asyncio").addHandler(handler)
        return Logger.logger


def jl(json_obj):
    Logger.get_logger().error('\n' + json.dumps(json_obj, indent=2, sort_keys=True))
