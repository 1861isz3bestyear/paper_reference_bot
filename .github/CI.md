# Debian compatibility checks

GitHub Actions runs `Debian compatibility` on pushes to `dev` and `main`,
pull requests targeting `main`, and manual dispatch. No local test runner or
VPS setup is needed. Each job uses a fresh Debian 11 or Debian 12 container
on a GitHub-hosted Ubuntu x86_64 runner and installs managed Python 3.14
with the application's locked dependencies. Both jobs begin without uv and run
`sh install/install.sh --entity data-saver --no-start`, exercising the real pinned
uv download, managed Python provisioning, locked sync, and service unit generation.
They repeat the bootstrap from another directory to check reuse. Shell regression
tests cover missing curl, download and sync failures, and argument forwarding.

The jobs check CLI imports, the application suite with its existing 92%
coverage gate, installer tests including service selection, and SMTP tests
with their existing 90% coverage gate. JUnit reports are uploaded as artifacts.
No GitHub secrets or trading credentials are required.

These containers do not run a user systemd manager. Installer tests use a
fake command runner; the bootstrap smoke test stubs only `systemctl` and
`systemd-analyze`; passing them does not prove service startup, reboot
recovery, journal access, ARM64 support, or exchange connectivity. The
credential-dependent `install/test_installed_services.py` suite is deliberately
not enabled by this workflow. Real post-install checks need a separate
disposable VM setup.

After the first workflow run, protect `main` using an active GitHub branch
ruleset. Require a pull request and both `Debian 11` and `Debian 12` status
checks, require the branch to be up to date, block force pushes and deletions,
and leave bypass permissions empty. Enable auto-merge in repository settings,
then enable it on the `dev` to `main` pull request if automatic merging is
wanted. The workflow itself neither merges nor deploys to a VPS.
