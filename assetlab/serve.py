"""Serve the catalogue over HTTP, on the local network or through a tunnel.

The pages under ``out/`` link everything relatively, so a plain static server is all
they need to work in a phone browser. What they do not carry is any access control,
and the catalogue holds extracted third-party art - so this module refuses to listen
without a password.

The tunnel is why that matters. A quick tunnel hands out a public hostname, and
public hostnames get crawled within hours whether or not anyone was told the
address; a random URL is obscurity, not access control. Basic auth over the
tunnel's TLS keeps the library to the one person it is for.

Nothing is uploaded anywhere. The tunnel is a pipe to this process: it reads the
files from this disk on demand, and when the process stops the address stops
resolving.
"""

from __future__ import annotations

import argparse
import hmac
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from base64 import b64encode
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

#: cloudflared announces a quick tunnel on stderr, inside a box of dashes.
TUNNEL_URL_RE = re.compile(rb"https://[-a-z0-9]+\.trycloudflare\.com")
TUNNEL_WAIT = 40.0

#: winget installs outside the shell's PATH until it is restarted, so the exe is
#: also looked for where the package actually puts it.
CLOUDFLARED_HINTS = (
    r"%LOCALAPPDATA%\Microsoft\WinGet\Links\cloudflared.exe",
    r"%ProgramFiles%\cloudflared\cloudflared.exe",
    # The MSI is a 32-bit package even though the binary is amd64, so on a 64-bit
    # machine it lands here and never reaches PATH.
    r"%ProgramFiles(x86)%\cloudflared\cloudflared.exe",
)


class Guarded(SimpleHTTPRequestHandler):
    """A static file handler that answers nothing until Basic auth checks out."""

    credential = ""     # base64 of user:password, filled in by serve()
    verbose = False

    # A catalogue page pulls thousands of thumbnails. Under HTTP/1.0 each one costs
    # a fresh connection, and over a tunnel that handshake dominates the transfer;
    # keep-alive turns a page that crawls on a phone into one that loads.
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        if self._allowed():
            super().do_GET()

    def do_HEAD(self) -> None:
        if self._allowed():
            super().do_HEAD()

    def _allowed(self) -> bool:
        header = self.headers.get("Authorization", "")
        offered = header[6:].strip() if header.startswith("Basic ") else ""
        # compare_digest even on a mismatch, so the password cannot be recovered
        # one character at a time by timing the replies.
        if offered and hmac.compare_digest(offered, self.credential):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="AssetLab", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def end_headers(self) -> None:
        # A public hostname is a crawlable hostname. Say no before one arrives.
        self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:
        if self.verbose:
            super().log_message(fmt, *args)


def find_cloudflared() -> str | None:
    """The cloudflared executable, whether or not this shell has seen the PATH."""
    found = shutil.which("cloudflared")
    if found:
        return found
    for hint in CLOUDFLARED_HINTS:
        candidate = Path(os.path.expandvars(hint))
        if "%" not in str(candidate) and candidate.is_file():
            return str(candidate)
    return None


def start_tunnel(exe: str, port: int) -> tuple[subprocess.Popen, str | None]:
    """Run a quick tunnel to the local port, and wait for the address it prints."""
    process = subprocess.Popen(
        [exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    found: list[str] = []

    def watch() -> None:
        # readline, not `for line in`: iterating a pipe reads ahead into a buffer
        # and hands nothing over until that buffer fills, so the address sits
        # unseen for as long as cloudflared stays quiet. It also keeps draining
        # afterwards - a full pipe would block the tunnel a few minutes in.
        while True:
            line = process.stderr.readline()      # type: ignore[union-attr]
            if not line:
                return
            match = TUNNEL_URL_RE.search(line)
            if match and not found:
                found.append(match.group().decode())

    threading.Thread(target=watch, daemon=True).start()
    deadline = time.monotonic() + TUNNEL_WAIT
    while not found and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.2)
    return process, found[0] if found else None


def local_address() -> str:
    """This machine's address on the local network, for the phone to type in."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No packet is sent; the OS just picks the interface it would route out of.
        probe.connect(("10.255.255.255", 1))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def serve(directory: Path, port: int, user: str, password: str,
          tunnel: bool = False, verbose: bool = False) -> None:
    Guarded.credential = b64encode(f"{user}:{password}".encode()).decode()
    Guarded.verbose = verbose
    # Redirected to a file or a log, stdout is block-buffered, so the address and
    # password - the only two things this command exists to tell you - would sit
    # in the buffer until the server was stopped.
    sys.stdout.reconfigure(line_buffering=True)

    # With a tunnel the only client is cloudflared on this machine, so the socket
    # stays off the network entirely; without one the LAN is the whole point.
    host = "127.0.0.1" if tunnel else "0.0.0.0"
    handler = partial(Guarded, directory=str(directory))
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True

    process: subprocess.Popen | None = None
    public: str | None = None
    if tunnel:
        exe = find_cloudflared()
        if not exe:
            httpd.server_close()
            raise SystemExit(
                "cloudflared not found.\n"
                "  install: winget install --id Cloudflare.cloudflared -e\n"
                "  then open a new shell, or re-run without --tunnel for LAN only.")
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        print("opening tunnel...", flush=True)
        process, public = start_tunnel(exe, port)
        if not public:
            process.terminate()
            httpd.server_close()
            raise SystemExit("cloudflared did not report an address within "
                             f"{TUNNEL_WAIT:.0f}s - is the network up?")

    print()
    print(f"  serving   {directory}")
    print(f"  user      {user}")
    print(f"  password  {password}")
    if public:
        print(f"  address   {public}/hub.html      (anywhere)")
    else:
        print(f"  address   http://{local_address()}:{port}/hub.html   (same wi-fi)")
    print()
    print("  the address works only while this is running. ctrl-c to stop.")
    print()

    try:
        if tunnel:
            while process and process.poll() is None:
                time.sleep(0.5)
            print("tunnel closed by cloudflared")
        else:
            httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        httpd.shutdown()
        httpd.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Serve the catalogue to a phone, over the LAN or a tunnel.")
    parser.add_argument("--out", type=Path, default=Path("out"),
                        help="the catalogue to serve; use dist/ for the portable copy")
    parser.add_argument("--port", type=int, default=8732)
    parser.add_argument("--user", default="assetlab")
    parser.add_argument("--password", default=None,
                        help="one is generated and printed if not given")
    parser.add_argument("--tunnel", action="store_true",
                        help="reach it from outside the network, via cloudflared")
    parser.add_argument("--verbose", action="store_true", help="log every request")
    args = parser.parse_args()

    if not (args.out / "hub.html").is_file():
        parser.error(f"no hub.html in {args.out} - run the pipeline first")
    serve(args.out.resolve(), args.port, args.user,
          args.password or secrets.token_urlsafe(9), args.tunnel, args.verbose)


if __name__ == "__main__":
    main()
