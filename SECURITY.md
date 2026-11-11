# Security Policy

Craftsman is designed to run locally within developers' environments, orchestrating lifecycle hooks and enforcing quality gates across AI agent platforms (Claude Code, GitHub Copilot, Google Antigravity, Cursor, and Codex).

---

## Supported Versions

| Version | Supported          |
| :---    | :---               |
| 5.x     | :white_check_mark: |
| < 5.0   | :x:                |

---

## Security Architecture & Invariants

- **Local Execution**: Craftsman scripts and hooks execute on the local machine within the permissions of the invoking developer.
- **Search & Execution Scopes**: Craftsman enforces directory boundaries (`craftsman/scope.py`) preventing agents from reading or modifying arbitrary directories outside the current Git working tree.
- **Symlink & Traversal Protections**: Scratch and inbox directory managers strictly reject path traversal, outside symlinks, and junctions.
- **Zero Remote Dependencies**: The runtime library introduces zero third-party packages, minimizing supply-chain attack vectors.

---

## Reporting a Vulnerability

If you discover a potential security vulnerability in Craftsman, please report it responsibly:

1. **Do NOT open a public GitHub issue.**
2. Report the vulnerability privately via **GitHub Private Vulnerability Reporting** on the repository page:
   `https://github.com/BuczynskiRafal/craftsman/security/advisories/new`
   or email the maintainer directly at:
   `buczynski.rafal2@gmail.com`
3. Include detailed reproduction steps, platform context, and any proof-of-concept scripts.

### Response Timeline
- **Initial acknowledgment**: within 48 hours.
- **Assessment & reproduction**: within 5 business days.
- **Fix & advisory publication**: coordinated with the reporter following testing.
