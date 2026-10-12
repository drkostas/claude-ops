# Working in this repository

This is a public open-source package. Its users read everything in it, including commit messages, pull request titles and descriptions, issues and release notes. Commit messages on main are never rewritten, so write each one for someone who has never seen any project that uses this package.

- Describe what the change does for a user of the package, and why they would want it.
- Do not name the projects, people, machines or networks that use this package, and do not tell the story of one particular installation. Say "the user".
- Keep real hostnames, IP addresses, account names, file paths and credentials out of code, tests, docs and history. Examples use `example.org`, `192.0.2.x` and `/path/to`.
- Do not describe how any particular installation is secured or where its secrets are kept. Describe the mechanism and its safe default.
- General mechanisms belong here, written for every user. Code that only makes sense for one installation belongs in that installation, which uses this package.
- Every pull request runs `.github/workflows/private-terms.yml`, which refuses terms that must stay out of this repository. When it fails, rewrite the places it names. It does not print the term, because the log is public too.

CONTRIBUTING.md says how to run the tests.
