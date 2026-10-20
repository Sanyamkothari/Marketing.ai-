# Pilot deployment kit: one client, one small VM

This is the supervised-pilot way to run Marketing AI for **one client**: one VM, one data volume, one
`.env`, one domain, one AI key per client. It is the path every v1 journey was walked on: the repo's
`api` image with the local backends (SQLite, files on disk, training inside the API process), Caddy in
front for automatic HTTPS. It is not the AWS/CDK stack (never deployed; see `docs/AWS_DEPLOYMENT.md`).

Inside one deployment there is no isolation between clients (users are deployment-wide, models share
one champion per use case, `GET /datasets` and `/runs/{id}` are addressed by id alone;
`docs/CLIENT_ISOLATION.md`). **Never put two clients on one VM.** `MARKETING_AI_CLIENT_ID` makes the API
refuse any other client id with 403, which is a guard rail, not a second tenant.

| File | What it is |
|---|---|
| `docker-compose.yml` | the `api` service (the repo's image, built here), Caddy, the data volume, restart policy, health check, log rotation |
| `Caddyfile` | HTTPS for `$DOMAIN`, proxy to the api, 2 GB upload limit, timeouts |
| `.env.example` | every variable, commented. Copy to `.env`, which holds the secrets and is gitignored |
| `smoke_test.sh` | curl checks: health, UI served, data routes refused without sign-in, security headers, CORS |
| `backup.sh`, `restore.sh` | consistent copy of the data volume to a dated tarball and off the VM; restore with checks |

Everything is run **from this directory** (`deploy/pilot`) of a checkout of the repo, on the VM.

## 1. Create the VM

- Ubuntu 22.04 or 24.04 LTS, **4 vCPU, 16 GB RAM, 100 GB disk**, in the client's data-residency region
  (Mumbai for Indian clients). Training (AutoGluon) runs inside the API process: 8 GB is tight.
- A public IPv4 address. Security group / firewall: allow **22** (ssh, ideally from the office only),
  **80** and **443** (80 is needed for the certificate and the redirect). Never open 8000: the compose file
  does not publish it, and nothing else may.
- Create a non-root sudo user and use ssh keys. Turn on the provider's automatic disk snapshots (weekly).

## 2. Install Docker

```bash
sudo apt-get update && sudo apt-get install -y git curl cron
curl -fsSL https://get.docker.com | sudo sh        # Docker Engine + the compose plugin
sudo usermod -aG docker "$USER"                    # log out and in again afterwards
docker compose version
```

For the off-VM backup, also install the tool for your target: `sudo apt-get install -y rclone`, or use
`scp` (already there) with an ssh key.

## 3. DNS

Create an **A record** `pilot-<client>.<your domain>` pointing at the VM's public IP, and wait until
`dig +short pilot-<client>.<your domain>` returns it. Caddy asks Let's Encrypt for the certificate at
first start; if DNS is not ready it retries, and too many failures get you rate limited for an hour.

## 4. Copy the files and set `.env`

```bash
sudo mkdir -p /opt/marketing-ai && sudo chown "$USER" /opt/marketing-ai
git clone --branch main <repo-url> /opt/marketing-ai      # always --branch main: the default branch is not main
cd /opt/marketing-ai/deploy/pilot
cp .env.example .env && chmod 600 .env
```

Generate the two secrets and **store both in the team's password manager now, off this VM**:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"                       # MARKETING_AI_PRIVACY_SALT
python3 -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"   # MARKETING_AI_CONNECTIONS_KEY (a Fernet key is 32 random bytes)
```

Edit `.env` (every variable is explained in it): `DOMAIN`, `ACME_EMAIL`, `IMAGE_TAG`,
`MARKETING_AI_CLIENT_ID` (`c_<client>_1`), `MARKETING_AI_CORS_ORIGINS` (`https://` + the domain, exactly),
the two secrets, `BACKUP_TARGET`. Leave `MARKETING_AI_ENV=prod` and `MARKETING_AI_AUTH_MODE=local` alone.

- Losing `PRIVACY_SALT` orphans every consent, erasure and audit hash, and the API refuses a changed value.
- Losing `CONNECTIONS_KEY` means typing the AI key in again.
- `docker compose config` prints the secrets in clear text: do not paste its output anywhere.
- `docker compose` refuses to start when a required variable is empty and says which one.

## 5. Build and start

```bash
docker compose build api        # builds marketing-ai:$IMAGE_TAG from this checkout; takes several minutes
docker compose up -d
docker compose ps               # api becomes "healthy" within ~2 minutes (AutoGluon's import is slow); caddy starts after it
docker compose logs -f api      # JSON lines; Ctrl+C leaves it running
```

## 6. Create the first user

Nobody can create the first user through the API, so use the command line, inside the running container
(it asks for the password twice, 12 characters or more, and never takes it as an argument):

```bash
docker compose exec api python -m scripts.create_user --username lead --role admin --role analyst --role approver
```

Roles are a set: Admin does not include Analyst or Approver, and only an Analyst can register the client
in step 8. So the pilot lead gets all three. Then, signed in at `https://<domain>/ui/`, **Admin > Users**
adds everyone else. Suggested: Minfy data scientist = analyst; **client sponsor = approver** (a different
person from whoever trains: the product refuses self-approval); client analyst = analyst; others = viewer.
Downloading customer-level rows (`scores.csv`, copy messages) needs Analyst. If the last Admin is locked
out, the same command (new username) is the recovery.

Sign-in is `auth_mode=local`: PBKDF2 passwords, 8-hour sessions, 5 wrong passwords lock an account for
15 minutes (20 per client address). **There is no MFA and no SSO**; say so to the client and keep the user
list small with strong passwords. The lock-out counters live in memory and reset when the API restarts
(every upgrade and every nightly backup stops it for about a minute), so a restart gives a guesser at most
5 more tries per account. This is accepted for the pilot; persisting the counters is a change to
`engine/access/throttle.py`, not a deployment setting.

## 7. Smoke test

```bash
./smoke_test.sh                 # uses DOMAIN from .env; or ./smoke_test.sh https://pilot-acme.example.com
```

It sends no credentials and changes nothing. It checks: `/healthz` says ok; the UI and its script are
served; `/datasets`, `/clients`, `/auth/me` answer **401** (a 503 means sign-in is off, a 200 means the data
is open); the security headers (nosniff, frame-ancestors, referrer-policy, HSTS) are on answers and
refusals; another origin gets no CORS permission and the configured one does; `http://` redirects to
`https://`. Any failure exits 1 and says which. (`make pilot-smoke URL=https://<domain>` runs the same script from a checkout with `make`.)

## 8. Register the client and connect the AI key

1. Sign in as the lead (an Analyst). Add the client with its real name and industry in the UI (or
   `POST /clients`). Because `MARKETING_AI_CLIENT_ID` is set, it is registered under exactly that id and
   no second client can be made. Check `GET /clients` lists one client with that id.
2. Open the **AI service connection** screen, choose the provider (Anthropic, OpenAI, OpenRouter, Hugging
   Face, custom, or Bedrock), paste the key, run **Test**. The key is stored encrypted with
   `MARKETING_AI_CONNECTIONS_KEY` and never returned or logged. The Deliverable slot (root-cause text,
   campaign copy) follows the Product slot unless set separately. Use a **pilot-only key with a spend limit**
   set at the provider, and rotate it when the pilot ends. Leave `ALLOW_FAKE_AI` unset: with no AI
   connected, AI screens answer 409 `AI_NOT_CONNECTED` rather than pretending.
3. Bedrock needs no key but an IAM role on the VM and model access in the region.

## 9. Backups (cron)

Everything lives in the data volume: `registry.db`, `platform.db`, `clients.db`, `runs/`, `models/`,
`uploads/`, `ai_service/`. Set `BACKUP_TARGET` in `.env`:

- `rclone:<remote>:<path>`: run `rclone config` once as the user that will run cron. For an encrypted
  copy create a `crypt` remote on top of your bucket and use that remote's name.
- `scp:<user>@<host>:<dir>`: a key-based ssh login that needs no password prompt (test with `ssh <user>@<host>`).

```bash
./backup.sh --local-only     # rehearsal: stops the API ~1 min, writes backups/marketing-ai-<client>-<date>.tgz
./backup.sh                  # the real thing: the same, then copies tarball + .sha256 off the VM, prunes
```

It stops the API (copying a live SQLite file can capture half a transaction), tars the volume, starts the
API again at once, checks the tarball, then uploads and deletes copies older than `BACKUP_KEEP_DAYS`
(14) locally and at the target. It exits non-zero if anything failed, and exits 1 rather than quietly
keeping the only copy on the same VM when `BACKUP_TARGET` is empty. A training run in progress when the
API stops is lost, hence the night time. Nightly at 02:00:

```bash
mkdir -p backups   # the log lives here, so it must exist before cron first runs
( crontab -l 2>/dev/null; echo '0 2 * * * cd /opt/marketing-ai/deploy/pilot && ./backup.sh >> backups/backup.log 2>&1' ) | crontab -
```

Cron mails or logs nothing on its own: check `backups/backup.log` (in the same directory; the cron user owns it, `/var/log` it does not) weekly, and have the
uptime service (step 10) alert on a stale backup if it can. The tarball holds the client's data but not
the secrets, which you already keep off the VM.

**Restore** (also the disaster-recovery path: a new VM, the same `.env` from the password manager,
`docker compose build api`, then):

```bash
rclone copy <remote>:<path>/marketing-ai-<client>-<date>.tgz backups/     # or scp; also fetch the .sha256
rclone copy <remote>:<path>/marketing-ai-<client>-<date>.tgz.sha256 backups/
./restore.sh backups/marketing-ai-<client>-<date>.tgz
```

It checks the checksum, gzip, the archive and that it holds a database; asks before replacing anything;
writes a `pre-restore-*.tgz` safety copy of what was there (pruned with the same `BACKUP_KEEP_DAYS` as the nightly copies; delete it sooner once the restore is confirmed); extracts; runs `PRAGMA integrity_check` on every
database (on a failure it leaves the API stopped and prints the command to undo); starts the API and waits
for it to be healthy. Then run `./smoke_test.sh`. Anything erased on request (DPDP) after the backup was
taken is back and must be erased again by hand: the audit log lists the requests. There is no
re-apply-by-hash command yet. **Rehearse a restore on a second VM once, with real timing, before go-live.**

## 10. Operating it

- **Logs:** `docker compose logs api` (JSON lines, rotated at 50 MB x 5). The audit log is in the app
  (**Admin > Audit log**, CSV download): **export it weekly**, it is append-only inside the database with no
  Object Lock.
- **Uptime:** point any uptime service at `https://<domain>/healthz`, email on failure, a named owner.
  Alert on disk use above 80% (`df -h`, `docker system df`).
- **Alerts** (drift, failed jobs) are log lines here (`alert_backend=log`): someone reads the Monitoring
  screen weekly (`docs/pilot/PLAYBOOK.md`).
- **Retention:** `docker compose exec api python -m scripts.run_retention` is a dry run; add `--apply` to delete.
- **Certificates** renew by themselves; the `caddy_data` volume keeps them.

## 11. Upgrades

```bash
cd /opt/marketing-ai/deploy/pilot
./backup.sh                                   # always first
git -C ../.. pull --ff-only origin main
nano .env                                     # set IMAGE_TAG to a NEW value, e.g. the git short sha
docker image ls marketing-ai                  # the previous tag must be listed: it is your rollback
docker compose build api
docker compose up -d                          # recreates api on the new image (~1 min of downtime)
./smoke_test.sh
```

## 12. Rollback

The previous image is still on the VM (do not `docker image prune -a`; `docker image ls marketing-ai` lists them).

```bash
nano .env                                     # IMAGE_TAG back to the previous value
docker compose up -d --no-build api           # --no-build: a missing old image fails loudly instead of building the new code under the old tag
./smoke_test.sh
```

If the new version had already changed the data in a way the old one cannot read (the release notes or
`CHANGELOG-prototype.md` say when), restore the backup taken at the start of the upgrade with
`./restore.sh` instead. SQLite needs no migration step (`migrate` is only for Postgres).

## Before go-live

Checked on a laptop (no Docker daemon was available while this kit was written): the compose file renders
with `docker compose config`; the Caddyfile validates and formats clean with Caddy 2.8.4; Caddy replaced a
forged `X-Forwarded-For`, redirected http to https and proxied to the API with a locally-issued certificate;
the API ran as `prod` + `local` auth with these variables and `smoke_test.sh` passed against it, directly
and through Caddy, and failed when pointed at a wrong origin or at nothing; the first user, `POST /clients`
under `CLIENT_ID`, and the 403 on another client id were exercised; `backup.sh` and `restore.sh` were run
against a stand-in for the `docker compose` calls and shellcheck is clean.

**Not verified, so do these on the real VM:**

- [ ] `docker compose build api` succeeds on the VM and `docker image ls marketing-ai` shows under 4 GiB (the repo's size rule); the
      image has never been built where this was written, nor the `libgomp1` layer.
- [ ] `docker compose up -d`: api becomes `healthy` (the 120 s start period is a chosen number: time it and
      note it here), caddy starts after it, the volume is writable by uid 10001.
- [ ] A real **certificate** is issued for the real domain (DNS, ports 80/443 open) and
      `./smoke_test.sh` passes against `https://<domain>` without `INSECURE=1`.
- [ ] `docker compose ps` shows no `0.0.0.0:8000`; from outside, `curl http://<ip>:8000` fails.
- [ ] `backup.sh` run for real: the `docker compose run --entrypoint tar` pipe writes a good tarball, the
      upload reaches the target, `restore.sh` on a **second VM** brings the data back and the smoke test passes.
      Time it. Cron has run once and `backups/backup.log` ends with `Backup done`.
- [ ] Uptime alert tested (stop the api, get the email); a named person owns it. Disk alert set.
- [ ] `MARKETING_AI_ENV=prod`, `AUTH_MODE=local` confirmed: an unauthenticated call returns 401, not data.
- [ ] First admin's temporary password replaced; one Approver who is not the trainer.
- [ ] Both secrets are in the password manager and a second person knows where. AI key saved, **Test** green,
      spend-capped at the provider.
- [ ] `CLIENT_ID` equals the registered client's id and `GET /clients` lists one client.
- [ ] `configs/privacy.yaml` is the one the client agreed to (consent and erasure routes read it); the consent
      ledger is imported if scoring will contact people.
- [ ] Sample file through upload, train, approve, score and download, **as each role**. Memory and disk during a
      training run on a file of the client's real size watched once (`docker stats`).
- [ ] The Data Doctor's AI gate against the live AI (exit 2 = no AI connected). The saved AI connection is
      inside the data volume, so run it in the image, with the checkout's `tests/` fixtures (the image does not
      contain them) mounted read-only:
      `docker compose run --rm --no-deps -v "$PWD/../../tests:/app/tests:ro" api python -m scripts.plan_o_benchmark --model product --k 5 --parts tools,ai,multi,extended --out /var/lib/marketing-ai/reports/doctor_ai_gate`
      (this stops nothing, but a training run in the API at the same moment will slow both). Or run
      `make doctor-ai-gate` on a dev machine that has the same provider key connected.
- [ ] Pilot playbook success and exit criteria signed (`docs/pilot/PLAYBOOK.md`); the client's data protection
      contact approved the data request; compliance has seen the DPDP section (the repo says its controls are
      not a legal opinion); a processing agreement covers the AI provider.
- [ ] Told to the client in writing: no MFA, no SSO; the product sends no email/SMS/WhatsApp (the client runs
      the campaign); erasure cannot reach the backups.
