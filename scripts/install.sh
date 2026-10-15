#!/usr/bin/env bash
# joule:code v2 – installation of the measurement machine (Ubuntu 24.04/26.04, Intel CPU, NVIDIA GPU).
#
# Run as a normal user (with sudo rights) from the repository directory:
#
#   ./scripts/install.sh            # everything
#   ./scripts/install.sh --help     # options
#
# The script can be run repeatedly: steps already done are detected and
# skipped. After installing the NVIDIA driver a reboot is required –
# afterwards simply run the script again.
#
# What it does:
#   1. Pre-flight check (Ubuntu >= 24.04, Intel, no VM/WSL, RAPL)
#   2. System packages
#   3. metrion < 0.2: BCC >= 0.35 (Ubuntu package or own build); metrion >= 0.2: build packages
#   4. Docker Engine (cgroup v2)
#   5. NVIDIA driver + container toolkit (for Ollama)
#   6. Rust + uv
#   7. METRION to /opt/metrion, its own venv with Python 3.12 (uv):
#        --metrion-source PATH  -> metrion >= 0.2 from a wheel, zip or source directory
#        otherwise              -> GitHub version (< 0.2) + bcc link
#   8. on-demand metrion service + sudoers rule (>= 0.2: configuration + calibration)
#   9. joule:code (venv in the repository)
#  10. Ollama (docker compose) + model
#  11. optional: noise reduction at boot (governor, turbo)
#  12. joulecode doctor

set -euo pipefail

# ---------------------------------------------------------------------------
# Settings (can be changed via option or environment variable)
# ---------------------------------------------------------------------------
METRION_REPO="${METRION_REPO:-https://github.com/ds-lab/Metrion.git}"
METRION_REF="${METRION_REF:-}"                 # empty = default branch
METRION_DIR="${METRION_DIR:-/opt/metrion}"
METRION_SOURCE="${METRION_SOURCE:-}"           # wheel/zip/directory of metrion >= 0.2
METRION_WORKDIR="${METRION_WORKDIR:-/var/lib/metrion}"
BCC_VERSION="${BCC_VERSION:-v0.35.0}"
BCC_MIN="${BCC_MIN:-0.35}"
NVIDIA_DRIVER="${NVIDIA_DRIVER:-nvidia-driver-580}"   # 580 = last series with Pascal (GTX 10xx)
OLLAMA_MODEL="${JOULECODE_OLLAMA_MODEL:-qwen2.5-coder:14b}"
SERVICE="joulecode-metrion.service"

SKIP_NVIDIA=0
SKIP_OLLAMA=0
SKIP_BCC=0
TUNE=0
ASSUME_YES=0
SKIP_CALIBRATE=0
RECALIBRATE=0
KEEP_RAW=0

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="${REPO_DIR}/install.log"
REBOOT_NEEDED=0

usage() {
    cat <<EOF
Usage: $0 [options]

  --metrion-source PATH install METRION >= 0.2 from a wheel (.whl), zip or source directory
                        (e.g. a version not yet released)
  --metrion-ref REF     GitHub version: branch/tag/commit (default: default branch)
  --metrion-dir DIR     installation directory of METRION (default: ${METRION_DIR})
  --skip-calibrate      do not calibrate metrion >= 0.2 (do it by hand later)
  --recalibrate         calibrate metrion >= 0.2 again
  --metrion-keep-raw    metrion >= 0.2: store raw data in addition to the live result
  --skip-nvidia         do not touch the NVIDIA driver and container toolkit
  --skip-ollama         do not start Ollama
  --skip-bcc            do not build BCC (if a suitable version is already installed)
  --tune                governor 'performance' and turbo off at every boot
  -y, --yes             no questions
  -h, --help            this help

Environment variables: METRION_SOURCE, METRION_REPO, METRION_REF, METRION_DIR, BCC_VERSION,
NVIDIA_DRIVER, JOULECODE_OLLAMA_MODEL
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --metrion-source) METRION_SOURCE="$2"; shift 2 ;;
        --metrion-ref) METRION_REF="$2"; shift 2 ;;
        --skip-calibrate) SKIP_CALIBRATE=1; shift ;;
        --recalibrate) RECALIBRATE=1; shift ;;
        --metrion-keep-raw) KEEP_RAW=1; shift ;;
        --metrion-dir) METRION_DIR="$2"; shift 2 ;;
        --skip-nvidia) SKIP_NVIDIA=1; shift ;;
        --skip-ollama) SKIP_OLLAMA=1; shift ;;
        --skip-bcc) SKIP_BCC=1; shift ;;
        --tune) TUNE=1; shift ;;
        -y|--yes) ASSUME_YES=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
    esac
done

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
if [[ -t 1 ]]; then B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; N=$'\e[0m'; else B='' G='' Y='' R='' N=''; fi
CURRENT_STEP="Start"
step() { CURRENT_STEP="$1"; echo; echo "${B}==> $1${N}"; }
ok()   { echo "  ${G}✔${N} $*"; }
warn() { echo "  ${Y}!${N} $*"; }
die()  { echo "  ${R}✘ $*${N}" >&2; exit 1; }
trap 'echo "${R}Aborted in step: ${CURRENT_STEP} (details: ${LOG})${N}" >&2' ERR

confirm() {
    [[ $ASSUME_YES -eq 1 ]] && return 0
    read -r -p "  $1 [y/N] " answer
    [[ "$answer" =~ ^[jJyY]$ ]]
}

run() { echo "    \$ $*" >>"$LOG"; "$@" >>"$LOG" 2>&1; }
apt_install() { run sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@"; }

version_ge() { [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" == "$2" ]]; }

: >"$LOG"
echo "${B}joule:code installation${N} – log: ${LOG}"

# Which metrion generation? >= 0.2 (its own eBPF via libbpf, command line with
# init/calibrate/monitor/analyze) or the older GitHub version (BCC, DuckDB).
METRION_BIN="${METRION_DIR}/.venv/bin/metrion"
metrion_v2_installed() { "$METRION_BIN" --help 2>/dev/null | grep -q calibrate; }
if [[ -n "$METRION_SOURCE" ]]; then
    [[ -e "$METRION_SOURCE" ]] || die "--metrion-source: ${METRION_SOURCE} not found."
    METRION_SOURCE="$(realpath "$METRION_SOURCE")"
    METRION_GEN=2
elif metrion_v2_installed; then
    METRION_GEN=2   # already installed – stays as it is
else
    METRION_GEN=1
fi

# ---------------------------------------------------------------------------
step "1/12 Pre-flight check"
# ---------------------------------------------------------------------------
[[ $EUID -ne 0 ]] || die "Please run as a normal user (not with sudo); the script asks for sudo itself."
sudo -v || die "sudo rights required."
# keep the sudo timestamp fresh during the long build steps
( while true; do sudo -n true; sleep 50; kill -0 "$$" 2>/dev/null || exit; done ) 2>/dev/null &

. /etc/os-release
[[ "${ID}" == "ubuntu" ]] || die "Only Ubuntu is supported (found: ${ID})."
if ! version_ge "${VERSION_ID}" "24.04"; then
    warn "Tested with Ubuntu 24.04 and 26.04, found ${VERSION_ID}."
    confirm "Continue anyway?" || exit 1
fi
command -v python3 >/dev/null || die "python3 is missing."
ok "Ubuntu ${VERSION_ID} (${VERSION_CODENAME}), system Python $(python3 -c 'import platform; print(platform.python_version())')"
# metrion needs Python 3.12; uv fetches it independently of the system Python (step 7).

if grep -qi microsoft /proc/version; then die "WSL detected – no RAPL counters. Please use Ubuntu directly on the hardware."; fi
VIRT="$(systemd-detect-virt 2>/dev/null || true)"
if [[ -n "$VIRT" && "$VIRT" != "none" ]]; then
    warn "Virtualisation detected (${VIRT}) – RAPL/eBPF measurements are usually not possible there."
    confirm "Continue anyway?" || exit 1
fi
if grep -q GenuineIntel /proc/cpuinfo; then ok "Intel CPU"; else warn "No Intel CPU – metrion measures CPU/DRAM only on Intel."; fi

HAS_DRAM=0
if [[ -d /sys/class/powercap/intel-rapl:0 ]]; then
    for n in /sys/class/powercap/intel-rapl:0:*/name; do [[ -f "$n" && "$(cat "$n")" == "dram" ]] && HAS_DRAM=1; done
    ok "RAPL available (DRAM domain: $([[ $HAS_DRAM -eq 1 ]] && echo yes || echo no))"
else
    warn "No RAPL counters under /sys/class/powercap – energy measurement not possible (check BIOS/kernel)."
fi
if [[ $METRION_GEN -eq 2 ]]; then
    [[ -f /sys/kernel/btf/vmlinux ]] && ok "kernel with BTF (for metrion >= 0.2)" \
        || die "/sys/kernel/btf/vmlinux is missing – metrion >= 0.2 needs a kernel with BTF."
fi

# ---------------------------------------------------------------------------
step "2/12 System packages"
# ---------------------------------------------------------------------------
run sudo apt-get update
apt_install ca-certificates curl gnupg git jq unzip build-essential pkg-config python3-venv python3-pip pciutils
ok "base packages installed"
# Kernel headers and perf tools separately: for a freshly updated
# kernel they are sometimes only available after a reboot.
if apt_install "linux-headers-$(uname -r)" linux-tools-common "linux-tools-$(uname -r)"; then
    ok "kernel headers and linux-tools for $(uname -r)"
else
    warn "kernel headers/linux-tools for $(uname -r) not installable – try again after a reboot."
fi

# ---------------------------------------------------------------------------
step "3/12 eBPF prerequisites for metrion"
# ---------------------------------------------------------------------------
if [[ $METRION_GEN -eq 2 ]]; then
    # metrion >= 0.2 brings its own eBPF programs (libbpf); BCC is not needed.
    # Only for building from source: clang (BPF programs), libelf, zlib.
    if [[ -n "$METRION_SOURCE" && "$METRION_SOURCE" != *.whl ]]; then
        apt_install clang libelf-dev zlib1g-dev
        ok "clang, libelf-dev, zlib1g-dev (build from source)"
    else
        ok "nothing needed (metrion >= 0.2 brings its own eBPF)"
    fi
else
# The bcc Python module is pure Python (loads libbcc.so.0 via ctypes). It is therefore
# not bound to a particular Python version but linked into metrion's own
# Python 3.12 environment in step 7.
pkg_version() { dpkg-query -W -f='${Version}' "$1" 2>/dev/null | sed 's/^[0-9]*://; s/[+~-].*//' || true; }
candidate_version() { LC_ALL=C apt-cache policy "$1" 2>/dev/null | awk '/Candidate:/ {print $2}' | sed 's/^[0-9]*://; s/[+~-].*//; s/(none)//'; }
bcc_pkg_dir() {
    # directory of the bcc Python package (from the Ubuntu package or the own build)
    local f
    f="$(dpkg -L python3-bpfcc 2>/dev/null | grep -m1 '/bcc/__init__.py$' || true)"
    [[ -z "$f" ]] && f="$(python3 -c 'import bcc; print(bcc.__file__)' 2>/dev/null || true)"
    [[ -n "$f" ]] && dirname "$f"
    return 0
}
have_libbcc() { ldconfig -p | grep -q 'libbcc.so.0'; }

INSTALLED_BCC="$(pkg_version python3-bpfcc)"
CANDIDATE_BCC="$(candidate_version python3-bpfcc)"
if [[ -n "$INSTALLED_BCC" ]] && version_ge "$INSTALLED_BCC" "$BCC_MIN"; then
    apt_install bpfcc-tools python3-bpfcc libbpfcc libbpfcc-dev
    ok "BCC ${INSTALLED_BCC} from Ubuntu packages"
elif [[ -n "$CANDIDATE_BCC" ]] && version_ge "$CANDIDATE_BCC" "$BCC_MIN"; then
    apt_install bpfcc-tools python3-bpfcc libbpfcc libbpfcc-dev
    ok "BCC ${CANDIDATE_BCC} installed from Ubuntu packages"
elif [[ -n "$(bcc_pkg_dir)" ]] && have_libbcc; then
    ok "BCC (own build) already present: $(bcc_pkg_dir)"
elif [[ $SKIP_BCC -eq 1 ]]; then
    warn "BCC skipped – Ubuntu only offers ${CANDIDATE_BCC:-no version}, metrion needs >= ${BCC_MIN}."
else
    echo "  Ubuntu only offers BCC ${CANDIDATE_BCC:-?} – building ${BCC_VERSION} from source (10–20 minutes) …"
    # use the newest available LLVM version between 15 and 21
    LLVM=""
    for v in 21 20 19 18 17 16 15; do
        if apt-cache show "llvm-${v}-dev" >/dev/null 2>&1; then LLVM=$v; break; fi
    done
    [[ -n "$LLVM" ]] || die "No suitable llvm-*-dev package found."
    apt_install zip bison cmake flex libedit-dev "libllvm${LLVM}" "llvm-${LLVM}-dev" "libclang-${LLVM}-dev" \
        zlib1g-dev libelf-dev libfl-dev python3-setuptools liblzma-dev libdebuginfod-dev "libpolly-${LLVM}-dev"
    BUILD="$(mktemp -d)"
    run git clone --depth 1 --branch "$BCC_VERSION" https://github.com/iovisor/bcc.git "$BUILD/bcc"
    mkdir -p "$BUILD/bcc/build"
    # build the Python bindings with the system Python (has setuptools); they are used
    # independently of it later (see above).
    ( cd "$BUILD/bcc/build" \
        && run cmake .. -DPYTHON_CMD=python3 -DCMAKE_INSTALL_PREFIX=/usr -DLLVM_DIR="/usr/lib/llvm-${LLVM}/lib/cmake/llvm" \
        && run make -j"$(nproc)" \
        && run sudo make install )
    run sudo ldconfig
    rm -rf "$BUILD"
    [[ -n "$(bcc_pkg_dir)" ]] || die "BCC Python package not found after the build (see ${LOG})."
    ok "BCC ${BCC_VERSION} built and installed"
fi
BCC_DIR="$(bcc_pkg_dir)"
if [[ -n "$BCC_DIR" ]]; then ok "bcc Python package: ${BCC_DIR}"; else warn "bcc Python package not found – metrion will not be able to use eBPF."; fi
fi

# ---------------------------------------------------------------------------
step "4/12 Docker"
# ---------------------------------------------------------------------------
if command -v docker >/dev/null && sudo docker info >/dev/null 2>&1; then
    ok "Docker present ($(sudo docker version --format '{{.Server.Version}}'))"
else
    run sudo install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
    sudo chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
        | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
    run sudo apt-get update
    apt_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    run sudo systemctl enable --now docker
    ok "Docker installed"
fi
if ! id -nG "$USER" | grep -qw docker; then
    run sudo usermod -aG docker "$USER"
    warn "User ${USER} added to the group docker – takes effect only after logging in again."
fi
CGV="$(sudo docker info --format '{{.CgroupVersion}}' 2>/dev/null || echo '?')"
[[ "$CGV" == "2" ]] && ok "cgroup v2" || warn "cgroup version ${CGV} – expected 2."

# ---------------------------------------------------------------------------
step "5/12 NVIDIA driver + container toolkit"
# ---------------------------------------------------------------------------
GPU_READY=0
if [[ $SKIP_NVIDIA -eq 1 ]]; then
    warn "skipped (--skip-nvidia)"
elif ! lspci | grep -qi nvidia; then
    warn "No NVIDIA GPU found – Ollama then runs on the CPU."
else
    if command -v nvidia-smi >/dev/null && nvidia-smi >/dev/null 2>&1; then
        ok "driver active: $(nvidia-smi --query-gpu=name,driver_version --format=csv,noheader | head -1)"
    else
        if apt-cache show "$NVIDIA_DRIVER" >/dev/null 2>&1; then
            apt_install "$NVIDIA_DRIVER"
        else
            warn "${NVIDIA_DRIVER} not available – using ubuntu-drivers."
            apt_install ubuntu-drivers-common
            run sudo ubuntu-drivers install
        fi
        REBOOT_NEEDED=1
        warn "driver installed – reboot required."
    fi
    if ! dpkg -s nvidia-container-toolkit >/dev/null 2>&1; then
        curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
            | sudo gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
        curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
            | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
            | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
        run sudo apt-get update
        apt_install nvidia-container-toolkit
        run sudo nvidia-ctk runtime configure --runtime=docker
        run sudo systemctl restart docker
        ok "NVIDIA Container Toolkit installed"
    else
        ok "NVIDIA Container Toolkit present"
    fi
    [[ $REBOOT_NEEDED -eq 0 ]] && GPU_READY=1
fi

# ---------------------------------------------------------------------------
step "6/12 Rust + uv"
# ---------------------------------------------------------------------------
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
if ! command -v cargo >/dev/null; then
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | run sh -s -- -y --profile minimal
fi
ok "Rust $(cargo --version | cut -d' ' -f2)"
if ! command -v uv >/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | run sh
fi
ok "uv $(uv --version | cut -d' ' -f2)"

# ---------------------------------------------------------------------------
step "7/12 METRION"
# ---------------------------------------------------------------------------
metrion_python_ok() { [[ "$("${METRION_DIR}/.venv/bin/python" -c 'import sys; print(sys.version_info[:2] == (3, 12))' 2>/dev/null)" == "True" ]]; }
if [[ $METRION_GEN -eq 2 && -z "$METRION_SOURCE" ]]; then
    ok "metrion $("${METRION_DIR}/.venv/bin/python" -c 'import importlib.metadata as m; print(m.version("metrion"))') already installed (to replace it: --metrion-source PATH)"
elif [[ $METRION_GEN -eq 2 ]]; then
    echo "  Source: ${METRION_SOURCE}"
    if [[ -d "${METRION_DIR}/.git" ]]; then
        # Move the GitHub version aside instead of deleting it – renaming it brings it back.
        OLD="${METRION_DIR}-github-$(date +%Y%m%d-%H%M%S)"
        run sudo mv "$METRION_DIR" "$OLD"
        warn "Previous GitHub version of metrion moved to ${OLD}"
    fi
    run sudo mkdir -p "$METRION_DIR"
    run sudo chown "$USER:$USER" "$METRION_DIR"
    # Fresh venv: no remains of the GitHub version (bcc link, old dependencies).
    rm -rf "${METRION_DIR}/.venv"
    run uv python install 3.12
    run uv venv --python 3.12 "${METRION_DIR}/.venv"
    if [[ "$METRION_SOURCE" == *.whl ]]; then
        run uv pip install --python "${METRION_DIR}/.venv/bin/python" "$METRION_SOURCE" \
            || die "The wheel could not be installed (see ${LOG})."
    else
        # copy the sources to ${METRION_DIR}/src (without build leftovers) and build there.
        SRC="${METRION_DIR}/src"
        rm -rf "$SRC"; mkdir -p "$SRC"
        if [[ "$METRION_SOURCE" == *.zip ]]; then
            UNZ="$(mktemp -d)"
            run unzip -q "$METRION_SOURCE" -d "$UNZ"
            # the shallowest pyproject.toml is the package's
            TOP="$(dirname "$(find "$UNZ" -maxdepth 3 -name pyproject.toml -print | awk -F/ '{print NF "\t" $0}' | sort -n | head -1 | cut -f2-)")"
            [[ -f "$TOP/pyproject.toml" ]] || die "No pyproject.toml found in the zip."
            tar -C "$TOP" --exclude=./target --exclude=./.venv -cf - . | tar -C "$SRC" -xf -
            rm -rf "$UNZ"
        else
            [[ -f "$METRION_SOURCE/pyproject.toml" ]] || die "${METRION_SOURCE} contains no pyproject.toml."
            tar -C "$METRION_SOURCE" --exclude=./target --exclude=./.venv -cf - . | tar -C "$SRC" -xf -
        fi
        grep -q '^name = "metrion"' "$SRC/pyproject.toml" || die "${METRION_SOURCE} is not a metrion source package."
        echo "  Building METRION $(sed -n 's/^version = "\(.*\)"/\1/p' "$SRC/pyproject.toml" | head -1) (Rust + eBPF, 10–20 minutes) …"
        build_v2() { run uv pip install --python "${METRION_DIR}/.venv/bin/python" --reinstall "$SRC"; }
        if ! build_v2; then
            warn "Build failed – trying a known fix (ethnum update for new Rust versions) …"
            ( cd "$SRC" && run cargo update -p ethnum ) || true
            build_v2 || die "METRION could not be built (see ${LOG})."
        fi
    fi
    metrion_v2_installed || die "${METRION_BIN} does not know 'calibrate' – not a metrion version >= 0.2? (see ${LOG})"
    ok "metrion $("${METRION_DIR}/.venv/bin/python" -c 'import importlib.metadata as m; print(m.version("metrion"))') installed: ${METRION_BIN}"
else
if [[ ! -d "${METRION_DIR}/.git" ]]; then
    run sudo mkdir -p "$METRION_DIR"
    run sudo chown "$USER:$USER" "$METRION_DIR"
    run git clone "$METRION_REPO" "$METRION_DIR"
else
    # Reset a Cargo.lock possibly modified by the script earlier (see below).
    run git -C "$METRION_DIR" checkout -- Cargo.lock || true
    run git -C "$METRION_DIR" fetch --all --tags
fi
if [[ -n "$METRION_REF" ]]; then
    run git -C "$METRION_DIR" checkout "$METRION_REF"
else
    run git -C "$METRION_DIR" pull --ff-only || warn "git pull not possible (local changes?) – staying on the current state."
fi
ok "state $(git -C "$METRION_DIR" log -1 --format='%h %cd' --date=short)"

# metrion requires Python 3.12 (<3.13). uv fetches it independently of the system Python.
if [[ "$("${METRION_DIR}/.venv/bin/python" -c 'import sys; print(sys.version_info[:2] == (3, 12))' 2>/dev/null)" != "True" ]]; then
    rm -rf "${METRION_DIR}/.venv"
    run uv python install 3.12
    run uv venv --python 3.12 "${METRION_DIR}/.venv"
fi
echo "  Building METRION (Rust extension, a few minutes) …"
build_metrion() { ( cd "$METRION_DIR" && run uv pip install --python .venv/bin/python -e . ); }
if ! build_metrion; then
    # Known problem: the version of ethnum pinned in Cargo.lock
    # (1.5.2) does not compile with newer Rust versions (E0512); 1.5.3 fixes that.
    warn "Build failed – trying a known fix (ethnum update for new Rust versions) …"
    ( cd "$METRION_DIR" && run cargo update -p ethnum ) || true
    build_metrion || die "METRION could not be built (see ${LOG})."
    ok "built after the ethnum update (local change to ${METRION_DIR}/Cargo.lock)"
fi
"${METRION_DIR}/.venv/bin/python" -c "import metrion.energy_attribution.energy_attribution_cli" >>"$LOG" 2>&1 \
    || die "METRION cannot be imported (see ${LOG})."
# link the system's bcc Python package into the venv (only this package, not
# all system packages – otherwise there would be version conflicts with metrion's dependencies).
if [[ -n "${BCC_DIR:-}" ]]; then
    PURELIB="$("${METRION_DIR}/.venv/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
    mkdir -p "${METRION_DIR}/.venv/bcc-site"
    ln -sfn "$BCC_DIR" "${METRION_DIR}/.venv/bcc-site/bcc"
    echo "${METRION_DIR}/.venv/bcc-site" >"${PURELIB}/joulecode-bcc.pth"
fi
if BCC_IN_VENV="$("${METRION_DIR}/.venv/bin/python" -c 'from bcc.version import __version__ as v; print(v)' 2>>"$LOG")"; then
    ok "METRION installed, BCC ${BCC_IN_VENV} visible in metrion's Python"
else
    warn "METRION installed, but 'import bcc' fails in metrion's Python – eBPF measurement will fail (see ${LOG})."
fi
fi

# ---------------------------------------------------------------------------
step "8/12 metrion service + sudoers rule"
# ---------------------------------------------------------------------------
if [[ $METRION_GEN -eq 2 ]]; then
    run sudo mkdir -p "$METRION_WORKDIR"
    # Generate the configuration every time from the default configuration of the installed
    # version – that way it also fits after a metrion update. calibration.json stays.
    run sudo rm -f "${METRION_WORKDIR}/metrion.yaml"
    ( cd "$METRION_WORKDIR" && run sudo "$METRION_BIN" init ) || die "metrion init failed (see ${LOG})."
    CFG_ARGS=()
    [[ $HAS_DRAM -eq 0 ]] && CFG_ARGS+=(--no-dram)
    [[ $KEEP_RAW -eq 1 ]] && CFG_ARGS+=(--keep-raw)
    run sudo "${METRION_DIR}/.venv/bin/python" -I "${REPO_DIR}/deploy/metrion/configure_v2.py" \
        "${METRION_WORKDIR}/metrion.yaml" "${CFG_ARGS[@]}" || die "metrion.yaml could not be adjusted (see ${LOG})."
    [[ $HAS_DRAM -eq 0 ]] && warn "No RAPL DRAM domain – DRAM measurement switched off."
    ok "${METRION_WORKDIR}/metrion.yaml (live attribution, GPU/network off)"

    sed -e "s#@METRION_BIN@#${METRION_BIN}#g" -e "s#@METRION_WORKDIR@#${METRION_WORKDIR}#g" \
        "${REPO_DIR}/deploy/metrion/joulecode-metrion-v2.service" | sudo tee "/etc/systemd/system/${SERVICE}" >/dev/null
    run sudo systemctl daemon-reload
    ok "service ${SERVICE} set up (starts only on demand by joule:code)"

    # Measure the idle power: metrion freezes everything else for ~2 minutes to do so.
    [[ $RECALIBRATE -eq 1 ]] && run sudo rm -f "${METRION_WORKDIR}/calibration.json"
    if [[ -f "${METRION_WORKDIR}/calibration.json" ]]; then
        ok "calibration present (to redo it: --recalibrate)"
    elif [[ $SKIP_CALIBRATE -eq 1 ]]; then
        warn "Not calibrated – before the first measurement: cd ${METRION_WORKDIR} && sudo ${METRION_BIN} calibrate"
    else
        echo "  metrion now measures the idle power. For this, the desktop, this terminal and"
        echo "  running containers are ${B}frozen${N} for about 2 minutes – screen and mouse do not respond"
        echo "  during that time. This is intended; afterwards it continues by itself."
        if confirm "Calibrate now?"; then
            if ( cd "$METRION_WORKDIR" && run sudo "$METRION_BIN" calibrate --yes ); then
                IDLE="$("${METRION_DIR}/.venv/bin/python" -I -c 'import json, sys
d = json.load(open(sys.argv[1]))
print(", ".join(f"{c.upper()} {sum(w for w in d[c].values() if w):.1f} W" for c in ("cpu", "dram") if d.get(c)))' \
                    "${METRION_WORKDIR}/calibration.json" 2>>"$LOG" || true)"
                ok "calibrated – idle: ${IDLE:-see ${METRION_WORKDIR}/calibration.json}"
            else
                warn "calibration failed (see ${LOG}) – later: cd ${METRION_WORKDIR} && sudo ${METRION_BIN} calibrate"
            fi
        else
            warn "Not calibrated – before the first measurement: cd ${METRION_WORKDIR} && sudo ${METRION_BIN} calibrate"
        fi
    fi
else
run sudo mkdir -p /etc/joulecode /var/lib/metrion
TMPCFG="$(mktemp)"
cp "${REPO_DIR}/deploy/metrion/config.github.yaml" "$TMPCFG"
if [[ $HAS_DRAM -eq 0 ]]; then
    # set the dram block to exclude (first mode line after "dram:")
    sed -i '/^    dram:/,/^    [a-z]*:/ s/^      mode: include/      mode: exclude/' "$TMPCFG"
    warn "No RAPL DRAM domain – DRAM measurement switched off in /etc/joulecode/metrion.yaml."
fi
sudo install -m 0644 "$TMPCFG" /etc/joulecode/metrion.yaml
rm -f "$TMPCFG"
sed "s#@METRION_DIR@#${METRION_DIR}#g" "${REPO_DIR}/deploy/metrion/joulecode-metrion.service" \
    | sudo tee "/etc/systemd/system/${SERVICE}" >/dev/null
run sudo systemctl daemon-reload
ok "service ${SERVICE} set up (starts only on demand by joule:code)"
fi

SUDOERS="/etc/sudoers.d/joulecode-metrion"
SYSTEMCTL="$(command -v systemctl)"
TMPSUDO="$(mktemp)"
cat >"$TMPSUDO" <<EOF
# joule:code may only start/stop/query the metrion measurement service.
${USER} ALL=(root) NOPASSWD: ${SYSTEMCTL} start ${SERVICE}, ${SYSTEMCTL} stop ${SERVICE}, ${SYSTEMCTL} is-active ${SERVICE}
EOF
sudo visudo -cf "$TMPSUDO" >>"$LOG" 2>&1 || die "sudoers rule invalid."
sudo install -m 0440 "$TMPSUDO" "$SUDOERS"
rm -f "$TMPSUDO"
ok "sudoers rule ${SUDOERS}"

# ---------------------------------------------------------------------------
step "9/12 joule:code"
# ---------------------------------------------------------------------------
if [[ ! -x "${REPO_DIR}/.venv/bin/python" ]]; then
    run python3 -m venv "${REPO_DIR}/.venv"
fi
run "${REPO_DIR}/.venv/bin/pip" install --upgrade pip
run "${REPO_DIR}/.venv/bin/pip" install -e "${REPO_DIR}[metrion]"
ENVFILE="${REPO_DIR}/.joulecode.env"
# Start from the commented template on the first run; later runs only update the keys this
# script manages, so your own settings (server, client, backend …) are kept.
[[ -f "$ENVFILE" ]] || cp "${REPO_DIR}/.joulecode.env.example" "$ENVFILE"
set_env() {  # set_env KEY VALUE – replace an existing (also commented) line or append
    if grep -qE "^#? *export $1=" "$ENVFILE"; then
        sed -i -E "s|^#? *export $1=.*|export $1=\"$2\"|" "$ENVFILE"
    else
        echo "export $1=\"$2\"" >>"$ENVFILE"
    fi
}
set_env JOULECODE_METRION_BIN "${METRION_BIN}"
set_env JOULECODE_METRION_DIR "${METRION_WORKDIR}"
set_env JOULECODE_METRION_SERVICE "${SERVICE}"
set_env JOULECODE_METRION_PYTHON "${METRION_DIR}/.venv/bin/python"
set_env JOULECODE_METRION_DB "/var/lib/metrion/monitor.db"
set_env JOULECODE_OLLAMA_HOST "http://localhost:11434"
set_env JOULECODE_OLLAMA_MODEL "${OLLAMA_MODEL}"
ACTIVATE="${REPO_DIR}/.venv/bin/activate"
grep -q joulecode.env "$ACTIVATE" || echo "[ -f \"${ENVFILE}\" ] && . \"${ENVFILE}\"" >>"$ACTIVATE"
ok "joule:code installed (${REPO_DIR}/.venv)"

# ---------------------------------------------------------------------------
step "10/12 Ollama"
# ---------------------------------------------------------------------------
if [[ $SKIP_OLLAMA -eq 1 ]]; then
    warn "skipped (--skip-ollama)"
elif [[ $REBOOT_NEEDED -eq 1 ]]; then
    warn "only after the reboot (then run the script again)"
else
    if [[ $GPU_READY -eq 0 ]]; then
        warn "No usable GPU – the Ollama container is not started without a GPU reservation."
        warn "Without a GPU: remove the 'deploy' section in docker-compose.yml and run 'docker compose up -d'."
    else
        ( cd "$REPO_DIR" && JOULECODE_OLLAMA_MODEL="$OLLAMA_MODEL" run sudo -E docker compose up -d ollama )
        echo "  Pulling model ${OLLAMA_MODEL} (several GB) …"
        for _ in $(seq 1 60); do sudo docker exec joulecode-ollama ollama list >/dev/null 2>&1 && break; sleep 2; done
        run sudo docker exec joulecode-ollama ollama pull "$OLLAMA_MODEL"
        ok "Ollama running with ${OLLAMA_MODEL}"
    fi
fi

# ---------------------------------------------------------------------------
step "11/12 Noise reduction"
# ---------------------------------------------------------------------------
if [[ $TUNE -eq 1 ]]; then
    sudo tee /etc/systemd/system/joulecode-tuning.service >/dev/null <<'EOF'
[Unit]
Description=joule:code: reproducible measurement conditions (governor performance, turbo off)

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo performance > "$g"; done; [ -f /sys/devices/system/cpu/intel_pstate/no_turbo ] && echo 1 > /sys/devices/system/cpu/intel_pstate/no_turbo || true'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
    run sudo systemctl daemon-reload
    run sudo systemctl enable --now joulecode-tuning.service
    ok "governor performance + turbo off (also after reboots)"
else
    echo "  skipped (with --tune: governor performance and turbo off at every boot)"
fi

# ---------------------------------------------------------------------------
step "12/12 Check"
# ---------------------------------------------------------------------------
if [[ $REBOOT_NEEDED -eq 1 ]]; then
    warn "Please reboot now (sudo reboot) and then run the script again."
    exit 0
fi
# The docker group only takes effect after logging in again. For the doctor run here,
# start with the group docker via sudo if necessary (sg is missing on 26.04).
DOCTOR=(bash -c "set -a; . '${ENVFILE}'; exec '${REPO_DIR}/.venv/bin/joulecode' doctor")
if id -nG | grep -qw docker; then
    "${DOCTOR[@]}" || true
else
    sudo -u "$USER" -g docker "${DOCTOR[@]}" || warn "Please run joulecode doctor after logging in again."
fi
echo
echo "${G}${B}Done.${N} Next steps:"
echo "  log in again (for the docker group), then:"
echo "  cd ${REPO_DIR} && source .venv/bin/activate"
echo "  joulecode measure examples/python-wordstats --aa      # get to know the measurement noise"
echo "  joulecode optimize examples/python-wordstats"
