"""Build an absent Ubuntu LVM image, never replace an existing backing image."""
import argparse
import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile


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
    packer = packer_binary(runtime)
    template = root / f'templates/ubuntu-lvm/ubuntu-{version}-lvm.pkr.hcl'
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.build-', dir=target_dir.parent))
    output = staging / 'image'
    env = dict(os.environ, PACKER_CACHE_DIR=str(runtime / 'packer-cache'))
    try:
        subprocess.run([packer, 'init', str(template)], check=True, env=env)
        subprocess.run([packer, 'build', '-var', f'output_directory={output}', str(template)],
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
