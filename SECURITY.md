# Security

## Security model

joule:code **executes the code it measures** — tests and benchmarks of the analysed repository and
of every LLM-generated candidate. That is the core of the approach, so the boundaries matter:

- Every test and benchmark run happens in a **fresh Docker container** without network, with
  CPU, memory and PID limits, all capabilities dropped and `no-new-privileges`.
- joule:code itself needs access to the Docker daemon, which is **equivalent to root** on that
  machine. Run it on a **dedicated measuring host**, not on a workstation with sensitive data.
- Only analyse repositories you trust as much as you would trust running their test suite.
- METRION runs as root (eBPF); joule:code only gets a narrow sudoers rule to start and stop its
  service. Making RAPL counters world-readable (back-end `rapl`) enables power side channels —
  only do it on a machine without untrusted users.

### Platform (`joulecode serve`)

- Listens on `127.0.0.1` by default. Reading (projects, jobs, results) needs no token; writing
  needs the admin token or a project token. Only token hashes are stored.
- **Anyone with a project token can run code on the measuring host** via the `gate` and
  `optimize` modes. Accept jobs only from trusted branches — never from forks — and store tokens
  as CI secrets.
- Uploaded archives are treated as untrusted (no links, no absolute paths or `..`, size and
  file-count limits). HTML reports are served with `Content-Security-Policy: sandbox`.
- If the server must be reachable over the network, put it behind a reverse proxy with TLS and
  access control.

Details: [docs/platform.md](docs/platform.md#security).

## Reporting a vulnerability

Please **do not open a public issue**. Report vulnerabilities privately via
[GitHub's private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
(“Security” tab → “Report a vulnerability”) of this repository. Include a description, steps to
reproduce and the affected version. We will acknowledge the report and keep you informed about
the fix.

joule:code is a research prototype; only the latest version on the default branch receives
security fixes.
