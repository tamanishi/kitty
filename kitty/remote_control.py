#!/usr/bin/env python3
# License: GPL v3 Copyright: 2018, Kovid Goyal <kovid at kovidgoyal.net>

import json
import os
import re
import sys
from contextlib import suppress
from functools import partial
from time import monotonic
from types import GeneratorType
from typing import (
    Any, Dict, Iterable, Iterator, List, Optional, Tuple, Union, cast
)

from .cli import emph, parse_args
from .cli_stub import RCOptions
from .constants import appname, version
from .fast_data_types import get_boss, read_command_response, send_data_to_peer
from .rc.base import (
    NoResponse, ParsingOfArgsFailed, PayloadGetter, all_command_names,
    command_for_name, parse_subcommand_cli
)
from .types import AsyncResponse
from .typing import BossType, WindowType
from .utils import TTYIO, parse_address_spec

active_async_requests: Dict[str, float] = {}


def encode_response_for_peer(response: Any) -> bytes:
    return b'\x1bP@kitty-cmd' + json.dumps(response).encode('utf-8') + b'\x1b\\'


def handle_cmd(boss: BossType, window: Optional[WindowType], serialized_cmd: str, peer_id: int) -> Union[Dict[str, Any], None, AsyncResponse]:
    cmd = json.loads(serialized_cmd)
    v = cmd['version']
    no_response = cmd.get('no_response', False)
    if tuple(v)[:2] > version[:2]:
        if no_response:
            return None
        return {'ok': False, 'error': 'The kitty client you are using to send remote commands is newer than this kitty instance. This is not supported.'}
    c = command_for_name(cmd['cmd'])
    payload = cmd.get('payload') or {}
    payload['peer_id'] = peer_id
    async_id = str(cmd.get('async', ''))
    if async_id:
        payload['async_id'] = async_id
        if 'cancel_async' in cmd:
            active_async_requests.pop(async_id, None)
            c.cancel_async_request(boss, window, PayloadGetter(c, payload))
            return None
        active_async_requests[async_id] = monotonic()
        if len(active_async_requests) > 32:
            oldest = next(iter(active_async_requests))
            del active_async_requests[oldest]
    try:
        ans = c.response_from_kitty(boss, window, PayloadGetter(c, payload))
    except Exception:
        if no_response:  # don't report errors if --no-response was used
            return None
        raise
    if isinstance(ans, NoResponse):
        return None
    if isinstance(ans, AsyncResponse):
        return ans
    response: Dict[str, Any] = {'ok': True}
    if ans is not None:
        response['data'] = ans
    if not c.no_response and not no_response:
        return response
    return None


global_options_spec = partial('''\
--to
An address for the kitty instance to control. Corresponds to the address
given to the kitty instance via the :option:`kitty --listen-on` option. If not specified,
messages are sent to the controlling terminal for this process, i.e. they
will only work if this process is run within an existing kitty window.
'''.format, appname=appname)


def encode_send(send: Any) -> bytes:
    es = ('@kitty-cmd' + json.dumps(send)).encode('ascii')
    return b'\x1bP' + es + b'\x1b\\'


class SocketIO:

    def __init__(self, to: str):
        self.family, self.address = parse_address_spec(to)[:2]

    def __enter__(self) -> None:
        import socket
        self.socket = socket.socket(self.family)
        self.socket.setblocking(True)
        self.socket.connect(self.address)

    def __exit__(self, *a: Any) -> None:
        import socket
        with suppress(OSError):  # on some OSes such as macOS the socket is already closed at this point
            self.socket.shutdown(socket.SHUT_RDWR)
        self.socket.close()

    def send(self, data: Union[bytes, Iterable[Union[str, bytes]]]) -> None:
        import socket
        with self.socket.makefile('wb') as out:
            if isinstance(data, bytes):
                out.write(data)
            else:
                for chunk in data:
                    if isinstance(chunk, str):
                        chunk = chunk.encode('utf-8')
                    out.write(chunk)
                    out.flush()
        self.socket.shutdown(socket.SHUT_WR)

    def simple_recv(self, timeout: float) -> bytes:
        dcs = re.compile(br'\x1bP@kitty-cmd([^\x1b]+)\x1b\\')
        self.socket.settimeout(timeout)
        with self.socket.makefile('rb') as src:
            data = src.read()
        m = dcs.search(data)
        if m is None:
            raise TimeoutError('Timed out while waiting to read cmd response')
        return bytes(m.group(1))


class RCIO(TTYIO):

    def simple_recv(self, timeout: float) -> bytes:
        ans: List[bytes] = []
        read_command_response(self.tty_fd, timeout, ans)
        return b''.join(ans)


def do_io(to: Optional[str], send: Dict[str, Any], no_response: bool, response_timeout: float) -> Dict[str, Any]:
    payload = send.get('payload')
    if not isinstance(payload, GeneratorType):
        send_data: Union[bytes, Iterator[bytes]] = encode_send(send)
    else:
        def send_generator() -> Iterator[bytes]:
            assert payload is not None
            for chunk in payload:
                send['payload'] = chunk
                yield encode_send(send)
        send_data = send_generator()

    io: Union[SocketIO, RCIO] = SocketIO(to) if to else RCIO()
    with io:
        io.send(send_data)
        if no_response:
            return {'ok': True}
        received = io.simple_recv(timeout=response_timeout)

    return cast(Dict[str, Any], json.loads(received.decode('ascii')))


cli_msg = (
        'Control {appname} by sending it commands. Set the'
        ' :opt:`allow_remote_control` option to yes in :file:`kitty.conf` for this'
        ' to work.'
).format(appname=appname)


def parse_rc_args(args: List[str]) -> Tuple[RCOptions, List[str]]:
    cmap = {name: command_for_name(name) for name in sorted(all_command_names())}
    cmds = (f'  :green:`{cmd.name}`\n    {cmd.short_desc}' for c, cmd in cmap.items())
    msg = cli_msg + (
            '\n\n:title:`Commands`:\n{cmds}\n\n'
            'You can get help for each individual command by using:\n'
            '{appname} @ :italic:`command` -h').format(appname=appname, cmds='\n'.join(cmds))
    return parse_args(args[1:], global_options_spec, 'command ...', msg, f'{appname} @', result_class=RCOptions)


def create_basic_command(name: str, payload: Any = None, no_response: bool = False, is_asynchronous: bool = False) -> Dict[str, Any]:
    ans = {'cmd': name, 'version': version, 'no_response': no_response}
    if payload is not None:
        ans['payload'] = payload
    if is_asynchronous:
        from kitty.short_uuid import uuid4
        ans['async'] = uuid4()
    return ans


def send_response_to_client(data: Any = None, error: str = '', peer_id: int = 0, window_id: int = 0, async_id: str = '') -> None:
    ts = active_async_requests.pop(async_id, None)
    if ts is None:
        return
    if error:
        response: Dict[str, Union[bool, int, str]] = {'ok': False, 'error': error}
    else:
        response = {'ok': True, 'data': data}
    if peer_id > 0:
        send_data_to_peer(peer_id, encode_response_for_peer(response))
    elif window_id > 0:
        w = get_boss().window_id_map[window_id]
        if w is not None:
            w.send_cmd_response(response)


def main(args: List[str]) -> None:
    global_opts, items = parse_rc_args(args)

    if not items:
        from kitty.shell import main as smain
        smain(global_opts)
        return
    cmd = items[0]
    try:
        c = command_for_name(cmd)
    except KeyError:
        raise SystemExit('{} is not a known command. Known commands are: {}'.format(
            emph(cmd), ', '.join(x.replace('_', '-') for x in all_command_names())))
    opts, items = parse_subcommand_cli(c, items)
    try:
        payload = c.message_to_kitty(global_opts, opts, items)
    except ParsingOfArgsFailed as err:
        exit(str(err))
    no_response = c.no_response
    if hasattr(opts, 'no_response'):
        no_response = opts.no_response
    response_timeout = c.response_timeout
    if hasattr(opts, 'response_timeout'):
        response_timeout = opts.response_timeout
    send = create_basic_command(cmd, payload=payload, no_response=no_response, is_asynchronous=c.is_asynchronous)
    listen_on_from_env = False
    if not global_opts.to and 'KITTY_LISTEN_ON' in os.environ:
        global_opts.to = os.environ['KITTY_LISTEN_ON']
        listen_on_from_env = False
    if global_opts.to:
        try:
            parse_address_spec(global_opts.to)
        except Exception:
            msg = f'Invalid listen on address: {global_opts.to}'
            if listen_on_from_env:
                msg += '. The KITTY_LISTEN_ON environment variable is set incorrectly'
            exit(msg)
    import socket
    try:
        response = do_io(global_opts.to, send, no_response, response_timeout)
    except (TimeoutError, socket.timeout):
        send.pop('payload', None)
        send['cancel_async'] = True
        do_io(global_opts.to, send, True, 10)
        raise SystemExit(f'Timed out after {response_timeout} seconds waiting for response from kitty')
    if no_response:
        return
    if not response.get('ok'):
        if response.get('tb'):
            print(response['tb'], file=sys.stderr)
        raise SystemExit(response['error'])
    data = response.get('data')
    if data is not None:
        if c.string_return_is_error and isinstance(data, str):
            raise SystemExit(data)
        print(data)
