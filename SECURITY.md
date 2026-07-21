# Security

This tool intentionally weakens TLS to talk to an iDRAC6's ancient embedded
webserver (TLS 1.0, no certificate verification). That's documented, not a
bug, see the "Security considerations" section of the README. Only run it
against an iDRAC on a trusted, isolated management network.

## Reporting a vulnerability

If you find an actual security issue in the scripts themselves (not the
iDRAC's own TLS limitations, which are out of scope and already documented),
please open a GitHub issue or reach out to the maintainer directly rather
than posting exploit details publicly. Include what you found and, if
possible, how to reproduce it.

## What not to post publicly

`idrac_flash_log.txt` and any config backup written by
`idrac_backup_config.py` can contain your iDRAC's network/user/SNMP/IPMI
configuration. `idrac_flash_log.txt` itself doesn't log your password or
session cookie/token, so it's safe to attach to a bug report as-is; config
backups are not, treat them like credentials.
