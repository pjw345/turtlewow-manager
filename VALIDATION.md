# Validation — 2026-10-11

Validated on the actual Ubuntu Hyper-V VM using Python 3.14 and Docker Engine
29.9.0 / Compose 5.6.0. Automated CI also uses Python 3.12.

- Twenty-one automated tests cover process-level operation exclusion, immediate
  HTTP conflict handling, failed stop/backup/build paths, source verification,
  deployment-image validation, initializer protection, unclean DB backup refusal,
  password redaction, and Host/origin/CSRF request guards.
- Desktop (1366 px) and mobile (390 px) screenshots reviewed with no horizontal
  overflow. Live browser controls exercised; no browser console errors observed.
- A real cold backup succeeded and its independent Windows copy passed SHA-256
  checks. Archive/checksum verification does not constitute a new restore test.
- Native console telemetry was observed: zero human sessions, ten bots.
- The existing development build helper was used to build explicit core
  `76c1741d80e44ba5050b87445b5a8f729d1f6a10`, using the existing build cache.
  All services stopped before backup/build. A simultaneous CLI startup attempt
  was rejected by the shared file lock. No changed game code was compiled.
- A verified same-core build was activated and started through the browser;
  MariaDB, login and world readiness passed. Final services are stopped.
- Twelve obsolete deployment entries were archived with before/after hashes;
  runtime Docker/config/client files and all existing backups remain.

## Core shutdown failure discovered

The final shutdown with ten bots online exited `mangosd` with code **139**,
`OOMKilled=false`. The database and login server exited zero. The core log says
`Received SIGSEGV` after `Unloading all maps`, with frames through
`TerrainInfo::GetAreaFlag`, `WorldPosition::GetArea`, `TravelMgr::GetAreaLevel`,
`TravelMgr::IsLocationLevelValid`, `TravelMgr::GetPartitions` and an asynchronous
future worker. This points to travel/terrain lifetime during teardown, but
the exact lifetime defect has not been proven or repaired here.

The manager now shows the abnormal exit explicitly and treats a requested
shutdown with nonzero exit status as a failed operation. A build encountering
that failure aborts before backup/compilation. An already stopped world can be
used by a subsequent explicit build to build a fix; an unclean database exit
always blocks physical backups. Automated tests cover both conditions.

The captured game log and runtime observations are retained in the operator's
development evidence directory. No core source changes or database repairs
were applied as part of the dashboard implementation. Gameplay correctness
and resolution of this core shutdown failure remain outside these checks.
