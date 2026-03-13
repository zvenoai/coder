# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in this project, please report it responsibly.

**Do NOT open a public GitHub issue for security vulnerabilities.**

Instead, please email: **security@zveno.ai**

Include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

We will acknowledge receipt within 48 hours and provide a timeline for a fix.

## Supported Versions

| Version | Supported |
|---------|-----------|
| Latest  | Yes       |

## Security Practices

This project uses:
- **Semgrep** for static analysis (OWASP rules)
- **Gitleaks** for secret detection
- **pip-audit** for dependency vulnerability scanning
- **Pre-merge code review** with OWASP security checklist
