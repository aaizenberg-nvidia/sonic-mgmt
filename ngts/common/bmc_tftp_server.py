#!/usr/bin/env python3
"""
Minimal read-only TFTP server for BMC SONiC image installation.

U-Boot on the BMC can only fetch the install FIT image over TFTP (its
'tftp' command speaks nothing else), so the deploy flow needs a TFTP server
co-located with the sonic-mgmt container that serves files from a local
directory (default /srv/tftp).
"""
import argparse
import logging
import os
import socket
import struct
import threading

# TFTP opcodes (RFC 1350)
OP_RRQ = 1
OP_WRQ = 2
OP_DATA = 3
OP_ACK = 4
OP_ERROR = 5
OP_OACK = 6

# TFTP error codes (RFC 1350)
ERR_NOT_DEFINED = 0
ERR_FILE_NOT_FOUND = 1
ERR_ACCESS_VIOLATION = 2
ERR_ILLEGAL_OP = 4

DEFAULT_BLKSIZE = 512
MIN_BLKSIZE = 8
MAX_BLKSIZE = 65464
ACK_TIMEOUT = 2.0        # seconds to wait for an ACK before retransmitting
MAX_RETRIES = 5

logger = logging.getLogger("bmc_tftp_server")


def _parse_request(payload):
    """
    Parse an RRQ/WRQ payload (everything after the 2-byte opcode):
        filename \\0 mode \\0 [opt \\0 val \\0]...
    Return (filename, mode_lower, {opt_lower: val}).
    """
    parts = payload.split(b"\x00")
    filename = parts[0].decode("latin-1") if parts else ""
    mode = parts[1].decode("latin-1").lower() if len(parts) > 1 else "octet"
    options = {}
    rest = parts[2:]
    for i in range(0, len(rest) - 1, 2):
        key = rest[i].decode("latin-1").lower()
        val = rest[i + 1].decode("latin-1")
        if key:
            options[key] = val
    return filename, mode, options


def _send_error(sock, addr, code, message):
    pkt = struct.pack("!HH", OP_ERROR, code) + message.encode("latin-1") + b"\x00"
    try:
        sock.sendto(pkt, addr)
    except OSError:
        pass


class _Transfer(threading.Thread):
    """Serve a single RRQ on its own ephemeral socket, in its own thread."""

    def __init__(self, root, client_addr, filename, options):
        super().__init__(daemon=True)
        self.root = root
        self.client_addr = client_addr
        self.filename = filename
        self.options = options

    def _safe_path(self):
        """Resolve the requested file within root; reject path traversal."""
        base = os.path.realpath(self.root)
        target = os.path.realpath(os.path.join(base, self.filename))
        if target != base and not target.startswith(base + os.sep):
            return None
        return target

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(ACK_TIMEOUT)
        try:
            path = self._safe_path()
            if not path or not os.path.isfile(path):
                logger.warning("RRQ for missing file %r from %s", self.filename, self.client_addr)
                _send_error(sock, self.client_addr, ERR_FILE_NOT_FOUND, "File not found")
                return

            filesize = os.path.getsize(path)
            blksize = DEFAULT_BLKSIZE
            oack_opts = {}

            if "blksize" in self.options:
                try:
                    blksize = max(MIN_BLKSIZE, min(int(self.options["blksize"]), MAX_BLKSIZE))
                    oack_opts["blksize"] = str(blksize)
                except ValueError:
                    pass
            if "tsize" in self.options:
                # For an RRQ the client sends tsize=0 to request the size.
                oack_opts["tsize"] = str(filesize)
            if "timeout" in self.options:
                oack_opts["timeout"] = self.options["timeout"]

            logger.info("Serving %s (%d bytes, blksize=%d) to %s",
                        path, filesize, blksize, self.client_addr)

            with open(path, "rb") as f:
                # Negotiated options require an OACK acknowledged by ACK #0
                # before the first data block is sent.
                if oack_opts and not self._send_oack_wait_ack(sock, oack_opts):
                    return
                self._send_file(sock, f, blksize)
        except Exception as e:
            logger.error("Transfer of %r to %s failed: %s", self.filename, self.client_addr, e)
            _send_error(sock, self.client_addr, ERR_NOT_DEFINED, str(e)[:100])
        finally:
            sock.close()

    def _send_oack_wait_ack(self, sock, oack_opts):
        pkt = struct.pack("!H", OP_OACK)
        for key, val in oack_opts.items():
            pkt += key.encode("latin-1") + b"\x00" + val.encode("latin-1") + b"\x00"
        for _ in range(MAX_RETRIES):
            sock.sendto(pkt, self.client_addr)
            try:
                resp, addr = sock.recvfrom(1024)
            except socket.timeout:
                continue
            if addr != self.client_addr or len(resp) < 4:
                continue
            opcode, block = struct.unpack("!HH", resp[:4])
            if opcode == OP_ACK and block == 0:
                return True
            if opcode == OP_ERROR:
                return False
        logger.error("No ACK for OACK from %s", self.client_addr)
        return False

    def _send_file(self, sock, f, blksize):
        block = 1
        while True:
            chunk = f.read(blksize)
            data_pkt = struct.pack("!HH", OP_DATA, block & 0xFFFF) + chunk
            if not self._send_block_wait_ack(sock, data_pkt, block):
                return
            # A block shorter than blksize (including a 0-byte block when the
            # file size is an exact multiple) marks the end of the transfer.
            if len(chunk) < blksize:
                logger.info("Transfer to %s complete (%d blocks)", self.client_addr, block)
                return
            block += 1

    def _send_block_wait_ack(self, sock, data_pkt, block):
        for _ in range(MAX_RETRIES):
            sock.sendto(data_pkt, self.client_addr)
            try:
                resp, addr = sock.recvfrom(1024)
            except socket.timeout:
                continue
            if addr != self.client_addr or len(resp) < 4:
                continue
            opcode, ack_block = struct.unpack("!HH", resp[:4])
            if opcode == OP_ACK and ack_block == (block & 0xFFFF):
                return True
            if opcode == OP_ERROR:
                logger.error("Client %s aborted with ERROR", self.client_addr)
                return False
        logger.error("No ACK for block %d from %s; aborting", block, self.client_addr)
        return False


def serve(root, host, port):
    root = os.path.realpath(root)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    logger.info("BMC TFTP server listening on %s:%d, root=%s", host, port, root)
    while True:
        try:
            data, addr = sock.recvfrom(4096)
        except OSError as e:
            logger.error("recvfrom failed: %s", e)
            continue
        if len(data) < 2:
            continue
        opcode = struct.unpack("!H", data[:2])[0]
        if opcode == OP_RRQ:
            filename, mode, options = _parse_request(data[2:])
            if mode != "octet":
                _send_error(sock, addr, ERR_ILLEGAL_OP, "Only octet mode is supported")
                continue
            _Transfer(root, addr, filename, options).start()
        elif opcode == OP_WRQ:
            _send_error(sock, addr, ERR_ACCESS_VIOLATION, "Read-only server")
        # Stray DATA/ACK/ERROR on the listening socket are ignored.


def main():
    parser = argparse.ArgumentParser(description="Minimal read-only TFTP server")
    parser.add_argument("--root", default="/srv/tftp", help="directory to serve")
    parser.add_argument("--host", default="0.0.0.0", help="bind address")
    parser.add_argument("--port", type=int, default=69, help="UDP port to listen on")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    serve(args.root, args.host, args.port)


if __name__ == "__main__":
    main()
