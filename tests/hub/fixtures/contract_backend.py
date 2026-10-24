"""Contract fixture backend for hub tests (HB-04): a project that looks "on" to ``wb``.

``python contract_backend.py PROJECT_DIR [--snapshot-json JSON]`` holds ``.wb/backend.lock``,
writes ``backend.json``/``runtime.json`` and answers ``ui_v1`` ``hello``/``snapshot``/``attach``
with the ``backend`` identity of the running process plus the keys of ``--snapshot-json``
laid over a ready snapshot. It prints ``ready`` once a client can connect and serves until
it is killed.

HB-05 options: ``--backend-json`` lays keys over ``snapshot.backend`` (a wrong identity),
``--snapshot-delay SECONDS`` delays each ``snapshot`` answer (``hello`` is answered at once),
``--frames-log PATH`` appends one JSON line ``{"connection": n, "header": ...}`` per received
frame (connection numbers start at 1, so the count of connections is the largest number),
``--closing-on-snapshot REASON`` answers ``snapshot`` with a ``closing`` frame of that reason
and closes the connection.

HB-07 options: ``--replies-json`` maps a request type to fields laid over its ``result`` reply
(``{"pause": {"ok": false, "reason": "not_attached"}}``: an older backend),
``--close-after TYPE`` ends the connection right after answering a request of that type, first
sending ``--closing-json`` (a ``closing`` frame header, ``null`` for none: the connection just ends).

Environment (``PYTHONPATH`` to ``src``, ``XDG_RUNTIME_DIR``) comes from the test, so the
runtime root matches the one ``wb`` computes for the same project.
Not a real backend: it starts no OMP and writes no other state.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import socket
import threading
import time

from workbench.backend.project import resolve_project_context
from workbench.backend.runtime_paths import ensure_runtime_root
from workbench.contracts.ui_v1 import FrameDecoder, encode_frame


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('project_dir')
    parser.add_argument('--snapshot-json', default='{}')
    parser.add_argument('--backend-json', default='{}')
    parser.add_argument('--snapshot-delay', type=float, default=0.0)
    parser.add_argument('--frames-log')
    parser.add_argument('--closing-on-snapshot')
    parser.add_argument('--replies-json', default='{}')
    parser.add_argument('--close-after')
    parser.add_argument('--closing-json', default='null')
    args = parser.parse_args()
    context = resolve_project_context(Path(args.project_dir), dict(os.environ))
    data, runtime = context.data_root, context.runtime_root
    lock = os.open(data / 'backend.lock', os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX)
    ticks = int(Path(f'/proc/{os.getpid()}/stat').read_bytes().rsplit(b') ', 1)[1].split()[19])
    marker = json.loads((data / 'project.json').read_text())
    # The product's own preparation: every directory it creates is 0700 whatever the umask, and it
    # writes the runtime.json binding the real backend writes.
    ensure_runtime_root(runtime, data, marker['project_id'])
    reference = {'pid': os.getpid(), 'start_ticks': ticks}
    record = {'data_dir': str(data), 'project_dir': str(context.project_root), 'runtime_root': str(runtime),
              'project_id': marker['project_id'], 'uid': os.geteuid(),
              'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
              'ui_socket': str(runtime / 'ui.sock'), 'bridge_socket': str(runtime / 'bridge.sock'),
              'processes': {'backend': reference}}
    snapshot = {'phase': 'ready', 'reason': None, 'backend': dict(record, pid=os.getpid(), process=reference),
                'panes': {}, 'omp_isolation': {'state': 'ok'}, 'attached': False}
    snapshot.update(json.loads(args.snapshot_json))
    snapshot['backend'].update(json.loads(args.backend_json))
    replies, closing = json.loads(args.replies_json), json.loads(args.closing_json)
    log_lock = threading.Lock()

    def record_frame(number: int, header: dict) -> None:
        if args.frames_log:
            with log_lock, open(args.frames_log, 'a') as log:
                log.write(json.dumps({'connection': number, 'header': header}) + '\n')

    def serve(connection: socket.socket, number: int) -> None:
        with connection:
            decoder = FrameDecoder()
            while chunk := connection.recv(65536):
                for frame in decoder.feed(chunk):
                    kind = frame.header['type']
                    record_frame(number, frame.header)
                    if kind == 'snapshot' and args.snapshot_delay:
                        time.sleep(args.snapshot_delay)
                    if kind == 'snapshot' and args.closing_on_snapshot:
                        connection.sendall(encode_frame({'v': 1, 'type': 'closing',
                                                         'reason': args.closing_on_snapshot}))
                        return
                    if kind == 'hello':
                        reply = {'v': 1, 'type': 'welcome', 'version': 1}
                    else:
                        reply = {'v': 1, 'type': 'result', 'id': frame.header['id'], 'ok': True}
                        if kind in ('snapshot', 'attach'):
                            reply['snapshot'] = snapshot
                        reply.update(replies.get(kind) or {})
                    connection.sendall(encode_frame(reply))
                    if kind == args.close_after:
                        if closing is not None:
                            connection.sendall(encode_frame(closing))
                        return

    with socket.socket(socket.AF_UNIX) as server:
        server.bind(record['ui_socket'])
        server.listen()
        temporary = data / 'fixture-record'
        temporary.write_text(json.dumps(record))
        temporary.chmod(0o600)
        temporary.replace(data / 'backend.json')
        print('ready', flush=True)
        number = 0
        while True:
            connection, _ = server.accept()
            number += 1
            threading.Thread(target=serve, args=(connection, number), daemon=True).start()


if __name__ == '__main__':
    main()
