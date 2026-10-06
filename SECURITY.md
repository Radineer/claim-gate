# Security policy

claim-gate is designed to be read-only: SQL checks accept only `SELECT`, process checks only observe
(`pgrep`, `pm2 jlist`), and HTTP checks issue `GET`. If you find a way to make a manifest write,
delete, execute arbitrary commands, or reach the network beyond the URL it names, please treat it as
a vulnerability.

Please report it privately via GitHub's "Report a vulnerability" (Security tab of this repository)
rather than a public issue. We aim to acknowledge within 7 days.

Supported version: the latest release on the default branch.
