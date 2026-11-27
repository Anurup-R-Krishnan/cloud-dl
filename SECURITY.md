# Security Policy

## Supported Versions

| Version | Supported |
| ------- | --------- |
| 2.4.x   | Yes       |
| < 2.4   | No        |

## Reporting a Vulnerability

Do not open public GitHub issues for security vulnerabilities or suspected credential exposure.

Instead, please report issues privately through GitHub Security Advisories (`Security -> Advisories -> Report a vulnerability` on `https://github.com/Anurup-R-Krishnan/cloud-dl`) with:
- A clear description of the vulnerability and affected files
- Steps to reproduce or minimal proof of concept
- Potential impact and suggested remediation if available

We aim to acknowledge reports within 72 hours.

## Credential Hygiene Reminder

- Never commit `~/.config/cloud-dl/config.json`, `.env`, SSH private keys, or AWS session files.
- Always generate runtime tokens with `cloud-dl init` (`0600` file permissions) and sync the rotated `rpc-secret` into `~/.aria2/aria2.conf` on your VPS hosts.
