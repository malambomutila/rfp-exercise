# Server deployment: rfp.malambomutila.com

This document covers the second of the two delivery paths for the generated RFP
report. It is written for someone who has never seen the server.

- **Path A, GitHub Pages:** https://malambomutila.github.io/rfp-exercise/, built
  and published by `.github/workflows/daily-report.yml`. Not covered here.
- **Path B, own server:** https://rfp.malambomutila.com, built and deployed by
  `.github/workflows/deploy-server.yml`. This document.

The two paths are deliberately independent. They share the build command but
nothing else, so a failure in one does not take the other down.

One thing can break that independence, so it is worth knowing about. If a file
named `CNAME` is placed in the repository root, `src/main.py` copies it into the
build output, and the daily workflow uploads that output as the GitHub Pages
artifact. GitHub reads it as a custom domain for Pages and then redirects the
`github.io` URL to whatever it names. A `CNAME` containing
`rfp.malambomutila.com` would therefore point Pages at a host name that this
server already answers for, collapsing the two paths into one and taking the
`github.io` URL down. No such file exists today, so the copy step is a no-op,
but the hazard returns the moment one is added. If a custom domain on Pages is
ever wanted, it needs a different host name from this one.

## What runs where

### The server

Host `198.54.121.71`, Ubuntu, reached as user `mm` over SSH. The user `mm` has
no passwordless sudo, so everything below is done through Docker and through
files that `mm` owns. Nothing here requires root on the host.

This is a shared production host. It also runs the OHASP stack: a Next.js app,
Directus, Superset, Airflow, pgAdmin and several Postgres databases. None of
that was modified.

| Component | Location | Purpose |
|---|---|---|
| Static file container `rfp_site` | `/home/mm/apps/rfp/docker-compose.yml` | Serves the contents of `site/` over plain HTTP inside the Docker network |
| Site content | `/home/mm/apps/rfp/site/` | `index.html` and `data.json`, overwritten by the pipeline |
| Inner server config | `/home/mm/apps/rfp/nginx-site.conf` | Static file serving rules and a `/healthz` endpoint |
| Certificate renewal | `/home/mm/apps/rfp/renew-cert.sh` | Weekly cron, renews this one certificate and reloads nginx |
| Front-facing reverse proxy | `/home/mm/srv/ohasp/nginx/nginx.conf` | Pre-existing. Owns ports 80 and 443 for every site on the host. A vhost for `rfp.malambomutila.com` was added to it |
| TLS certificate | `/home/mm/srv/ohasp/nginx/ssl/live/rfp.malambomutila.com/` | Let's Encrypt, single domain, separate from the shared OHASP certificate |

Copies of the server-side files are kept in this repository under
`deploy/server/` so the setup is reproducible and reviewable:

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

`.github/workflows/deploy-server.yml` runs on GitHub-hosted runners and fires on
three triggers:

1. **Push to `main`** touching `src/**`, `data/**`, `deploy/**` or the workflow
   itself. Documentation-only commits do not redeploy.
2. **Manual dispatch**, from the Actions tab. Use this for the first run.
3. **Completion of the "Daily RFP report" workflow**, so the server refreshes
   after each scheduled daily build. The schedule lives in that workflow only,
   so the two delivery paths cannot drift apart. The job is skipped unless that
   run succeeded, because deploying the output of a failed build would be worse
   than serving yesterday's report.

The steps are: check out, set up Python 3.12 to match the server, run
`python3 src/main.py --days 7 --out site`, then rsync `site/` to
`/home/mm/apps/rfp/site/` over SSH, then fetch the live URL and fail the run if
it does not return 200.

Two safety details are worth knowing. First, rsync runs with `--delete` so stale
pages do not linger, and a gate before it aborts the run if `site/index.html` is
missing or empty. Without that gate a broken build would wipe the live site;
with it, the server simply keeps serving the previous copy. Second, the SSH host
key for the server is pinned in `known_hosts` inside the workflow and
`StrictHostKeyChecking` is left on, so the deployment cannot be silently
intercepted. Host public keys are not secret, so pinning it in the repository is
safe.

A concurrency group prevents two deployments from interleaving rsync writes.
In-flight runs are allowed to finish rather than being cancelled.

### Credentials

One repository secret, `SERVER_SSH_KEY`, holds the private half of a keypair
generated on the server for this repository alone. It is not anyone's personal
key. In the server's `~/.ssh/authorized_keys` it carries the `restrict` option,
which denies port forwarding, agent forwarding, X11 forwarding and PTY
allocation. The key is written to the runner from the secret, used, and deleted
in a step that runs even if an earlier step failed.

## One-off manual steps

### 1. Set the repository secret, required

The pipeline cannot deploy until this is done. The private key was saved on the
local machine during setup; the exact path was given in the setup report and is
deliberately not recorded here.

```
gh secret set SERVER_SSH_KEY --repo malambomutila/rfp-exercise < <path to the private key>
```

Then trigger the first run:

```
gh workflow run deploy-server.yml --repo malambomutila/rfp-exercise
```

If the key ever needs replacing, generate a fresh one on the server, add the new
public key to `~/.ssh/authorized_keys`, update the secret, confirm a run
succeeds, and only then remove the old public key.

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

**Bad content, good infrastructure.** Redeploy a known-good build. Either rerun
the workflow from a commit that worked, from the Actions tab, or copy the
content up by hand:

```
rsync -rlptvz --delete -e "ssh -i <path to the deploy key>" \
  ./site/ mm@198.54.121.71:/home/mm/apps/rfp/site/
```

There is no automatic content history on the server, only the current copy. The
`reports/` directory in the repository and the GitHub Pages history are the
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
