# VM Foundry PoC

Self-service portal proof of concept for VMware VM provisioning. The current
version validates VM requests, creates approved VMs in vcsim through Ansible,
and simulates phpIPAM, NetBox and Nagios workflow steps. VMware inventory inspection remains available in
`vmware-test.yml`.

## Quick install

On a supported Linux host, the installer installs Docker Engine and Docker
Compose when they are missing:

```bash
git clone https://github.com/impsik/vm-automation-deployment.git
cd vm-automation-deployment
./install.sh
```

The installer asks for the local administrator password and provisioning
backend (`vcsim` or `local_qemu`), creates the required `.env` file with a
PBKDF2-SHA256 hash, generates `.runtime/config.yml` and starts the portal.
Open <http://localhost:8080> afterwards. Run `./install.sh --no-start` if you
only want to prepare the configuration.

`vcsim` is self-contained and needs no host VM storage. For `local_qemu`, the
installer also asks for the libvirt socket, QEMU image/template directory and
VM storage directory, then creates the required Compose override and maps them
to container paths.

The installer supports Debian/Ubuntu, Fedora/RHEL-compatible systems, Arch,
openSUSE and Alpine. It may ask for the `sudo` password to install system
packages and start Docker.

The generated `.env` and `.runtime/` directory are local-only and are ignored
by Git.

## Run with Docker Compose

```bash
docker compose up --build -d
```

Open <http://localhost:8080>. VMware vcsim listens on port 8989.

The PoC login uses the public ForumSys LDAP test service configured in
`portal/config.yml`. For example, ForumSys publishes the test account
`einstein` with password `password`. Replace all LDAP settings before using the
portal outside the PoC environment.

LDAP users receive the regular user view and only see requests linked to their
LDAP username.

The approval administrator uses a separate local login at
`/admin-login.html`; LDAP users do not receive the administrator role. Configure
`LOCAL_ADMIN_USERNAME` and a PBKDF2 password hash in the Compose `.env` file.
The local administrator sees every request, receives an in-portal notification
for requests in `awaiting_approval`, and can approve or reject them.

Generate a compatible password hash without storing the plaintext password in
the Compose configuration:

```bash
read -rsp "Admin password: " password; echo
ADMIN_PASSWORD="$password" python3 -c '
import hashlib, os, secrets
salt = secrets.token_bytes(16)
iterations = 600000
digest = hashlib.pbkdf2_hmac(
    "sha256", os.environ["ADMIN_PASSWORD"].encode(), salt, iterations
)
print(f"LOCAL_ADMIN_PASSWORD_HASH=pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}")
'
unset password
```

```bash
docker compose logs -f portal
docker compose down
```

Requests and audit events are persisted in a SQLite database in the
`portal-data` Docker volume.

Run the isolated API integration tests after backend or approval-workflow
changes:

```bash
docker compose exec portal python -m unittest discover -s tests -v
```

The tests use a separate temporary SQLite database and do not modify portal
requests or real virtual machines.

## Run without Docker

```bash
cd portal
python3 app.py
```

The local server writes requests to `portal/data/requests.json` by default.

## Implemented in the PoC

- standard Small, Medium and Large profiles;
- custom CPU, memory and disk sizing;
- server-side automatic and hard resource limits;
- datastore capacity preflight before approval, with a 20% free-space reserve;
- placement revalidation immediately before provisioning;
- mandatory approval for production and oversized requests;
- approve/reject actions in the request view;
- Ansible-driven VM creation in VMware vcsim;
- persistent SQLite request history and provisioning audit events;
- persistent audit records for passed and rejected preflight checks;
- LDAP login, session cookies and authentication audit events;
- role-based admin and user views with server-side authorization;
- requester, approver and rejector LDAP identities stored with requests;
- visible submit progress and duplicate hostname protection;
- configurable hostmaster DNS-registration email simulation;
- ten selectable vcsim PoC networks backed by distributed portgroups;
- responsive UI and JSON API.

Each authenticated portal user has a **Settings** view for storing OpenSSH
public keys and choose a Linux login username for each key. A VM request must
select one of that user's keys; when only one key exists it is selected
automatically. The request stores the key and username snapshot for auditing.
Cloud-init installs exactly that key for the selected account and creates the
passwordless sudo-enabled account when it is not already present in the image.

The PoC invokes `ansible/provision-vm.yml` directly. A production implementation
should start the same workflow through AWX and store the AWX job ID.

## Provisioning backend

`portal/config.yml` selects the temporary provisioning backend:

```yaml
provisioning:
  backend: local_qemu  # or vcsim
```

The `local_qemu` backend creates a qcow2 overlay from the configured Ubuntu
cloud image, applies the requested vCPU, memory and virtual disk size, creates
cloud-init metadata, defines the domain in `qemu:///system`, and starts it. The
portal request and audit model remains the same as for VMware.

The local backend uses the configurable `cloud_init_template` as user-data,
replaces the `HOSTNAME` placeholder, and appends LVM root filesystem growth settings.
The default Ubuntu 24.04 image is built by the Packer/Autoinstall definition in
`templates/ubuntu-lvm` and has an LVM-backed root filesystem. VM requests use
thin qcow2 overlays from this prebuilt image, keeping end-user provisioning
fast. The request form can also add up to 20 data disks. On first boot the guest
turns each disk into an independent LVM PV/VG/LV, formats it as ext4, and
persists its selected mountpoint in `/etc/fstab` by filesystem UUID.
After boot, the portal reads the actual IP address from the libvirt DHCP lease;
it never reports the simulated phpIPAM address for local QEMU domains.
If the lease arrives after the initial wait, the requests view reconciles pending
IP events on refresh.

Completed local QEMU machines expose a **Resize existing VM** action. It accepts
new total vCPU, RAM and OS disk values, rejects decreases, and runs
`ansible/resize-local-qemu.yml`. New domains are created with vCPU and memory
hot-add headroom (32 vCPU and at least 32 GB RAM) and a QEMU guest-agent channel.
CPU growth also activates the CPUs inside Linux through the guest agent and
verifies the online count before reporting success. A working guest agent is
checked before any resource changes, including CPU-only resizes. Submitting
the current sizes again repairs an earlier CPU hot-add that left CPUs offline;
it does not require a reboot or a further increase in the requested CPU count.
RAM growth uses persistent live DIMM attachment rather than memory ballooning.
OS and portal-managed data disk
growth uses libvirt block resize followed by dynamic mountpoint, partition, LVM
and filesystem discovery inside the guest. Multiple disks can be enlarged in
one operation and no reboot is performed. Older domains must already have
sufficient hot-add limits and a working QEMU guest agent for the corresponding
live change.

## VM lifecycle and resize recovery

Completed local QEMU machines expose self-service **Start**, graceful **Stop**
and **Reboot** actions. Shutdown and reboot prefer the QEMU guest agent and
fall back to ACPI. Every request and result is recorded in the workflow audit
events.

Before each online resize, the portal creates an atomic external qcow2 disk
snapshot and saves the inactive libvirt domain XML. A failed resize can be
retried against the same snapshot. Administrators can roll back the latest
active snapshot after stopping the VM and typing its hostname to confirm.
Rollback restores the previous domain XML and resource values. Post-snapshot
overlay files are renamed with a `.rolled-back-*` suffix and retained for
recovery instead of being deleted.

Manual snapshot restores also run through libvirt. The portal first saves the
current inactive domain definition, switches to the selected snapshot base,
and asks libvirt to create all restore overlays in one atomic disk-snapshot
operation. This keeps qcow2 ownership under libvirt control and avoids direct
portal access to protected backing files. If the operation or its disk-source
verification fails, the previous domain definition is restored and any
remaining overlay files are retained for recovery.

Resize snapshots expire 12 hours after creation, after which rollback is
disabled. An administrator can review an exact cleanup plan and confirm
**Commit & cleanup** while the VM is running. Cleanup commits each active
overlay into its base disk, pivots and verifies the live disk chain, and only
then deletes the confirmed overlay and saved XML files.

## Hostmaster email simulation

The request form collects the VM name and a mandatory full DNS name. The DNS
name must start with the VM name, while the UI hints the expected format as
`<vm-name>.*`. After successful
provisioning, the portal records a simulated hostmaster email in the request
and its workflow events; it does not send external email. Configure the PoC
recipient, short message and form default in `portal/config.yml`:

```yaml
hostmaster:
  email: hostmaster@domeen.ee
  description: Palun registreerida uue virtuaalmasina DNS-nimi.
```

## Requester email notifications

The portal can send plain-text lifecycle notifications through an SMTP server:

- successful VM provisioning with hostname, IP address, SSH login and resources;
- successful online resize with previous and new resources;
- failed provisioning or rejected requests with the detailed failure or
  administrator rejection reason.

Configure the SMTP relay and recipient in `portal/config.yml`:

```yaml
notifications:
  enabled: true
  to: imre@localhost
  from: vm-foundry@localhost
  smtp_host: 172.17.0.1
  smtp_port: 25
```

Delivery attempts are stored in the request's `notification_history` and
workflow events. An SMTP delivery failure does not change an otherwise
successful VM operation into a failed operation.

The `networks` list in the same file defines the CIDR label, description and
vcsim distributed portgroup. The CIDRs are descriptive in this PoC: selecting
one attaches the correct portgroup to the VM, but does not configure working IP
connectivity.
