"""Build an absent Ubuntu LVM image, never replace an existing backing image."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import select
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile


ISO_RELEASES = {'22.04': '22.04.5', '24.04': '24.04.3'}
ISO_HOST = 'releases.ubuntu.com'
PROBE_BYTES = 2 * 1024 * 1024


def probe_server(ip, path):
    """Read at most 2 MiB within a four-second connection/TLS/read budget."""
    started = time.monotonic()
    deadline = started + 4
    received = 0
    try:
        context = ssl.create_default_context()
        with socket.create_connection((ip, 443), timeout=2) as raw:
            raw.settimeout(max(0.01, deadline - time.monotonic()))
            with context.wrap_socket(raw, server_hostname=ISO_HOST) as tls:
                tls.settimeout(max(0.01, deadline - time.monotonic()))
                tls.sendall((f'GET {path} HTTP/1.1\r\nHost: {ISO_HOST}\r\n'
                             f'Range: bytes=0-{PROBE_BYTES - 1}\r\nConnection: close\r\n\r\n').encode())
                response = http.client.HTTPResponse(tls)
                response.begin()
                if response.status != 206:
                    return 0
                while received < PROBE_BYTES and time.monotonic() < deadline:
                    tls.settimeout(max(0.01, deadline - time.monotonic()))
                    block = response.read1(min(65536, PROBE_BYTES - received))
                    if not block:
                        break
                    received += len(block)
    except (OSError, http.client.HTTPException):
        pass
    return received / max(0.001, time.monotonic() - started)


def fastest_server(url):
    # Respect an explicitly configured proxy rather than testing a different route.
    if any(key in urllib.request.getproxies() for key in ('https', 'http', 'all')):
        print('Proxy configured; keeping the normal wget connection.', flush=True)
        return None
    try:
        # Isolate DNS resolution so a broken resolver cannot stall installation.
        result = subprocess.run([sys.executable, '-c',
            'import json,socket; print(json.dumps(list(dict.fromkeys('
            'a[4][0] for a in socket.getaddrinfo("releases.ubuntu.com",443,type=socket.SOCK_STREAM)))))'],
            capture_output=True, text=True, check=True, timeout=3)
        addresses = json.loads(result.stdout)[:6]
    except (OSError, ValueError, subprocess.SubprocessError):
        addresses = []
    if not addresses:
        print('Server discovery unavailable; using normal wget DNS selection.', flush=True)
        return None
    print(f'Testing {len(addresses)} Ubuntu server addresses in parallel (up to 4 seconds)...', flush=True)
    path = urllib.parse.urlsplit(url).path
    with ThreadPoolExecutor(max_workers=len(addresses)) as executor:
        speeds = list(executor.map(lambda ip: probe_server(ip, path), addresses))
    for ip, speed in zip(addresses, speeds):
        print(f'  {ip}: {speed / 1_000_000:.2f} MB/s', flush=True)
    fastest = max(range(len(speeds)), key=speeds.__getitem__)
    if speeds[fastest] <= 0:
        print('Speed tests unavailable; using normal wget DNS selection.', flush=True)
        return None
    print(f'Selected Ubuntu server: {addresses[fastest]}', flush=True)
    return addresses[fastest]


@contextmanager
def pinned_wget_options(ip):
    """A temporary loopback CONNECT tunnel pins DNS without changing TLS or /etc/hosts."""
    if ip is None:
        yield []
        return

    class Tunnel(BaseHTTPRequestHandler):
        def do_CONNECT(self):
            if self.path != ISO_HOST + ':443':
                self.send_error(403)
                return
            try:
                with socket.create_connection((ip, 443), timeout=10) as upstream:
                    self.send_response(200, 'Connection established')
                    self.end_headers()
                    self.connection.settimeout(30)
                    upstream.settimeout(30)
                    while True:
                        readable, _, _ = select.select([self.connection, upstream], [], [], 30)
                        if not readable:
                            return
                        for source in readable:
                            data = source.recv(65536)
                            if not data:
                                return
                            destination = upstream if source is self.connection else self.connection
                            destination.sendall(data)
            except OSError:
                return

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(('127.0.0.1', 0), Tunnel) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield ['-e', 'use_proxy=yes', '-e', 'no_proxy=', '-e',
                   f'https_proxy=http://127.0.0.1:{server.server_port}']
        finally:
            server.shutdown()
            thread.join()


def download_iso(runtime, version):
    name = f'ubuntu-{ISO_RELEASES[version]}-live-server-amd64.iso'
    base = f'https://releases.ubuntu.com/{version}/'
    cache = runtime / 'iso-cache'
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / name
    partial = cache / (name + '.part')
    if not target.exists():
        selected = fastest_server(base + name)
        print(f'Downloading ISO with wget: {name}', flush=True)
        with pinned_wget_options(selected) as options:
            subprocess.run(['wget', *options, '--continue', '--progress=bar:force',
                            '--output-document', str(partial), base + name], check=True)
    candidate = target if target.exists() else partial
    print(f'Checking ISO SHA256: {candidate.name}', flush=True)
    with urllib.request.urlopen(base + 'SHA256SUMS', timeout=60) as response:
        entries = [line.split() for line in response.read().decode().splitlines()]
    matches = [fields[0] for fields in entries
               if len(fields) == 2 and fields[1].lstrip('*') == name]
    if len(matches) != 1 or len(matches[0]) != 64 or any(
            char not in '0123456789abcdefABCDEF' for char in matches[0]):
        raise ValueError(f'No unambiguous SHA256 checksum found for {name}')
    expected = matches[0].lower()
    digest = hashlib.sha256()
    with candidate.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    if digest.hexdigest() != expected:
        # Remove only this managed cache file so a retry starts a clean download.
        candidate.unlink()
        raise ValueError(f'ISO checksum mismatch for {name}; invalid cache file removed. Retry the installer.')
    if candidate == partial:
        partial.replace(target)
    print('ISO checksum OK; using local file for Packer.', flush=True)
    return target, 'sha256:' + expected


def packer_binary(runtime):
    existing = shutil.which('packer')
    if existing:
        return existing
    version = '1.14.3'
    name = f'packer_{version}_linux_amd64.zip'
    base = f'https://releases.hashicorp.com/packer/{version}/'
    with urllib.request.urlopen(base + f'packer_{version}_SHA256SUMS', timeout=60) as response:
        sums = response.read().decode()
    expected = next(line.split()[0] for line in sums.splitlines() if line.split()[-1] == name)
    with urllib.request.urlopen(base + name, timeout=120) as response:
        archive = response.read()
    if hashlib.sha256(archive).hexdigest() != expected:
        raise ValueError('Packer checksum mismatch')
    binary = runtime / 'bin/packer'
    binary.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(archive)) as zip_file:
        binary.write_bytes(zip_file.read('packer'))
    binary.chmod(0o755)
    return str(binary)


def build(root, data, version):
    root, data = Path(root).resolve(), Path(data).resolve()
    target_dir = data / f'templates/ubuntu-{version}-lvm'
    target = target_dir / f'ubuntu-{version}-lvm.qcow2'
    if target.is_file():
        subprocess.run(['qemu-img', 'info', str(target)], check=True)
        return
    if target_dir.exists():
        raise ValueError(f'Refusing to overwrite existing output directory: {target_dir}')
    if os.uname().machine != 'x86_64':
        raise ValueError('These Ubuntu templates require an x86_64 KVM host')
    if not os.access('/dev/kvm', os.R_OK | os.W_OK):
        raise ValueError('KVM access required. Log in again after joining the kvm group.')
    runtime = root / '.runtime'
    runtime.mkdir(exist_ok=True)
    iso, checksum = download_iso(runtime, version)
    packer = packer_binary(runtime)
    template = root / f'templates/ubuntu-lvm/ubuntu-{version}-lvm.pkr.hcl'
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.build-', dir=target_dir.parent))
    output = staging / 'image'
    env = dict(os.environ, PACKER_CACHE_DIR=str(runtime / 'packer-cache'))
    try:
        subprocess.run([packer, 'init', str(template)], check=True, env=env)
        print('Building Ubuntu template with Packer (ISO download complete).', flush=True)
        subprocess.run([packer, 'build', '-var', f'output_directory={output}',
                        '-var', f'iso_url={iso}', '-var', f'iso_checksum={checksum}', str(template)],
                       check=True, env=env)
        subprocess.run(['qemu-img', 'check', str(output / target.name)], check=True)
        output.chmod(0o755)
        (output / target.name).chmod(0o644)
        # Host libvirt needs to read the backing image; install only after success.
        output.rename(target_dir)
        staging.rmdir()
    except Exception:
        print(f'Build files retained for inspection: {staging}', flush=True)
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root')
    parser.add_argument('data')
    parser.add_argument('version', choices=['22.04', '24.04'])
    args = parser.parse_args()
    build(args.root, args.data, args.version)
