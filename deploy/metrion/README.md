# METRION deployment files

Files used by [`scripts/install.sh`](../../scripts/install.sh) to run
[METRION](https://github.com/ds-lab/Metrion) as the energy back-end of joule:code. You normally do
not need to touch them; see [docs/installation.md](../../docs/installation.md).

| File | Back-end | Purpose |
|---|---|---|
| `joulecode-metrion-v2.service` | `metrion` (≥ 0.2) | on-demand systemd unit template; installed as `joulecode-metrion.service`, started and stopped by joule:code per measurement campaign |
| `configure_v2.py` | `metrion` (≥ 0.2) | adapts the `metrion.yaml` created by `metrion init`: Parquet sink, live attribution on, raw data off, GPU/network off, DRAM off without a DRAM domain |
| `joulecode-metrion.service` | `metrion-legacy` (< 0.2) | on-demand systemd unit template for the GitHub version |
| `config.github.yaml` | `metrion-legacy` (< 0.2) | METRION configuration for the GitHub version (DuckDB, absolute path, GPU/network off) |
| `metrion-live.service` | `metrion-live` | permanently running unit for METRION versions with live attribution |
| `config.live.yaml` | `metrion-live` | matching configuration (Parquet, live attribution) |

`@METRION_DIR@`, `@METRION_BIN@` and `@METRION_WORKDIR@` in the unit templates are replaced by the
install script. In every case joule:code itself runs without root: a sudoers rule allows exactly
`systemctl start|stop|is-active joulecode-metrion.service`.
