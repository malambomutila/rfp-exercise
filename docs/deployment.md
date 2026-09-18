# Deployment guide

This guide takes the repository from a fresh clone to a live daily report at
<https://rfp.malambomutila.com>. It assumes you have admin rights on the
GitHub repository and access to the Namecheap account that holds
`malambomutila.com`. No prior knowledge of this repository is needed.

The tool itself needs no installation. It uses the Python standard library
only, so there is no `requirements.txt` and no `pip install` step anywhere,
including in the GitHub Actions workflow. If you want to check the build
locally before touching any settings, run `python src/main.py` with Python
3.12 or later.

## 1. Enable GitHub Pages

1. Open the repository on GitHub and go to **Settings** then **Pages**.
2. Under **Build and deployment**, set **Source** to **GitHub Actions**.
3. There is nothing to save. The setting applies immediately.

This step matters: with the source left on the default branch option, the
`deploy-pages` step in the workflow fails with a permissions error rather than
an obvious message about Pages not being configured.

## 2. Add the OPENROUTER_API_KEY secret

The scorer works without this key. When the key is present it also asks a
language model to sharpen the wording of each rationale. Without it, the
deterministic keyword scorer runs on its own and the report still builds.

1. Go to **Settings** then **Secrets and variables** then **Actions**.
2. Select **New repository secret**.
3. Name it exactly `OPENROUTER_API_KEY`.
4. Paste the key as the value, then select **Add secret**.

Two rules apply here. The name must match exactly, because the workflow reads
`secrets.OPENROUTER_API_KEY` and GitHub secret names are case sensitive. And
the key belongs only in this settings page: never commit it to a file in the
repository, and never paste it into an issue or a pull request.

## 3. Trigger the first run

The schedule will not fire until the next cron tick, so run it by hand once to
confirm the whole chain works.

1. Go to the **Actions** tab.
2. Select **Daily RFP report** in the left hand list.
3. Select **Run workflow**, keep the branch as `main`, then confirm.

The run takes a couple of minutes. It has two jobs: `build`, which fetches the
notices, scores them, renders the site and commits a dated copy to `reports/`,
and `deploy`, which publishes the site to Pages.

## 4. Find the live URL

Once the `deploy` job finishes, the URL appears in three places:

- On the workflow run summary page, as a link under the **deploy** job.
- In **Settings** then **Pages**, at the top of the page.
- In the repository sidebar on the main page, under **Environments**, next to
  `github-pages`.

Before DNS is configured the URL is
`https://malambomutila.github.io/rfp-exercise/`. After the DNS section below
is done it becomes `https://rfp.malambomutila.com`.

## 5. Change the schedule

The schedule lives in one place: the `cron` line near the top of
`.github/workflows/daily-report.yml`.

```yaml
on:
  schedule:
    - cron: '30 5 * * *'
```

The five fields are minute, hour, day of month, month, day of week. GitHub
runs cron in UTC only and ignores any timezone setting, so convert first.
`30 5` means 05:30 UTC, which is 07:30 in Lusaka and 08:30 in Nairobi, chosen
so the report is waiting before the working day starts. To move the report an
hour earlier in East Africa, use `30 4`. To run it twice a day, add a second
`- cron:` line.

Two things to know. Scheduled runs on GitHub's shared runners are often a few
minutes late at busy times, which does not matter for a daily digest. And a
schedule change only takes effect once it is merged into the default branch.

## 6. DNS at Namecheap

Create one record on `malambomutila.com` so that `rfp.malambomutila.com`
serves the Pages site.

| Type | Host | Value | TTL |
| --- | --- | --- | --- |
| CNAME Record | `rfp` | `malambomutila.github.io` | Automatic |

Steps in Namecheap: sign in, open **Domain List**, select **Manage** next to
`malambomutila.com`, open the **Advanced DNS** tab, select **Add New Record**,
then enter the values in the table and save.

Three notes on the values:

- The host is `rfp` on its own, not the full `rfp.malambomutila.com`.
  Namecheap appends the domain for you, and typing the full name produces
  `rfp.malambomutila.com.malambomutila.com`.
- The target ends in `malambomutila.github.io`, the GitHub user site, not the
  repository path. A CNAME cannot point at a path.
- Leave TTL on **Automatic**. There is no benefit to a custom value here.

Then tell GitHub about the domain. The repository needs a file named `CNAME`
in its root containing exactly one line:

```
rfp.malambomutila.com
```

Setting the custom domain in **Settings** then **Pages** creates that file for
you. If the file is added by hand instead, keep it in the root of the branch
that Pages serves, and note that removing it reverts the site to the
`github.io` address.

Finally, wait for TLS. Once the CNAME record resolves publicly, GitHub
requests a Let's Encrypt certificate for the domain automatically. This
usually takes a few minutes and can take up to 24 hours. Until it completes,
**Settings** then **Pages** shows a certificate pending notice and the
**Enforce HTTPS** tick box is greyed out. Once the tick box becomes available,
switch it on. Nothing needs to be done in Namecheap for the certificate.

## Troubleshooting the first run

| Symptom | Cause | Fix |
| --- | --- | --- |
| `deploy` job fails with a Pages error | Pages source is not set to GitHub Actions | Redo step 1, then re-run the failed job |
| Build fails on `Locate the generated site` | `src/main.py` did not write `site/index.html` | Run `python src/main.py` locally and check where it writes |
| Report builds but rationales look generic | The secret is missing or invalid | Confirm the name in step 2, then re-run |
| Browser shows a certificate warning | TLS certificate not issued yet | Wait for the CNAME to resolve, then enable Enforce HTTPS |
