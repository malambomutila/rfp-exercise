# Deployment: rfp.malambomutila.com

This document covers how the generated RFP report reaches the web. It is
written for someone who has never seen the server.

There is one delivery path, and it runs entirely on this server: the
`rfp_cron` container builds the report on a daily schedule and writes it
straight into the directory that the `rfp_site` container serves at
https://rfp.malambomutila.com, behind a login gate.

An earlier version also published to GitHub Pages as a second path. That was
dropped once the server was working. It had been a quick way to get a shareable
link up before the server existed, and it was costing more than it returned:
each workflow ran the pipeline separately, so the two URLs served different
data from runs minutes apart. There is now one build and one destination, which
removes that whole class of inconsistency. The schedule itself later moved off
GitHub Actions and onto this server, so the project no longer depends on
anything outside the machine it runs on.

## What runs where

### The server

Host `198.54.121.71`, Ubuntu, reached as user `mm` over SSH. The user `mm` has
no passwordless sudo, so everything below is done through Docker and through
files that `mm` owns. Nothing here requires root on the host.

This is a shared production host. It also runs the OHASP stack: a Next.js app,
Directus, Superset, Airflow, pgAdmin and several Postgres databases. None of
that was modified.

The stack is a git clone of this repository. On this host it lives at
`/home/mm/apps/rfp-exercise`, and everything below is relative to
`<repo>/deploy/server`. Nothing is hardcoded to that path, so it runs from
wherever you clone it.

| Component | Location | Purpose |
|---|---|---|
| Whole stack | `<repo>/deploy/server/docker-compose.yml` | Three containers: scheduler, static server, login gate |
| Scheduler `rfp_cron` | `<repo>/deploy/server/run-scheduler.sh` | Builds the report daily and writes it into `site/` |
| Static file container `rfp_site` | same compose file | Serves `site/` over plain HTTP inside the Docker network and enforces the login gate |
| Login gate `rfp_auth` | `<repo>/deploy/server/auth/app.py` | Validates the login form, signs the session cookie |
| Site content | `<repo>/deploy/server/site/` | `index.html` and `data.json`, overwritten by each build |
| Report archive | `<repo>/deploy/server/reports/` | One dated HTML file per day, the content history |
| Configuration | `<repo>/deploy/server/.env` | Credentials and schedule. Gitignored, mode 600 |
| Inner server config | `<repo>/deploy/server/nginx-site.conf` | Serving rules, the login gate and a `/healthz` endpoint |
| Certificate renewal | `<repo>/deploy/server/renew-cert.sh` | Weekly cron, renews this one certificate and reloads nginx |
| Front-facing reverse proxy | `/home/mm/srv/ohasp/nginx/nginx.conf` | Pre-existing. Owns ports 80 and 443 for every site on the host. A vhost for `rfp.malambomutila.com` was added to it |
| TLS certificate | `/home/mm/srv/ohasp/nginx/ssl/live/rfp.malambomutila.com/` | Let's Encrypt, single domain, separate from the shared OHASP certificate |

The server-side files live in this repository under `deploy/server/`, and the
server runs them directly from a clone rather than from copies, so what is
reviewed here is exactly what runs:

- `docker-compose.yml`
- `nginx-site.conf`
- `renew-cert.sh`
- `nginx-vhost-rfp.conf.fragment`, a record of the block added to the shared
  nginx configuration. It is a fragment for reference, not a file the server
  reads.

### Why a container and a proxy rather than one or the other

Ports 80 and 443 were already bound by a pre-existing nginx container that does
TLS and vhost routing for the other live sites. Only one process can hold those
ports, so the new site could not bind them itself. Mounting the report directory
straight into that nginx container would have meant recreating it, which would
have interrupted every other site on the host.

So responsibilities are split. The `rfp_site` container serves the files and
publishes nothing publicly, binding only `127.0.0.1:8090` for on-host
diagnosis. The existing nginx terminates TLS and proxies to it across the
`ohasp_app_network` Docker network. The only change to the shared nginx was an
added server block, applied after a backup and validated before reloading, and
a reload never drops connections.

### The request path

```
browser
  -> 198.54.121.71:443            nginx container, TLS termination
  -> http://rfp_site:80           over the ohasp_app_network Docker network
  -> /usr/share/nginx/html        bind mount of /home/mm/apps/rfp/site, read-only
```

Port 80 for this host name serves only the ACME challenge directory and
redirects everything else to HTTPS.

The upstream is resolved through the Docker embedded DNS resolver at request
time rather than being fixed at reload time. That means recreating the
`rfp_site` container does not leave nginx holding a stale IP address, and a
stopped `rfp_site` container does not make `nginx -t` fail for the whole host.

## How the pipeline works

The schedule runs on this server, inside the `rfp_cron` container, not on
GitHub. That is deliberate: it means the project is self-contained. Anyone who
clones this repository onto any server gets a working daily tool without a
GitHub account, an Actions runner, a deploy key or a single repository secret.

`deploy/server/run-scheduler.sh` is the whole scheduler. On start it builds
once, so the site is never empty while you wait for the first scheduled run,
then it loops: work out how many seconds remain until the configured time,
sleep that long, build, repeat. It recomputes the next time from the clock on
every pass, so it does not drift and it behaves correctly if the container is
restarted at any hour.

A build is `python3 /app/src/main.py --days 7 --out /app/site
--archive /app/reports`. The output lands directly in the directory that
`rfp_site` serves, so there is no copy step, no rsync and nothing to go wrong
between building and publishing.

### Why a sleep loop rather than crond

A container running crond needs its log plumbed to stdout to be visible to
`docker logs`, needs the environment exported into the crontab because cron
does not inherit it, and fails quietly in ways that are tedious to debug. The
loop is a dozen lines, logs like every other container, and is easy to reason
about.

### What happens when a build fails

The pipeline's exit status is captured rather than propagated, so a source
outage or a transient network failure costs one day's refresh rather than
stopping the scheduler. The previous report keeps serving and the failure is
logged. Check with `docker logs rfp_cron`.

### Configuration

Everything is set in `deploy/server/.env`, which is gitignored. See
`.env.example` for the full list. The ones that govern the schedule:

| Variable | Default | What it does |
| --- | --- | --- |
| `RFP_SCHEDULE_UTC` | `05:30` | Build time, UTC. 05:30 UTC is 07:30 in Lusaka |
| `RFP_WINDOW_DAYS` | `7` | How many days of notices to include |
| `RFP_RUN_ON_START` | `1` | Build immediately on container start |
| `OPENROUTER_API_KEY` | unset | Optional. Without it, keyword scoring only |

## One-off manual steps

### 1. Create the environment file, required

Nothing runs until this exists.

```
cd <repo>/deploy/server
cp .env.example .env
chmod 600 .env
```

Fill in `RFP_USER` and `RFP_PASSWORD` for the login gate, generate
`RFP_SESSION_SECRET` with
`python3 -c "import secrets; print(secrets.token_hex(32))"`, and set
`RFP_NETWORK` to the Docker network your reverse proxy is on. Add
`OPENROUTER_API_KEY` if you want model-refined scoring.

Then:

```
docker compose up -d
docker logs -f rfp_cron
```

The first build starts immediately and takes a few minutes, most of it waiting
on the sources.

### 2. The shared OHASP certificate is expired, not caused by this work

Found while verifying, and reported here because it affects the other sites
rather than this one. The certificate covering `ohasp`, `cms`, `superset` and
`pgadmin` expired on 6 September 2026, so browsers show a TLS warning on all
four. The site for `rfp.malambomutila.com` has its own separate certificate and
is unaffected.

The cause is in the shared nginx configuration. The port 80 server block that
serves the ACME challenge lists `ohasp`, `cms` and `superset` but not
`pgadmin.malambomutila.com`. Requests for the pgAdmin challenge therefore fall
through to the default server, which redirects them, so that one challenge fails
and Let's Encrypt fails the whole four-domain certificate with it. The renewal
log for 1 September 2026 shows exactly that.

The fix is to add `pgadmin.malambomutila.com` to that `server_name` line in
`/home/mm/srv/ohasp/nginx/nginx.conf`, validate, reload, then rerun the renewal.
This was left alone deliberately: it is someone else's live service and outside
the remit of this work.

## Routine operations

All commands run as `mm` on the server. None need sudo.

```
# Is the site container healthy
docker ps --filter name=rfp_site

# Container logs
docker logs --tail 50 rfp_site

# Serve from the container directly, bypassing the proxy
curl -sI http://127.0.0.1:8090/
curl -s  http://127.0.0.1:8090/healthz

# Restart just this site. Safe: it touches no other container
docker compose -f /home/mm/apps/rfp/docker-compose.yml restart

# Certificate expiry
docker run --rm -v /home/mm/srv/ohasp/nginx/ssl:/etc/letsencrypt \
  certbot/certbot certificates --cert-name rfp.malambomutila.com

# Renew now, rather than waiting for the weekly cron
/home/mm/apps/rfp/renew-cert.sh
```

The certificate renews automatically. `/home/mm/apps/rfp/renew-cert.sh` runs
weekly from `mm`'s crontab at 03:20 on Mondays and logs to
`/home/mm/apps/rfp/renew-cert.log`. It is scoped with `--cert-name` to this one
certificate, and it reloads nginx rather than restarting it. It is separate from
the pre-existing `/home/mm/ssl-renew.sh` on purpose: that script renews every
certificate under `set -euo pipefail`, so a failure on any one of them aborts it
before nginx is reloaded, and a freshly renewed certificate would never be
picked up.

## Rolling back

Pick the smallest step that fixes the problem.

**Bad content, good infrastructure.** Restore a dated copy from the archive
that the scheduler keeps beside the deployment, then force a rebuild only when
you are ready:

```
cd <repo>/deploy/server
cp reports/2026-09-18.html site/index.html    # any archived day
```

To rebuild immediately rather than waiting for the schedule, restart the
scheduler, which builds on start:

```
docker compose restart rfp_cron
docker logs -f rfp_cron
```

The archive in `deploy/server/reports/` is the content history. The
`reports/` directory in the repository is the
archive to pull a previous version from.

**The site container is misbehaving.** Recreate only this stack. This cannot
affect the other sites, because the stack owns one container and no shared
volumes. The proxy resolves the upstream at request time, so no nginx reload is
needed afterwards.

```
docker compose -f /home/mm/apps/rfp/docker-compose.yml up -d --force-recreate
```

**The nginx change needs reverting.** The pristine configuration, as it was
before this work, is at
`/home/mm/srv/ohasp/nginx/nginx.conf.bak-rfp`. Restoring it removes the
`rfp.malambomutila.com` vhost and leaves every other site exactly as it was.
Always validate before reloading, and never restart or recreate the nginx
container, because it serves every site on the host.

```
cp /home/mm/srv/ohasp/nginx/nginx.conf.bak-rfp /home/mm/srv/ohasp/nginx/nginx.conf
docker exec nginx nginx -t && docker exec nginx nginx -s reload
```

**Removing this deployment entirely.** Stop and remove only the `rfp_site`
container, restore the nginx backup as above, and optionally delete
`/home/mm/apps/rfp/`. Remove the cron line referring to
`apps/rfp/renew-cert.sh` with `crontab -e`. Leave the OHASP stack, its
containers, its volumes and its certificates untouched.

```
docker compose -f /home/mm/apps/rfp/docker-compose.yml down
```

That `down` is safe only because it is scoped with `-f` to this stack's own
compose file. Never run `docker compose down` from `/home/mm/srv/ohasp`, which
would take every other service on the host offline.

## Constraints worth remembering

- The user `mm` has no sudo. Anything needing root must go through a container
  that mounts the relevant directory, which is how certbot writes to the
  root-owned `letsencrypt` directories.
- Only the pre-existing nginx container may bind ports 80 and 443. Any new
  service must bind a high port on `127.0.0.1` and be proxied.
- The certificate directories under
  `/home/mm/srv/ohasp/nginx/ssl/live/` are root-owned and cannot be read from
  the host shell as `mm`. Read them through a container instead.

## Login gate

The report on `rfp.malambomutila.com` sits behind a shared login. The GitHub
Pages mirror does not, because static Pages hosting cannot authenticate.

Two containers serve the site, both defined in
`/home/mm/apps/rfp/docker-compose.yml`:

| Container | Role |
|---|---|
| `rfp_site` | nginx, serves the static report and enforces the gate |
| `rfp_auth` | Python standard library service, validates the login form |

nginx cannot check a form POST on its own, so `rfp_site` issues an
`auth_request` subrequest to `rfp_auth` for every content request. `rfp_auth`
answers 204 when the session cookie is valid and 401 when it is not, and a 401
redirects the visitor to `/login`. The cookie is stateless: it carries an
expiry and an HMAC-SHA256 signature over it, so there is no session store.

### Credentials

They live in `/home/mm/apps/rfp/.env` on the server, mode 600, and are never
committed, because this repository is public. `deploy/server/.env.example`
shows the shape. To change them:

```
nano /home/mm/apps/rfp/.env
cd /home/mm/apps/rfp && docker compose up -d rfp_auth
```

Generate a fresh signing key with
`python3 -c "import secrets; print(secrets.token_hex(32))"`. Changing it signs
everyone out, which is the quickest way to revoke access.

### Scope

This is a courtesy gate over public tender notices, with one shared account
rather than per-user logins. It is not an access control for anything
sensitive. nginx rate limits `/login` to 10 requests a minute per address, and
the service adds a short delay on a failed attempt.

### Rolling it back

The pre-login configuration is kept on the server:

```
cd /home/mm/apps/rfp
cp nginx-site.conf.bak-prelogin nginx-site.conf
cp docker-compose.yml.bak-prelogin docker-compose.yml
docker compose up -d --remove-orphans
docker exec rfp_site nginx -s reload
```
