# TurtleWoW Control Room

A single-page, private server dashboard for the Ubuntu Hyper-V deployment of
[`pjw345/tortoise-wow`](https://github.com/pjw345/tortoise-wow) and
[`pjw345/tortoise-docker`](https://github.com/pjw345/tortoise-docker).
Python 3.11+ and the browser's native APIs; no Node runtime, CDN, database
migrations or frontend build step. Docker Engine/Compose must already be installed.

## What it does

- Login/world/database container state and service readiness, VM memory and disk.
- Human client sessions and bot counts from the core's native `rndbot diff`
  console response. Bot sessions are not in `World::m_sessions`, so sessions
  and bots are reported independently. These are native samples, not account-name guesses.
- The source SHA embedded in the container's image label, selected deployment
  image and committed development SHA.
- Start, clean stop, cold backup, source-pinned build and separate build activation.
- Durable operation history and logs, archive checksums and recent backups.

The manager binds **127.0.0.1 only**. Access uses SSH authentication and a tunnel;
there is no public management port or separate dashboard password. The VM's
trusted local users can access it, and the service account has Docker privileges.
Requests are restricted to localhost Host headers, same-origin JSON and a CSRF
token. Do not proxy or expose this service publicly without adding authenticated
TLS access. Game ports remain controlled by the existing deployment.

## Install on the Ubuntu VM

```bash
mkdir -p ~/development
cd ~/development
git clone https://github.com/pjw345/turtlewow-manager.git
cd turtlewow-manager
# Adjust paths for your deployment before running setup:
mkdir -p ~/.config/turtlewow-manager
cp config.example.json ~/.config/turtlewow-manager/config.json
chmod 600 ~/.config/turtlewow-manager/config.json
nano ~/.config/turtlewow-manager/config.json
bash setup.sh
```

The example paths match our deployment. Use this checkout path or adapt the
service template. Config/state/backup directories are outside Git. `setup.sh`
preserves an existing configuration. The Compose deployment and containers are
not modified by installation; installing the dashboard does not start the realm.

For the user service to start when Ubuntu boots, before an SSH login:

```bash
sudo loginctl enable-linger "$USER"
systemctl --user status turtlewow-manager
```

Lingering is a one-time host setting requiring administrator permission.
Without it, availability depends on your user session. SSH sessions and the VM
must be running for a Windows tunnel to work.

## Open from Windows

In PowerShell (substitute your username/IP/key):

```powershell
ssh -N -L 8787:127.0.0.1:8787 -i "$HOME\.ssh\turtlewow_vm" YOUR_UBUNTU_USER@turtlewow-server
```

Keep that terminal open and browse to **http://127.0.0.1:8787**. Alternatively,
save a MobaXTerm SSH tunnel with local port 8787, destination 127.0.0.1:8787,
SSH server `turtlewow-server` and your usual Ubuntu SSH credentials. The local port
must remain 8787 with the default server config because Host validation uses it.

## Operations and build exclusion

Every supported start/stop/build/backup/activation action takes the same Linux
`flock` under the configured state directory. A second action gets an immediate
conflict; it is not silently queued. The lock is inherited by Docker subprocesses
so an orphaned Docker client continues holding it after a manager crash.

Build checks clean source/packaging trees and verifies the full SHA can be fetched
from the configured core origin. It stops world/login/DB, verifies all deployment
containers are stopped, takes a cold backup, compiles with two jobs, and verifies
`/opt/turtle/SOURCE_COMMIT`. It leaves the realm stopped. Failed builds do not
select an image or automatically restart the realm.

Activation takes another cold backup and changes only `TURTLE_IMAGE` in `.env`.
Startup remains a separate action. Image switching can run migrations at next
startup; an old image is not a database rollback. Restore compatible database
and configuration copies if reverting a migrated database.

Use the same guard from MobaXTerm/development scripts:

```bash
python3 ~/development/turtlewow-manager/manager.py start
python3 ~/development/turtlewow-manager/manager.py stop
python3 ~/development/turtlewow-manager/manager.py backup
python3 ~/development/turtlewow-manager/manager.py build
# Or build an explicit, committed, pushed core revision:
python3 ~/development/turtlewow-manager/manager.py build FULL_40_CHARACTER_SHA
python3 ~/development/turtlewow-manager/manager.py activate
```

The migrated `~/development/build-turtlewow.sh` delegates to this command.
Direct `docker compose up` or `docker build` commands bypass the shared lock;
use the dashboard or guarded CLI for normal management. Startup additionally
detects already-running ad-hoc Docker build clients, but it cannot prevent a
privileged user from issuing a later conflicting Docker command. Docker group
members remain trusted administrators. The manager does not prevent manual
Hyper-V shutdown, Docker restarts or other host administration.

## Backups

Backups archive the named volumes actually mounted by the stopped `db` and
`db-init` containers, plus `.env`, Compose, `config` and runtime `docker` files.
The exact volume names and database image IDs are recorded in a manifest.
Client data is not duplicated each time; retain a verified independent copy of
those assets. Archive readability and SHA-256 checksums are verified; that is
not the same as testing a restore. Physical DB restores require a compatible
MariaDB image and isolated volumes. Never restore into a running/live database.

The ordinary Backup action restarts only previously running services after a
successful backup. Failure leaves them stopped for review. Build/activation
backups always leave the services stopped. Incomplete backup directories are
marked `INCOMPLETE` and are excluded from the dashboard's verified list.

No backup deletion or volume pruning is exposed. Copy backups outside the VM,
test a restore and retain a rollback image before discarding older copies.
Existing migration/retirement backups are preserved, not imported into the new
recent-backup list. Existing Docker image/log retention remains a host setting.

## Maintenance and validation

```bash
cd ~/development/turtlewow-manager
python3 -m unittest discover -s tests -v
systemctl --user restart turtlewow-manager
journalctl --user -u turtlewow-manager -n 50
```

The automated checks exercise operation exclusion, failed shutdown/build,
source verification, backup failure and HTTP origin/CSRF guards using isolated
fake services. They do not compile the game or certify gameplay.

Dashboard sampling is every five seconds; native count requests are at most
every fifteen seconds. Unknown/stale telemetry is shown as unavailable, not
invented zero. A stopped world reports zero connected sessions/bots. The native
bot count itself updates with the Playerbot manager's location-count cycle.
World readiness requires the startup log or a native console response as well
as the game port. Container “running” alone is not marked world-ready.

## Scope

This is a single-realm control plane, not a general shell, database editor,
account/password manager or public player website. User input cannot select
arbitrary shell commands, paths, image references or SQL. Historical migration
and character-move scripts belong in the deployment's recovery archive rather
than the operational root. The runtime `docker/`, configuration and client data
remain essential and must not be removed during script cleanup.
