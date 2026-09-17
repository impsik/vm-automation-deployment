#!/usr/bin/env bash

set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly ENV_FILE="$SCRIPT_DIR/.env"
readonly RUNTIME_DIR="$SCRIPT_DIR/.runtime"
readonly RUNTIME_CONFIG="$RUNTIME_DIR/config.yml"

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
  - prepares a self-contained VMware vcsim demo configuration;
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
    mkdir -p "$RUNTIME_DIR"
    cp -- "$SCRIPT_DIR/portal/config.yml" "$RUNTIME_CONFIG"

    # The self-contained installer uses vcsim. Local QEMU remains available for
    # advanced deployments by using the original portal/config.yml and paths.
    sed -i 's/^  backend: local_qemu/  backend: vcsim/' "$RUNTIME_CONFIG"
}

main() {
    local no_start=false
    local argument

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

    if [[ ! -f "$ENV_FILE" ]]; then
        touch "$ENV_FILE"
        chmod 600 "$ENV_FILE"
    fi

    log "Preparing administrator credentials"
    ensure_admin_credentials

    set_env_value PORTAL_CONFIG_PATH "./.runtime/config.yml"
    prepare_runtime_config

    log "Validating Docker Compose configuration"
    run_compose config >/dev/null

    if [[ "$no_start" == true ]]; then
        printf '\nConfiguration ready. Start the portal with: docker compose up --build -d\n'
        exit 0
    fi

    log "Building and starting the portal"
    run_compose up --build -d

    cat <<'EOF'

VM Foundry is running.

Open: http://localhost:8080
Admin login: http://localhost:8080/admin-login.html
View logs: docker compose logs -f portal
Stop:      docker compose down
EOF
}

main "$@"
