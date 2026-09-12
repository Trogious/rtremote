#!/usr/bin/env python3
"""Minimal ad-hoc WSS client for smoke testing a running rtremote server."""
import asyncio
import json
import ssl

from websockets.asyncio.client import connect


def get_ssl_context():
    # test client for a self-signed local server: no certificate verification
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    return ssl_context


def get_json_request():
    json_obj = {}
    json_obj['jsonrpc'] = '2.0'
    json_obj['id'] = 1
    json_obj['method'] = 'register'
    json_obj['params'] = {'secret_key': 'abc123', 'view': 'name'}
    return json.dumps(json_obj)


def get_json_request_files(method='get_files'):
    json_obj = {}
    json_obj['jsonrpc'] = '2.0'
    json_obj['id'] = 1
    json_obj['method'] = method
    json_obj['params'] = {'hash': '67DD1659106DCDDE0FEC4283D7B0C84B6C292675'}
    return json.dumps(json_obj)


async def hello():
    uri = 'wss://127.0.0.1:8765'
    async with connect(uri, ssl=get_ssl_context()) as websocket:
        await websocket.send(get_json_request())
        await websocket.send(get_json_request_files())
        await websocket.send(get_json_request_files('get_trackers'))
        await websocket.send(get_json_request_files('get_peers'))
        async for m in websocket:
            print(m)


if __name__ == '__main__':
    asyncio.run(hello())
