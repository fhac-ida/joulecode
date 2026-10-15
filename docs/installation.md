# Installation

This guide sets up a complete **measuring host** step by step: a Linux machine on which
joule:code measures the energy of your code with [METRION](https://github.com/ds-lab/Metrion)
and runs the language model for the optimiser on an NVIDIA GPU. This is the recommended set-up;
everything else is an [alternative](#alternatives) with fewer capabilities.

> **Security first.** joule:code builds and runs the code it analyses (tests, benchmark and
> LLM-generated candidates). Only use it on repositories you trust, and preferably on a
> dedicated machine — see [Security notice](../README.md#security-notice).

**Contents**

1. [Before you start: requirements](#1-before-you-start-requirements)
2. [Choose a language model](#2-choose-a-language-model)
3. [Get joule:code](#3-get-joulecode)
4. [Run the install script](#4-run-the-install-script)
5. [Reboot and run the script again](#5-reboot-and-run-the-script-again)
6. [Check the installation](#6-check-the-installation)
7. [First measurement](#7-first-measurement)
8. [Optional: reduce measurement noise](#8-optional-reduce-measurement-noise)

[Alternatives](#alternatives) · [What the install script does](#what-the-install-script-does) ·
[How joule:code uses METRION](#how-joulecode-uses-metrion) ·
[Manual installation](#manual-installation-reference) · [Updating and uninstalling](#updating-and-uninstalling)

After the installation, continue with [configuration](configuration.md).

---

## 1. Before you start: requirements

| | Recommended (this guide) | Minimum (see [alternatives](#alternatives)) |
|---|---|---|
| Operating system | **Ubuntu 24.04 or 26.04 directly on the hardware** (no VM, no WSL) | any Linux or Windows with WSL 2 |
| CPU | **Intel** with RAPL energy counters | any x86-64 |
| GPU | **NVIDIA** with ≥ 12 GB VRAM ([which model for which GPU](#2-choose-a-language-model)) | none (LLM on the CPU, slow) |
| RAM | 32 GB | 16 GB |
| Disk | 50 GB free (Docker images, models, results) | 20 GB |
| Software | installed by the script | Docker, Python ≥ 3.11, Git |
| Access | a normal user with `sudo` rights | a user in the `docker` group |

Why these requirements?

- **Bare metal + Intel:** METRION reads the CPU's RAPL energy counters and attributes the energy
  to individual containers with eBPF. Virtual machines and WSL do not expose these counters.
  Check your CPU quickly:

  ```bash
  lscpu | grep 'Model name'                        # should be an Intel CPU
  ls /sys/class/powercap/ | grep intel-rapl        # RAPL present? (empty = no energy counters)
  systemd-detect-virt                              # should print "none"
  ```

- **NVIDIA GPU:** the optimiser runs a code LLM locally with [Ollama](https://ollama.com). On a
  GPU, one optimisation run takes 30 min to a few hours; on the CPU considerably longer. joule:code
  always unloads the model before measuring, so the GPU never disturbs the measurement.
- **A dedicated machine:** measurements are only meaningful if nothing else runs at the same
  time (no desktop session, no browser, no automatic updates).

## 2. Choose a language model

joule:code uses the **Qwen2.5-Coder** models via Ollama. The bigger the model, the more
candidates pass the tests — for PHP, JavaScript and Java the 7B model hardly produces usable
candidates. Choose the largest model that fits into your GPU's memory:

| GPU memory (VRAM) | Recommended model | Download | Notes |
|---|---|---|---|
| **≥ 12 GB** (e.g. RTX 3060 12 GB, RTX 4070 Ti, A4000) | **`qwen2.5-coder:14b`** | 9.0 GB | **default** — good results for all four languages |
| ≥ 24 GB (e.g. RTX 3090/4090, A5000, L4) | `qwen2.5-coder:32b` | 20 GB | strongest candidates; not yet evaluated with the [test suite](../examples/suite/README.md) |
| 8 GB (e.g. GTX 1080, RTX 3070) | `qwen2.5-coder:14b` | 9.0 GB | runs partly on the CPU: slower, but works (this is the reference host) |
| 8 GB, speed matters more | `qwen2.5-coder:7b` | 4.7 GB | fits completely into the GPU; good for Python, weak for PHP/JS/Java |
| no NVIDIA GPU | `qwen2.5-coder:7b` | 4.7 GB | runs on the CPU — see [without an NVIDIA GPU](#without-an-nvidia-gpu) |

The model also needs system RAM when parts of it run on the CPU (≈ the download size plus a few
GB). The choice is stored in `.joulecode.env` (`JOULECODE_OLLAMA_MODEL`), so you never have to
pass `--model` on the command line. You can change it at any time — see
[configuration](configuration.md#change-the-language-model).

## 3. Get joule:code

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/fhac-ida/joulecode.git ~/joulecode
cd ~/joulecode
```

**METRION ≥ 0.2 (recommended if you have it).** METRION is developed separately (also in the ITEA
project [GreenCode](https://itea4.org/project/greencode.html)). The install script installs the
public version from [GitHub](https://github.com/ds-lab/Metrion) automatically. If you have
received METRION ≥ 0.2 as a wheel (`.whl`), zip archive or source folder, keep its path at hand
for the next step — it is faster and needs no BCC. Never put METRION files into the joule:code
repository (separate project and license).

## 4. Run the install script

Run the script as your normal user (not as root) from the repository folder. It asks for your
`sudo` password when needed.

```bash
# METRION from GitHub, default model qwen2.5-coder:14b
./scripts/install.sh

# or: METRION ≥ 0.2 from a wheel, zip or folder
./scripts/install.sh --metrion-source ~/Downloads/metrion-0.2.0.zip

# another model (see step 2), e.g. on a 24 GB GPU
JOULECODE_OLLAMA_MODEL=qwen2.5-coder:32b ./scripts/install.sh
```

The script installs system packages, Docker, the NVIDIA driver and container toolkit, METRION as
an on-demand service (including calibration — **the desktop freezes for about 2 minutes**, that
is intended), joule:code in `.venv`, the settings file `.joulecode.env` and Ollama with the
model. Each step is explained in [What the install script does](#what-the-install-script-does).
All output goes to `install.log` in the repository.

Show all options with `./scripts/install.sh --help`:

| Option | Effect |
|---|---|
| `--metrion-source PATH` | install METRION ≥ 0.2 from a wheel (`.whl`), zip or source folder |
| `--skip-calibrate` / `--recalibrate` | skip / repeat the calibration of METRION ≥ 0.2 |
| `--metrion-keep-raw` | METRION ≥ 0.2: also store raw data next to the live result |
| `--metrion-ref REF` | GitHub version: use a specific branch, tag or commit |
| `--metrion-dir DIR` | installation directory (default `/opt/metrion`) |
| `--skip-nvidia` | do not touch the NVIDIA driver and container toolkit |
| `--skip-ollama` | do not start Ollama |
| `--skip-bcc` | do not build BCC |
| `--tune` | CPU governor `performance` and turbo off at every boot ([step 8](#8-optional-reduce-measurement-noise)) |
| `-y` | no questions |

Environment variables for the script: `JOULECODE_OLLAMA_MODEL`, `METRION_SOURCE`, `METRION_REPO`,
`METRION_REF`, `METRION_DIR`, `BCC_VERSION`, `NVIDIA_DRIVER`.

## 5. Reboot and run the script again

If the NVIDIA driver was installed, the script stops and asks for a reboot:

```bash
sudo reboot
# after the reboot:
cd ~/joulecode && ./scripts/install.sh            # same options as before
```

Completed steps are skipped, so the script can always be run again safely.

## 6. Check the installation

Log out and in again (so that the `docker` group takes effect), then:

```bash
cd ~/joulecode
source .venv/bin/activate          # also loads .joulecode.env
joulecode doctor
```

`joulecode doctor` checks operating system, cgroups, Docker, GPU, RAPL and every energy back-end
and prints the most accurate one that is ready. Expected on a measuring host:
`Most accurate available back-end: metrion` (or `metrion-legacy` for the GitHub version).
If a line shows `✘` or `!`, its message says what is missing.

Every command explains itself with `--help`, e.g. `joulecode --help` or `joulecode measure --help`.

## 7. First measurement

```bash
# 1. measure the original against itself – shows how much this machine fluctuates
joulecode measure examples/python-wordstats --aa

# 2. measure a hand-written optimisation against the original
joulecode measure examples/python-wordstats --candidate-patch examples/python-wordstats-optimized.patch

# 3. let the LLM find and measure optimisations (30–60 min)
joulecode optimize examples/python-wordstats
```

The A/A measurement should report **no significant difference** with a narrow confidence
interval (a few percent). The second one should show a large saving (around −95 %). Each run
writes a folder under `joulecode-runs/` with `report.html` — what the output means is explained in
[the README](../README.md#usage).

Now [configure joule:code for your own repository](configuration.md).

## 8. Optional: reduce measurement noise

For more reproducible results, fix the CPU frequency. `--tune` does this at every boot:

```bash
./scripts/install.sh --tune
```

By hand, until the next reboot:

```bash
sudo cpupower frequency-set -g performance
echo 1 | sudo tee /sys/devices/system/cpu/intel_pstate/no_turbo
```

Also: no desktop session, browser or automatic updates (`unattended-upgrades`) during
measurements, and pin benchmarks to a fixed core with `limits.cpuset` in `joulecode.yml` (not
core 0, see [configuration](configuration.md#joulecodeyml-reference)). Repeat
`joulecode measure REPO --aa` after changes to see the effect.

---

## Alternatives

The recommended set-up above gives you real energy numbers and a capable LLM. Each of the
following alternatives works, with the limitation stated.

### Without an NVIDIA GPU

Ollama then runs the model on the CPU — candidates take several times longer to generate; the
measurement is unaffected. Use the 7B model unless you have a lot of time:

```bash
JOULECODE_OLLAMA_MODEL=qwen2.5-coder:7b ./scripts/install.sh --skip-nvidia --skip-ollama
# start Ollama without GPU reservation and pull the model:
docker run -d --name joulecode-ollama --restart unless-stopped -p 127.0.0.1:11434:11434 \
    -v ollama:/root/.ollama -e OLLAMA_NUM_PARALLEL=1 ollama/ollama:0.32.15
docker exec joulecode-ollama ollama pull qwen2.5-coder:7b
```

### Without METRION: whole-socket RAPL (back-end `rapl`)

The `rapl` back-end reads the energy counters in `/sys/class/powercap` directly: whole socket
(package + DRAM) minus the idle power measured beforehand. Everything else running on the
machine is included, so it is less precise than METRION, but needs no eBPF. Since 2020 the
counters are readable by root only (a mitigation for the PLATYPUS side channel). To allow
joule:code to read them on a dedicated measuring host:

```bash
sudo chmod a+r /sys/class/powercap/intel-rapl:*/energy_uj /sys/class/powercap/intel-rapl:*:*/energy_uj   # until reboot
# permanently:
echo 'z /sys/class/powercap/intel-rapl:*/energy_uj 0444 - - -
z /sys/class/powercap/intel-rapl:*:*/energy_uj 0444 - - -' | sudo tee /etc/tmpfiles.d/joulecode-rapl.conf
```

Only do this on a machine where no untrusted users can log in. `--backend auto` picks `rapl`
when METRION is not available; to force it, set `JOULECODE_BACKEND=rapl` in `.joulecode.env`.

### Without a measuring host: any Linux or WSL 2 with Docker (back-end `cgroup`)

joule:code runs anywhere Docker runs. It then compares **CPU time** instead of energy — a good
proxy for CPU-bound code, and every report says clearly that it is not energy. Requirements:
Docker, Python ≥ 3.11, Git; no root, no GPU.

```bash
git clone https://github.com/fhac-ida/joulecode.git && cd joulecode
python3 -m venv .venv
.venv/bin/pip install -e .                                  # add '.[server]' for joulecode serve

# settings file, loaded automatically by `source .venv/bin/activate`
cp .joulecode.env.example .joulecode.env
echo 'export JOULECODE_BACKEND="cgroup"' >> .joulecode.env
echo '[ -f "$VIRTUAL_ENV/../.joulecode.env" ] && . "$VIRTUAL_ENV/../.joulecode.env"' >> .venv/bin/activate

source .venv/bin/activate
joulecode doctor                                            # expected: back-end cgroup ready
joulecode measure examples/python-wordstats --candidate-patch examples/python-wordstats-optimized.patch
```

For `optimize`, start Ollama as described in [without an NVIDIA GPU](#without-an-nvidia-gpu), or
with GPU via `docker compose up -d` (needs the NVIDIA Container Toolkit).

### Older METRION from GitHub (back-end `metrion-legacy`)

Without `--metrion-source` the script installs the public GitHub version (< 0.2). It needs
**BCC ≥ 0.35** (Ubuntu 26.04 ships it; on 24.04 the script builds it from source, 10–20 min), and
stopping METRION after a measurement can take minutes. joule:code handles this automatically;
details in [How joule:code uses METRION](#older-github-version--02-back-end-metrion-legacy).

---

## What the install script does

1. **Pre-flight check:** Ubuntu ≥ 24.04, no VM/WSL, Intel CPU, RAPL present, DRAM domain yes/no.
2. **System packages**, including kernel headers and `linux-tools`.
3. **eBPF prerequisites.** METRION ≥ 0.2 ships its own eBPF programs (libbpf, needs a kernel
   with BTF); building from source only needs `clang`, `libelf-dev` and `zlib1g-dev`, a wheel
   needs nothing. Only for the GitHub version: **BCC ≥ 0.35**. Ubuntu 26.04 ships it as a
   package (`python3-bpfcc`, `libbpfcc`); on 24.04 the package is too old, so BCC `v0.35.0` is
   built from source with the newest available LLVM and installed to `/usr` (10–20 min). A
   suitable package is never removed.
4. **Docker Engine** from Docker's official repository, user added to the `docker` group,
   cgroup v2 check.
5. **NVIDIA:** `nvidia-driver-580` (the last series supporting Pascal, e.g. GTX 1080; override
   with `NVIDIA_DRIVER`) and the NVIDIA Container Toolkit → reboot required.
6. **Rust** (rustup) and **uv**.
7. **METRION ≥ 0.2** (with `--metrion-source`): fresh venv with Python 3.12 (via uv) in
   `/opt/metrion/.venv`; a wheel is installed directly, sources are copied to
   `/opt/metrion/src` and built (10–20 min, Rust + eBPF) — **never** inside the joule:code
   repository. Later runs without `--metrion-source` keep the installed version. An existing
   GitHub version is moved to `/opt/metrion-github-<date>`, not deleted.
   Without `--metrion-source`: **METRION from GitHub** (`https://github.com/ds-lab/Metrion.git`)
   into `/opt/metrion`, with Python 3.12 from uv. The BCC Python bindings are linked into
   METRION's venv via a `.pth` file. If the build fails with the known `ethnum` error of newer
   Rust versions, the script updates that one crate and retries.
8. **On-demand METRION service** `joulecode-metrion.service`. For METRION ≥ 0.2: working
   directory `/var/lib/metrion` with `metrion.yaml` (from `metrion init`, adjusted by
   `deploy/metrion/configure_v2.py`: Parquet, live attribution on, raw data off, GPU/network
   off, DRAM off if the CPU has no DRAM domain) and **calibration** (`metrion calibrate`
   measures idle power; desktop and terminal freeze for ~2 min). For the GitHub version:
   `/etc/joulecode/metrion.yaml`. Plus a **sudoers rule**: your user may run exactly
   `systemctl start|stop|is-active joulecode-metrion.service` without a password — nothing else.
   The files are described in [deploy/metrion/README.md](../deploy/metrion/README.md).
9. **joule:code** in `.venv` inside the repository and the settings file **`.joulecode.env`**
   (METRION paths, Ollama address and model), which `source .venv/bin/activate` loads. The file
   is created from [`.joulecode.env.example`](../.joulecode.env.example) on the first run; later
   runs only update these lines and keep everything else you have added.
10. **Ollama** via `docker compose` with the chosen model (default `qwen2.5-coder:14b`, 9 GB).
11. Optionally `--tune`: more reproducible measurement conditions at every boot.
12. `joulecode doctor`.

## How joule:code uses METRION

You do not have to do anything by hand — joule:code starts and stops METRION for every
measurement. This section helps with troubleshooting.

### METRION ≥ 0.2 (back-end `metrion`)

1. joule:code starts `joulecode-metrion.service` (`metrion monitor` in `/var/lib/metrion`) and
   waits 20 s until METRION is ready.
2. All benchmark runs, each in its own container; the containers stay until the end.
3. joule:code waits 50 s (METRION writes a time window only once it is 30 s old), stops the
   service and copies the live result `energy_<UTC time>` into the result folder
   (`<result folder>/metrion/`, a few MB).
4. Evaluation uses only METRION's official command line:
   `metrion analyze <energy_…> --cgroup docker- --csv <tmp>`. joule:code sums the **dynamic**
   energy (CPU + DRAM, without the idle share) of all rows whose cgroup contains the
   container ID.

Stopping takes seconds. `joulecode reanalyze <result folder>` evaluates the archived copy again.
Runs in `/var/lib/metrion` older than 7 days are deleted when the service next starts.

Troubleshooting: `journalctl -u joulecode-metrion -n 100`; by hand:
`cd /var/lib/metrion && sudo /opt/metrion/.venv/bin/metrion monitor` (Ctrl+C to stop), then
`/opt/metrion/.venv/bin/metrion analyze energy_<time> --cgroup docker-`.

### Older GitHub version < 0.2 (back-end `metrion-legacy`)

The GitHub version writes to a DuckDB file (`/var/lib/metrion/monitor.db`) and evaluates
afterwards; while METRION writes, nobody else can read the file. So for each measurement
campaign:

1. joule:code starts `joulecode-metrion.service` (fresh database) and waits 20 s.
2. All benchmark runs, each in its own container. Containers stay until the end so that
   METRION can still resolve their cgroup to a path.
3. joule:code stops the service (METRION flushes and exits), copies the database into the
   result folder and evaluates it with METRION's own `execute_queries`: the sum of **active**
   energy (CPU + DRAM) of all threads whose cgroup path contains the container ID.

**Stopping METRION can take minutes.** It drains a write queue before exiting, otherwise
measurements would be lost. joule:code shows every 15 s how long it has been waiting and how
large the database is. After 15 min (`TimeoutStopSec`) systemd kills METRION.

Runs are written to the result folder **before** stopping, so Ctrl+C during the evaluation
loses no measurement: joule:code cleans up the containers, writes the reports, and
`joulecode reanalyze` can catch up later (from the archive or from
`/var/lib/metrion/monitor.db`, as long as no new measurement has started). The database is
copied **together with `monitor.db.wal`**, where DuckDB keeps data until a clean close.

### Live attribution (back-end `metrion-live`)

For METRION versions with `live_attribution` (Parquet output) there is the back-end
`metrion-live`, using `deploy/metrion/config.live.yaml` and
`deploy/metrion/metrion-live.service` (runs permanently; joule:code only reads the Parquet
files). `--backend auto` chooses it when it finds fresh output. See
[deploy/metrion/README.md](../deploy/metrion/README.md).

## Manual installation (reference)

The steps of the script one by one, in case something goes wrong (shown for the GitHub version
of METRION):

```bash
# Packages
sudo apt update && sudo apt install -y git curl build-essential pkg-config python3-venv \
    linux-headers-$(uname -r) linux-tools-common linux-tools-$(uname -r)

# BCC (only for METRION < 0.2) – Ubuntu 26.04: the package is enough (>= 0.35)
sudo apt install -y bpfcc-tools python3-bpfcc libbpfcc libbpfcc-dev
# BCC – Ubuntu 24.04: from source (https://github.com/iovisor/bcc/blob/master/INSTALL.md)
sudo apt install -y zip bison cmake flex libedit-dev libllvm18 llvm-18-dev libclang-18-dev zlib1g-dev \
    libelf-dev libfl-dev python3-setuptools liblzma-dev libdebuginfod-dev libpolly-18-dev
git clone --depth 1 --branch v0.35.0 https://github.com/iovisor/bcc.git && mkdir bcc/build && cd bcc/build
cmake .. -DPYTHON_CMD=python3 -DCMAKE_INSTALL_PREFIX=/usr && make -j$(nproc) && sudo make install

# Docker
curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker $USER

# NVIDIA (e.g. GTX 1080 = Pascal → driver series 580)
sudo apt install -y nvidia-driver-580 && sudo reboot
# Container toolkit: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html

# METRION (GitHub version)
sudo mkdir -p /opt/metrion && sudo chown $USER /opt/metrion
git clone https://github.com/ds-lab/Metrion.git /opt/metrion && cd /opt/metrion
uv python install 3.12 && uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e .
# on error E0512 in ethnum:  cargo update -p ethnum  and build again
# link the system's bcc bindings
BCC_DIR=$(dirname "$(dpkg -L python3-bpfcc | grep -m1 '/bcc/__init__.py$')")   # self-built: python3 -c 'import bcc;print(bcc.__file__)'
mkdir -p .venv/bcc-site && ln -sfn "$BCC_DIR" .venv/bcc-site/bcc
echo "$PWD/.venv/bcc-site" > "$(.venv/bin/python -c 'import sysconfig;print(sysconfig.get_paths()["purelib"])')/joulecode-bcc.pth"
.venv/bin/python -c 'from bcc.version import __version__; print(__version__)'

# Service + configuration
sudo mkdir -p /etc/joulecode /var/lib/metrion
sudo cp ~/joulecode/deploy/metrion/config.github.yaml /etc/joulecode/metrion.yaml
sed 's#@METRION_DIR@#/opt/metrion#g' ~/joulecode/deploy/metrion/joulecode-metrion.service \
    | sudo tee /etc/systemd/system/joulecode-metrion.service && sudo systemctl daemon-reload
echo "$USER ALL=(root) NOPASSWD: /usr/bin/systemctl start joulecode-metrion.service, /usr/bin/systemctl stop joulecode-metrion.service, /usr/bin/systemctl is-active joulecode-metrion.service" \
    | sudo tee /etc/sudoers.d/joulecode-metrion && sudo chmod 440 /etc/sudoers.d/joulecode-metrion

# joule:code + settings file
cd ~/joulecode && python3 -m venv .venv && .venv/bin/pip install -e ".[metrion]"
cp .joulecode.env.example .joulecode.env       # then set the METRION_DB/METRION_PYTHON lines (< 0.2)
echo '[ -f "$VIRTUAL_ENV/../.joulecode.env" ] && . "$VIRTUAL_ENV/../.joulecode.env"' >> .venv/bin/activate

# Ollama + model (model from .joulecode.env)
set -a; . ./.joulecode.env; set +a; docker compose up -d
```

Many desktop CPUs have **no `dram` domain** in RAPL
(`cat /sys/class/powercap/intel-rapl:0:*/name`). In that case set `mode: exclude` under
`dram:` in `/etc/joulecode/metrion.yaml`.

**GPU energy:** METRION reads `nvmlDeviceGetTotalEnergyConsumption`, which exists only from
Volta onwards; on Pascal (e.g. GTX 1080) the metric stays empty, so GPU is excluded in the
METRION profile. This is irrelevant for CPU-bound benchmarks.

## Updating and uninstalling

```bash
cd ~/joulecode && git pull && ./scripts/install.sh       # update; your .joulecode.env is kept
joulecode cleanup                                        # remove leftovers of aborted runs
```

To remove joule:code completely: `joulecode cleanup --images`, delete the repository folder,
`docker rm -f joulecode-ollama && docker volume rm joulecode_ollama_data`, and as root
`systemctl disable --now joulecode-metrion.service joulecode-tuning.service`, then remove
`/etc/systemd/system/joulecode-*.service`, `/etc/sudoers.d/joulecode-metrion`, `/opt/metrion`
and `/var/lib/metrion`.
