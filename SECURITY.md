# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in this project, please report it
privately via
[GitHub's security advisory feature](https://github.com/curlewlabs-com/runner-pause-lease/security/advisories/new).

Do not open a public issue for security vulnerabilities.

## Scope

The tool changes runner labels and lease issues in the one repository it is
given, through `gh`. The security surface is:

- **The credentials it is handed.** `RUNNER_ADMIN_TOKEN` reaches only the
  runner label calls, and the ambient `gh` login reaches only the issue calls.
  It never prints either.
- **The `gh` it runs.** This is the first `gh` on `PATH`, trusted the way
  `python3` is. Arguments are passed as a list, never through a shell.
- **The lease issues it reads.** Every issue carrying the `ci-quiet` label is
  read as a lease, and its marker decides which labels a restore puts back on
  which runners, using the Administration credential. Only people who can label
  issues can make an issue a lease, and only people with write access can edit
  one they did not open. Do not put the label on an issue somebody else wrote,
  and treat label and write access to issues accordingly.
- **A host left stripped.** A sequence that leaves a host without its labels
  and without a lease to restore them is a defect in what the tool promises.
  Report it as a bug, or privately if you think it matters more than that.
