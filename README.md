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

Nonzero container exit codes are displayed prominently. If a requested shutdown
ends with an error, that operation fails and a build stops before backup/compile.
The manager does not repair core crashes. A later explicit build can operate on
an already stopped world to build a fix, but physical backups require a normally
stopped database.

Open **https://turtlewow-server/admin** with a separate dashboard login. Nginx
provides HTTPS on the VM and proxies Python on loopback. Salted PBKDF2-SHA256
password hashes stay outside Git. Sessions use Secure, HttpOnly, SameSite cookies,
per-session CSRF tokens, eight-hour expiry and a thirty-minute idle timeout.
Login attempts are limited. The management account can operate Docker; restrict
HTTPS to your trusted LAN. Game ports use the existing deployment.

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
Without it, availability depends on your Ubuntu user session.

Create a dashboard account and enable HTTPS from the checkout. Run separately:

```bash
python3 manager.py create-admin
```

```bash
bash deploy/install-https.sh
```

Enter the username/password interactively. To reset credentials, rerun
`create-admin`; existing sessions are invalidated. This account is separate from
Ubuntu and game accounts. The HTTPS proxy restarts independently of the realm.
`public_origin` in the manager config and `server_name` / forwarded Host in
`deploy/nginx.conf` must match your HTTPS hostname. The certificate script uses
`turtlewow-server` and its `.localdomain` alias. Other names require a replacement
certificate and matching configuration.

## Open from Windows

Ensure `turtlewow-server` resolves to your VM; reserve its IP in your router.
Copy only the public certificate using your trusted SSH connection:

```powershell
scp paul@turtlewow-server:~/.config/turtlewow-manager/tls/server.crt "$HOME\Downloads\turtlewow-server.crt"
Import-Certificate -FilePath "$HOME\Downloads\turtlewow-server.crt" -CertStoreLocation Cert:\CurrentUser\Root
```

Confirm the certificate came from your VM before trusting it. Its private key
stays on Ubuntu. Open **https://turtlewow-server/admin** and use your dashboard
login. No SSH tunnel is needed. Other Windows users/computers need the public
certificate trust step too. The certificate lasts 825 days; regenerate it before
expiry, restart the proxy and trust the replacement. Remove the old certificate
from Windows after replacement.

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

### Incremental Ubuntu builds

The dashboard passes the local committed core checkout to Docker as the
`local-core` named context and selects the packaging repository's `local-builder`.
Compilation still uses its Ubuntu toolchain; installing compilers on the VM host
is unnecessary. The builder copies the requested commit and Eluna submodule
from local Git objects into a separate persistent cache, without changing the
development checkout. The existing origin-SHA check remains in place.

BuildKit retains one source/build tree per target platform in
`turtlewow-native-v1-*`, including CMake dependency information and object files.
Unchanged files keep their timestamps across revisions. Make recompiles changed
files and files affected by changed headers or compiler flags. A **3 GB ccache**
limit bounds the secondary compiler-result cache; it is not a cap on the whole
object/build tree. Every build still embeds and verifies its requested SHA.

The first build, a toolchain change or removal/eviction of Docker's build cache
can require a full compilation. Documentation-only changes normally require no
C++ compilation, although the core's generated revision metadata may require a
small rebuild/relink. Packaging, image export and cold backup still take time.
Starting the realm never compiles it. Two parallel compile jobs remain the default.

Normal image cleanup should retain build cache. `docker builder prune`/Docker
system pruning can discard it; that loses speed, not source or runtime data.
The cache is reusable build data, not a backup. BuildKit's locked cache mount
serializes writers in addition to the dashboard's existing operation lock.
Use a packaging revision that contains `local-builder` with this manager version.

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


The core must also keep the changing Git revision out of the bot precompiled
header. `PlayerbotAIConfig.h` declares the default filename; its platform-specific
definition and `SystemConfig.h` include live in `PlayerbotAIConfig.cpp`. Older
core revisions with that include in the shared header legitimately invalidate
the entire bot PCH on every new commit. Caching cannot bypass that dependency.
The incremental fixture includes PCH consumers and private revision metadata.
