# Security Policy

## Reporting a Vulnerability

We take the security of `mcp-awareness` seriously. If you believe you have found a security vulnerability, please report it to us as described below.

**Please do NOT report security vulnerabilities through public GitHub issues.**

### How to Report

You can report security vulnerabilities through:

1. **GitHub Security Advisories**: Use the [GitHub Security Advisories](https://github.com/cmeans/mcp-awareness/security/advisories) feature
2. **Email**: Contact us at `security@cmeans.dev` (if available) or via the repository owner's GitHub profile

### What to Include

Please include the following information in your report:

- A description of the vulnerability
- Steps to reproduce the issue
- Potential impact of the vulnerability
- Any suggested fixes (if applicable)

## Response Expectations

- We acknowledge receipt of vulnerability reports within **72 hours**
- We aim to provide an initial assessment within **7 days**
- We will keep you informed of our progress throughout the remediation process

## Scope

### In-Scope

- Server code in this repository
- Demo installation scripts
- Documentation related to security configurations

### Out-of-Scope

- Upstream dependencies (please report these to their respective maintainers)
- Third-party integrations not maintained by this project
- Documentation typos or non-security-related issues

## Disclosure Policy

We follow a **90-day coordinated disclosure** policy:

1. We will work with you to verify and remediate the vulnerability
2. We aim to release a patch within 30 days of confirmation
3. After 90 days, we may publicly disclose the issue if a patch is available
4. Earlier disclosure may occur if a patch is released before the 90-day window

## Credit Policy

We believe in giving credit where credit is due. We will:

- Acknowledge your contribution in the security advisory
- Include your name in our "Security Hall of Fame" (if you wish)
- Coordinate with you on CVE assignment if applicable

## Safe Harbor

We support safe harbor for security researchers who:

- Make a good faith effort to avoid privacy violations, data loss, or other harm
- Follow responsible disclosure practices
- Do not exploit the vulnerability beyond what is necessary to demonstrate it

**We will not pursue legal action against you for good-faith security research** that follows this policy.

## Additional Security Features

### GitHub Private Vulnerability Reporting

This repository has GitHub Private Vulnerability Reporting enabled. You can use this feature to securely report vulnerabilities.

### Bug Bounty

At this time, we do not offer a formal bug bounty program. However, we deeply appreciate the time and effort of security researchers who help us improve our security posture.

## Security Best Practices

When deploying `mcp-awareness`:

1. Keep the software up to date with the latest security patches
2. Use strong authentication mechanisms
3. Follow the principle of least privilege when configuring access
4. Regularly audit logs and access patterns
5. Review and update security configurations as needed

---

*Last updated: April 2026*
