#!/usr/bin/env bash

set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly ENV_FILE="$SCRIPT_DIR/.env"
readonly RUNTIME_DIR="$SCRIPT_DIR/.runtime"
readonly RUNTIME_CONFIG="$RUNTIME_DIR/config.yml"
readonly RUNTIME_COMPOSE_OVERRIDE="$RUNTIME_DIR/docker-compose.local-qemu.yml"

DOCKER=(docker)
COMPOSE=(docker compose)

usage() {
    cat <<'EOF'
Usage: ./install.sh [--no-start]

Prepare and start the VM Foundry self-service portal.

The installer:
  - installs Docker Engine and Docker Compose when missing;
  - creates .env with a local administrator account;
  - generates a PBKDF2-SHA256 password hash;
  - generates configuration for the selected vcsim or local_qemu backend;
  - builds and starts the portal with Docker Compose.

Options:
  --no-start  prepare the configuration but do not start containers
  -h, --help  show this help
EOF
}

die() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

log() {
    printf '\n==> %s\n' "$*"
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

run_as_root() {
    if [[ "$EUID" -eq 0 ]]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        die "Installing system packages requires root or sudo access"
    fi
}

run_docker() {
    "${DOCKER[@]}" "$@"
}

run_compose() {
    "${COMPOSE[@]}" "$@"
}

apt_has_candidate() {
    apt-cache policy "$1" 2>/dev/null \
        | awk '$1 == "Candidate:" && $2 != "(none)" { found = 1 } END { exit !found }'
}

install_docker_packages() {
    local distro_id
    distro_id="$(. /etc/os-release; printf '%s' "${ID:-}")"

    log "Installing Docker dependencies"
    case "$distro_id" in
        debian|ubuntu|linuxmint|pop)
            run_as_root apt-get update
            apt_has_candidate docker.io || die "Docker package is unavailable in the configured APT repositories"
            run_as_root apt-get install -y docker.io

            if ! docker compose version >/dev/null 2>&1 && ! docker-compose version >/dev/null 2>&1; then
                if apt_has_candidate docker-compose-plugin; then
                    run_as_root apt-get install -y docker-compose-plugin
                elif apt_has_candidate docker-compose-v2; then
                    run_as_root apt-get install -y docker-compose-v2
                elif apt_has_candidate docker-compose; then
                    run_as_root apt-get install -y docker-compose
                else
                    die "Docker Compose package is unavailable in the configured APT repositories"
                fi
            fi
            ;;
        fedora)
            run_as_root dnf install -y docker docker-compose-plugin
            ;;
        rhel|rocky|almalinux|centos)
            if command -v dnf >/dev/null 2>&1; then
                run_as_root dnf install -y docker docker-compose-plugin
            else
                run_as_root yum install -y docker docker-compose-plugin
            fi
            ;;
        arch|manjaro)
            run_as_root pacman -Sy --needed --noconfirm docker docker-compose
            ;;
        opensuse*|sles)
            run_as_root zypper --non-interactive install docker docker-compose
            ;;
        alpine)
            run_as_root apk add docker docker-cli-compose
            ;;
        *)
            die "Unsupported Linux distribution '$distro_id'. Install Docker Engine and Docker Compose manually."
            ;;
    esac
}

ensure_docker() {
    local compose_as_docker=false
    local target_user="${SUDO_USER:-${USER:-}}"

    if ! command -v docker >/dev/null 2>&1; then
        install_docker_packages
    elif docker compose version >/dev/null 2>&1 || docker-compose version >/dev/null 2>&1; then
        :
    else
        install_docker_packages
    fi

    command -v docker >/dev/null 2>&1 || die "Docker installation did not provide the docker command"

    if command -v systemctl >/dev/null 2>&1; then
        run_as_root systemctl enable --now docker >/dev/null 2>&1 || true
    elif command -v service >/dev/null 2>&1; then
        run_as_root service docker start >/dev/null 2>&1 || true
    fi

    if [[ -n "$target_user" ]] && getent group docker >/dev/null 2>&1; then
        if ! id -nG "$target_user" 2>/dev/null | tr ' ' '\n' | grep -Fxq docker; then
            run_as_root usermod -aG docker "$target_user"
            printf '%s was added to the docker group; a new login is required for direct Docker access.\n' "$target_user"
        fi
    fi

    if docker info >/dev/null 2>&1; then
        DOCKER=(docker)
    elif command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
        DOCKER=(sudo docker)
        printf 'Docker is available through sudo until you log in again after the group change.\n'
    else
        die "Docker is installed but the daemon is not running or the current user cannot access it"
    fi

    if run_docker compose version >/dev/null 2>&1; then
        COMPOSE=("${DOCKER[@]}" compose)
        compose_as_docker=true
    elif command -v docker-compose >/dev/null 2>&1; then
        if [[ "${DOCKER[0]}" == "sudo" ]]; then
            COMPOSE=(sudo docker-compose)
        else
            COMPOSE=(docker-compose)
        fi
    else
        die "Docker Compose is not available after installation"
    fi

    if [[ "$compose_as_docker" == true ]]; then
        log "Docker Compose plugin is ready"
    else
        log "Docker Compose standalone command is ready"
    fi
}

set_env_value() {
    local key="$1"
    local value="$2"
    local temporary

    temporary="$(mktemp "$ENV_FILE.XXXXXX")"
    awk -v key="$key" -v value="$value" '
        BEGIN { replaced = 0 }
        $0 ~ "^" key "=" {
            print key "=" value
            replaced = 1
            next
        }
        { print }
        END {
            if (!replaced) print key "=" value
        }
    ' "$ENV_FILE" >"$temporary"
    chmod 600 "$temporary"
    mv -- "$temporary" "$ENV_FILE"
}

read_env_value() {
    local key="$1"
    local value

    value="$(awk -F= -v key="$key" '$1 == key { sub(/^[^=]*=/, ""); print; exit }' "$ENV_FILE" 2>/dev/null || true)"
    if [[ "$value" == \'*\' && "$value" == *\' ]]; then
        value="${value:1:${#value}-2}"
    fi
    printf '%s' "$value"
}

choose_backend() {
    local current backend
    current="$(read_env_value PROVISIONING_BACKEND)"
    current="${current:-vcsim}"

    while :; do
        read -r -p "Provisioning backend [vcsim/local_qemu] ($current): " backend
        backend="${backend:-$current}"
        case "$backend" in
            vcsim|local_qemu) break ;;
            *) printf 'Choose either vcsim or local_qemu.\n' >&2 ;;
        esac
    done

    set_env_value PROVISIONING_BACKEND "$backend"
    printf '%s' "$backend"
}

configure_local_qemu_paths() {
    local previous data storage cloud socket version answer owner path qemu_user ancestor
    previous="$(read_env_value VM_FOUNDRY_DATA_DIR)"
    previous="${previous:-$(read_env_value KVM_READONLY_PATH)}"
    read -r -p "VM Foundry data directory [${previous:-/var/lib/vm-foundry}]: " data
    data="${data:-${previous:-/var/lib/vm-foundry}}"
    [[ "$data" =~ ^/[A-Za-z0-9_./-]+$ ]] || die "Use an absolute data path without spaces or special characters"
    data="$(realpath -m "$data")"
    [[ "$data" != / && "$data" != /var && "$data" != /var/lib ]] || die "Choose a dedicated application data directory"
    if [[ -n "$previous" && "$data" != "$(realpath -m "$previous")" ]]; then
        die "Existing deployment uses $previous. Moving VM disks requires a separate migration."
    fi
    storage="$(read_env_value KVM_STORAGE_PATH)"
    storage="${storage:-$data/instances}"
    cloud="$(read_env_value CLOUD_INIT_TEMPLATE)"
    if [[ -z "$cloud" && -f "$data/cloud_init.cfg.orig" ]]; then
        cloud="$data/cloud_init.cfg.orig"
    fi
    cloud="${cloud:-$data/cloud-init/user-data.yml}"
    socket="$(read_env_value LIBVIRT_SOCKET_PATH)"
    socket="${socket:-/var/run/libvirt/libvirt-sock}"
    ensure_local_qemu
    [[ -S "$socket" ]] || die "Libvirt socket missing: $socket. Start libvirtd or virtproxyd."
    owner="${SUDO_USER:-$(id -un)}"
    for path in "$data" "$data/templates" "$data/cloud-init" "$storage"; do
        if [[ ! -d "$path" ]]; then
            run_as_root install -d -m 0755 -o "$owner" "$path"
        fi
    done
    [[ -w "$data/templates" ]] || die "Template directory must be writable by the installer user: $data/templates"
    if [[ ! -f "$cloud" ]]; then
        run_as_root install -m 0644 "$SCRIPT_DIR/templates/cloud-init/user-data.yml" "$cloud"
    fi
    # Grant the container UID access without changing existing disk ownership.
    if [[ ! -w "$storage" || "$(id -u)" != 1000 ]]; then
        run_as_root setfacl -m u:1000:rwx,d:u:1000:rwx "$storage"
    fi
    # Shared ancestors must be traversable by host QEMU; report custom-path issues.
    run_as_root setfacl -m u:1000:rx "$data"
    for qemu_user in libvirt-qemu qemu; do
        if id "$qemu_user" >/dev/null 2>&1; then
            ancestor="$data"
            while [[ "$ancestor" != / ]]; do
                run_as_root setfacl -m "u:$qemu_user:rx" "$ancestor"
                ancestor="$(dirname "$ancestor")"
            done
        fi
    done
    set_env_value VM_FOUNDRY_DATA_DIR "$data"
    set_env_value KVM_READONLY_PATH "$data"
    set_env_value KVM_STORAGE_PATH "$storage"
    set_env_value CLOUD_INIT_TEMPLATE "$cloud"
    set_env_value LIBVIRT_SOCKET_PATH "$socket"

    version="$(read_env_value UBUNTU_VERSION)"
    read -r -p "Ubuntu template [22.04/24.04] (${version:-24.04}): " answer
    version="${answer:-${version:-24.04}}"
    [[ "$version" == 22.04 || "$version" == 24.04 ]] || die "Choose 22.04 or 24.04"
    if [[ ! -f "$data/templates/ubuntu-$version-lvm/ubuntu-$version-lvm.qcow2" ]]; then
        read -r -p "Build missing Ubuntu $version LVM image now? Downloads an ISO and may take 30+ minutes [Y/n]: " answer
        case "$answer" in
            ''|y|Y|yes)
                if [[ -r /dev/kvm && -w /dev/kvm ]]; then
                    python3 "$SCRIPT_DIR/scripts/prepare_image.py" "$SCRIPT_DIR" "$data" "$version"
                else
                    run_as_root usermod -aG kvm "$owner"
                    run_as_root runuser -u "$owner" -- python3 "$SCRIPT_DIR/scripts/prepare_image.py" "$SCRIPT_DIR" "$data" "$version"
                fi
                ;;
            *) die "A template is required. Rerun the installer to build it." ;;
        esac
    fi
    set_env_value UBUNTU_VERSION "$version"
}

ensure_python() {
    if python3 -c 'import yaml' >/dev/null 2>&1; then return; fi
    if command -v apt-get >/dev/null; then
        run_as_root apt-get update
        run_as_root apt-get install -y python3 python3-yaml ca-certificates
    elif command -v dnf >/dev/null; then
        run_as_root dnf install -y python3 python3-pyyaml ca-certificates
    elif command -v pacman >/dev/null; then
        run_as_root pacman -S --needed --noconfirm python python-yaml ca-certificates
    elif command -v zypper >/dev/null; then
        run_as_root zypper --non-interactive install python3 python3-PyYAML ca-certificates
    elif command -v apk >/dev/null; then
        run_as_root apk add python3 py3-yaml ca-certificates
    else
        die "Install Python 3 and PyYAML on this distribution"
    fi
    python3 -c 'import yaml' || die "Python 3 with PyYAML is required"
}

ensure_local_qemu() {
    if ! command -v virsh >/dev/null || ! command -v qemu-img >/dev/null || ! command -v qemu-system-x86_64 >/dev/null || ! command -v setfacl >/dev/null; then
        if command -v apt-get >/dev/null; then
            run_as_root apt-get update
            run_as_root apt-get install -y qemu-system-x86 qemu-utils libvirt-daemon-system libvirt-clients acl
        elif command -v dnf >/dev/null; then
            run_as_root dnf install -y qemu-kvm qemu-img libvirt libvirt-client acl
        else
            die "Install QEMU/KVM, libvirt, virsh and ACL tools; automatic local-QEMU packages support Debian/Ubuntu and Fedora/RHEL"
        fi
    fi
    [[ -c /dev/kvm ]] || die "KVM is unavailable; enable hardware or nested virtualization"
    if [[ ! -S /var/run/libvirt/libvirt-sock ]]; then
        run_as_root systemctl enable --now libvirtd || run_as_root systemctl enable --now virtproxyd.socket
    fi
    # Only create/start the standard network if absent/inactive.
    if ! run_as_root virsh -c qemu:///system net-info default >/dev/null 2>&1; then
        run_as_root virsh -c qemu:///system net-define "$SCRIPT_DIR/templates/libvirt/default-network.xml"
    fi
    if ! run_as_root virsh -c qemu:///system net-list --name | grep -Fxq default; then
        run_as_root virsh -c qemu:///system net-start default
    fi
    run_as_root virsh -c qemu:///system net-autostart default
}

generate_password_hash() {
    local password="$1"

    if command -v python3 >/dev/null 2>&1; then
        ADMIN_PASSWORD="$password" python3 - <<'PY'
import hashlib
import os
import secrets

iterations = 600_000
salt = secrets.token_bytes(16)
digest = hashlib.pbkdf2_hmac(
    "sha256", os.environ["ADMIN_PASSWORD"].encode(), salt, iterations
)
print(f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}")
PY
        return
    fi

    if run_docker run --help >/dev/null 2>&1; then
        run_docker run --rm -i -e ADMIN_PASSWORD="$password" python:3.13-alpine python - <<'PY'
import hashlib
import os
import secrets

iterations = 600_000
salt = secrets.token_bytes(16)
digest = hashlib.pbkdf2_hmac(
    "sha256", os.environ["ADMIN_PASSWORD"].encode(), salt, iterations
)
print(f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}")
PY
        return
    fi

    die "Password hash generation requires python3 or Docker"
}

ensure_admin_credentials() {
    local username password confirmation password_hash
    username="$(read_env_value LOCAL_ADMIN_USERNAME)"
    password_hash="$(read_env_value LOCAL_ADMIN_PASSWORD_HASH)"

    if [[ -z "$username" ]]; then
        read -r -p "Local admin username [admin]: " username
        username="${username:-admin}"
        [[ "$username" =~ ^[A-Za-z0-9._-]{1,64}$ ]] || die "Admin username contains unsupported characters"
        set_env_value LOCAL_ADMIN_USERNAME "$username"
    fi

    if [[ "$password_hash" == pbkdf2_sha256\$*\$*\$* && "$password_hash" != *replace-with-* ]]; then
        if [[ "$password_hash" != \'*\' ]]; then
            set_env_value LOCAL_ADMIN_PASSWORD_HASH "'$password_hash'"
        fi
        return
    fi

    while :; do
        read -r -s -p "Local admin password: " password
        printf '\n'
        [[ -n "$password" ]] || { printf 'Password must not be empty.\n' >&2; continue; }
        read -r -s -p "Repeat local admin password: " confirmation
        printf '\n'
        [[ "$password" == "$confirmation" ]] && break
        printf 'Passwords do not match; please try again.\n' >&2
    done

    password_hash="$(generate_password_hash "$password")"
    unset password confirmation
    # Single quotes keep the dollar signs in the hash literal in Compose .env.
    set_env_value LOCAL_ADMIN_PASSWORD_HASH "'$password_hash'"
}

prepare_runtime_config() {
    local backend="$1"

    mkdir -p "$RUNTIME_DIR"
    chmod 755 "$RUNTIME_DIR"

    if [[ "$backend" == "vcsim" ]]; then
        python3 "$SCRIPT_DIR/scripts/configure_runtime.py" "$SCRIPT_DIR" vcsim
        set_env_value COMPOSE_FILE "./docker-compose.yml"
        return
    fi

    python3 "$SCRIPT_DIR/scripts/configure_runtime.py" "$SCRIPT_DIR" local_qemu \
        --data "$(read_env_value KVM_READONLY_PATH)" \
        --storage "$(read_env_value KVM_STORAGE_PATH)" \
        --cloud-init "$(read_env_value CLOUD_INIT_TEMPLATE)" \
        --socket "$(read_env_value LIBVIRT_SOCKET_PATH)"
    set_env_value COMPOSE_FILE "./docker-compose.yml:./.runtime/docker-compose.local-qemu.yml"
}

main() {
    local no_start=false
    local argument backend

    for argument in "$@"; do
        case "$argument" in
            --no-start) no_start=true ;;
            -h|--help) usage; exit 0 ;;
            *) die "Unknown option: $argument" ;;
        esac
    done

    cd -- "$SCRIPT_DIR"
    umask 077

    ensure_docker
    ensure_python

    if [[ ! -f "$ENV_FILE" ]]; then
        touch "$ENV_FILE"
        chmod 600 "$ENV_FILE"
    fi

    log "Preparing administrator credentials"
    ensure_admin_credentials

    log "Selecting provisioning backend"
    backend="$(choose_backend)"
    if [[ "$backend" == "local_qemu" ]]; then
        configure_local_qemu_paths
    fi

    set_env_value PORTAL_CONFIG_PATH "./.runtime/config.yml"
    prepare_runtime_config "$backend"

    log "Validating Docker Compose configuration"
    run_compose config >/dev/null

    if [[ "$no_start" == true ]]; then
        printf '\nConfiguration ready. Start the portal with: docker compose up --build -d\n'
        exit 0
    fi

    log "Building and starting the portal"
    run_compose up --build -d
    # Atomic config replacement changes the inode behind a file bind mount.
    run_compose up -d --no-deps --force-recreate portal

    cat <<'EOF'

VM Foundry is running.

Open: http://localhost:8080
Admin login: http://localhost:8080/admin-login.html
View logs: docker compose logs -f portal
Stop:      docker compose down
EOF
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
