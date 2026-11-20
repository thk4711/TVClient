# Security policy

## Supported versions

The project is a development appliance. Security fixes are maintained on the
current main branch; there is no supported stable image or automatic firmware
update mechanism yet. Deploy only on a trusted LAN, not directly on the Internet.
Read [the security model](doc/security.md) and [update/backup procedures](doc/operations.md).

## Reporting a vulnerability

Do not post credentials, exploit details or sensitive device exports in public
issues. Use GitHub’s **Security → Report a vulnerability** when private reporting
is enabled for this repository. If it is unavailable, open an issue requesting a
private contact route **without disclosing the vulnerability**.

Include the source/image identity, affected configuration, impact and a minimal
sanitized reproduction in the private report. Avoid collecting real secrets to
demonstrate file-access or privilege defects.

Before making the repository public, the maintainer must enable GitHub private
vulnerability reporting or provide an alternative private reporting route.
