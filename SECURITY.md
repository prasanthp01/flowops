# Security policy

Report suspected vulnerabilities through [private vulnerability reporting](https://github.com/prasanthp01/flowops/security/advisories/new). Include the affected version, minimal reproduction, expected boundary and observed result. Do not put secrets or private documents in a public issue. There is no guaranteed response SLA.

## Supported scope

The current documented release runs synthetic fixtures by default. Anonymous requests cannot supply provider credentials, arbitrary connector URLs, SQL, shell commands or executable plugins. Public execution never performs paid inference or a customer-side action.

Requests are schema- and size-bounded. Session ownership, CSRF/origin checks, rate limits, queue limits and retention apply to public runs. Private service credentials have a separate configured scope. Model output and retrieved text are untrusted; deterministic policy owns authorization. See the architecture and API documentation for project-specific controls.

## Operator responsibilities

Use HTTPS and secure cookies for public hosts. Bind services to loopback or an isolated proxy network, preserve the documented path prefixes, and do not expose databases. Generate fresh credentials; use a secret manager or owner-only configuration. Encrypt off-host backups and verify restoration into a disposable environment. Keep audit logs free of tokens, raw prompts and personal records. Apply retention and access rules before introducing real data.

Review dependency updates and container advisories. A passing scanner or test suite is evidence for specific checks, not a security certification. Formal penetration testing, commercial-load soak testing and live-provider evaluations are not claimed.
