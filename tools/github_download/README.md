# Github download — Google Drive transfer

This branch adds a manual GitHub Actions workflow that downloads a URL on a GitHub-hosted runner, extracts supported archives, and uploads the extracted files to a single Google Drive folder named **Github download**. The original archive is not uploaded; it stays in temporary runner storage until the extracted contents are uploaded and verified.

## Current implementation

- Workflow: `.github/workflows/download-to-drive.yml`
- Transfer engine: `tools/github_download/transfer.py`
- HTTP control worker source: `cloudflare/worker.js`
- Manual GitHub trigger: Actions → **Download to Google Drive** → **Run workflow**
- The Cloudflare worker source is not deployed yet. HTTP Shortcuts will not work until deployment and secret configuration are completed.
- No end-to-end transfer has yet been tested against the user's Drive account.

## Google Drive OAuth secrets

In repository **Settings → Secrets and variables → Actions**, add:

- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REFRESH_TOKEN`

Use a Google OAuth client and a refresh token authorized for the Google account that should receive the files. Enable the Google Drive API and authorize scope `https://www.googleapis.com/auth/drive`. Never commit these values or paste them into a chat or issue.

## Cloudflare Worker secrets

After merging the workflow to `main` and deploying `cloudflare/worker.js` as a Worker, configure these Worker secrets:

- `GH_TOKEN`: a GitHub fine-grained personal access token limited to this repository, with Actions read/write permission.
- `API_KEY`: a long random secret used as the Bearer token in HTTP Shortcuts.

Do not publish either secret. The worker exposes:
- `POST /start` with JSON `{"url":"https://...","filename":"optional-name.zip"}`
- `GET /status` for recent workflow runs
- `POST /status` with JSON `{"run_id":123}` for detailed job steps
- `GET /health` for a basic health check

For authenticated requests, set header `Authorization: Bearer YOUR_API_KEY`. The worker is configured to dispatch workflow `download-to-drive.yml` on branch `main`; change that only for a deliberate test.

## Run a small test

1. Configure the three Google OAuth secrets in GitHub.
2. Before deploying the worker, run the workflow manually from the Actions tab with a small, non-sensitive direct HTTPS file URL.
3. Confirm that `Github download` appears in Drive and the file opens correctly.
4. After that succeeds, deploy/configure the Worker and create the HTTP Shortcuts actions.
5. Test one ZIP archive before attempting a large file.

The first successful run creates `Github download` in the Drive root if it does not exist. For supported archives, it creates a subfolder named after the archive and uploads only the extracted files, preserving nested directories. Existing names receive a numeric suffix rather than overwriting files.

## Supported archive types

ZIP, TAR, TAR.GZ/TGZ, TAR.BZ2/TBZ2, TAR.XZ/TXZ, standalone GZ, 7Z, and RAR (depending on support from the runner's installed tools). Encrypted archives and archives requiring a password are not supported in this first version.

## Limitations

- Login-protected pages, short-lived links, and websites requiring browser cookies or human verification may not work. Prefer direct download URLs.
- HTTP Range is used to continue an interrupted download when the source supports it; otherwise it may restart.
- Hosted runners have finite disk, runtime, and bandwidth. A 2 GB transfer is a target to test, not a guarantee.
- Extraction is capped at 12 GiB expanded data and 100,000 files.
- GitHub Actions logs provide phase and transfer progress. The worker currently exposes workflow/run status and job steps, but does not provide push notifications or durable cross-run resume state.
- The repository is public, so code is visible. Keep all OAuth and API secrets in encrypted secret stores.

## Security

The downloader accepts only HTTP/HTTPS URLs with public IP addresses and checks redirect destinations. Treat source URLs as untrusted. Use this only for files you are authorized to download.
