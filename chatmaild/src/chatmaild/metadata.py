import pwd

from pathlib import Path
from queue import Queue
from threading import Thread
from socketserver import (
    UnixStreamServer,
    StreamRequestHandler,
    ThreadingMixIn,
)
import sys
import logging
import os
import requests


DICTPROXY_LOOKUP_CHAR = "L"
DICTPROXY_ITERATE_CHAR = "I"
DICTPROXY_SET_CHAR = "S"
DICTPROXY_BEGIN_TRANSACTION_CHAR = "B"
DICTPROXY_COMMIT_TRANSACTION_CHAR = "C"
DICTPROXY_TRANSACTION_CHARS = "SBC"

METADATA_TOKEN_KEY = "devicetoken"


class Notifier:
    def __init__(self, vmail_dir):
        self.vmail_dir = vmail_dir
        self.to_notify_queue = Queue()

    def get_metadata_dir(self, mbox):
        "get metadata directory inside mailbox directory"
        mbox_path = self.vmail_dir.joinpath(mbox)
        if not mbox_path.exists():
            mbox_path.mkdir()
        metadata_dir = mbox_path / "metadata"
        if not metadata_dir.exists():
            metadata_dir.mkdir()
        return metadata_dir

    def set_token(self, mbox, token):
        metadata_dir = self.get_metadata_dir(mbox)
        token_path = metadata_dir / METADATA_TOKEN_KEY
        write_path = token_path.with_suffix(".tmp")
        write_path.write_text(token)
        write_path.rename(token_path)

    def del_token(self, mbox):
        metadata_dir = self.get_metadata_dir(mbox)
        if metadata_dir is not None:
            metadata_dir.joinpath(METADATA_TOKEN_KEY).unlink(missing_ok=True)

    def get_token(self, mbox):
        metadata_dir = self.get_metadata_dir(mbox)
        if metadata_dir is not None:
            token_path = metadata_dir / METADATA_TOKEN_KEY
            if token_path.exists():
                return token_path.read_text()

    def new_message_for_mbox(self, mbox):
        self.to_notify_queue.put(mbox)

    def thread_run_loop(self):
        requests_session = requests.Session()
        while 1:
            self.thread_run_one(requests_session)

    def thread_run_one(self, requests_session):
        mbox = self.to_notify_queue.get()
        token = self.get_token(mbox)
        if token:
            response = requests_session.post(
                "https://notifications.delta.chat/notify",
                data=token,
                timeout=60,
            )
            if response.status_code == 410:
                # 410 Gone status code
                # means the token is no longer valid.
                self.del_token(mbox)


def handle_dovecot_protocol(rfile, wfile, notifier):
    # HELLO message, ignored.
    msg = rfile.readline().strip().decode()

    transactions = {}
    while True:
        msg = rfile.readline().strip().decode()
        if not msg:
            break

        res = handle_dovecot_request(msg, transactions, notifier)
        if res:
            wfile.write(res.encode("ascii"))
            wfile.flush()


def handle_dovecot_request(msg, transactions, notifier):
    # see https://doc.dovecot.org/3.0/developer_manual/design/dict_protocol/
    logging.warning("handling request: %r", msg)
    short_command = msg[0]
    parts = msg[1:].split("\t")
    if short_command == DICTPROXY_LOOKUP_CHAR:
        # Lpriv/43f5f508a7ea0366dff30200c15250e3/devicetoken\tlkj123poi@c2.testrun.org
        keyparts = parts[0].split("/")
        if keyparts[0] == "priv":
            keyname = keyparts[2]
            mbox = parts[1]
            if keyname == METADATA_TOKEN_KEY:
                return f"O{notifier.get_token(mbox)}\n"
        logging.warning("lookup ignored: %r", msg)
        return "N\n"
    elif short_command == DICTPROXY_ITERATE_CHAR:
        # Empty line means ITER_FINISHED.
        # If we don't return empty line Dovecot will timeout.
        return "\n"

    if short_command not in (DICTPROXY_TRANSACTION_CHARS):
        return

    transaction_id = parts[0]

    if short_command == DICTPROXY_BEGIN_TRANSACTION_CHAR:
        mbox = parts[1]
        transactions[transaction_id] = dict(mbox=mbox, res="O\n")
    elif short_command == DICTPROXY_COMMIT_TRANSACTION_CHAR:
        # returns whether it failed or succeeded.
        return transactions.pop(transaction_id)["res"]
    elif short_command == DICTPROXY_SET_CHAR:
        # For documentation on key structure see
        # <https://github.com/dovecot/core/blob/5e7965632395793d9355eb906b173bf28d2a10ca/src/lib-storage/mailbox-attribute.h>

        keyname = parts[1].split("/")
        value = parts[2] if len(parts) > 2 else ""
        mbox = transactions[transaction_id]["mbox"]
        if keyname[0] == "priv" and keyname[2] == METADATA_TOKEN_KEY:
            notifier.set_token(mbox, value)
        elif keyname[0] == "priv" and keyname[2] == "messagenew":
            notifier.new_message_for_mbox(mbox)
        else:
            # Transaction failed.
            transactions[transaction_id]["res"] = "F\n"


class ThreadedUnixStreamServer(ThreadingMixIn, UnixStreamServer):
    request_queue_size = 100


def main():
    socket, username, vmail_dir = sys.argv[1:]
    passwd_entry = pwd.getpwnam(username)

    vmail_dir = Path(vmail_dir)

    if not vmail_dir.exists():
        logging.error("vmail dir does not exist: %r", vmail_dir)
        return 1

    notifier = Notifier(vmail_dir)

    class Handler(StreamRequestHandler):
        def handle(self):
            try:
                handle_dovecot_protocol(self.rfile, self.wfile, notifier)
            except Exception:
                logging.exception("Exception in the handler")
                raise

    try:
        os.unlink(socket)
    except FileNotFoundError:
        pass

    # start notifier thread for signalling new messages to
    # Delta Chat notification server

    t = Thread(target=notifier.thread_run_loop)
    t.setDaemon(True)
    t.start()

    with ThreadedUnixStreamServer(socket, Handler) as server:
        os.chown(socket, uid=passwd_entry.pw_uid, gid=passwd_entry.pw_gid)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass