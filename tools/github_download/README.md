# Github download — Google Drive transfer

This adds a manual GitHub Actions workflow that downloads a URL on a GitHub-hosted runner, extracts supported archives, and uploads the extracted files to a single Google Drive folder named **Github download**. The original archive is not uploaded; it is kept only in the temporary runner workspace until the extracted contents have been uploaded and verified.

## Current status

- Workflow file: `.github/workflows/download-to-drive.yml`
- Transfer engine: `tools/github_download/transfer.py`
- Trigger: GitHub Actions → **Download to Google Drive** → **Run workflow**
- This is the first implementation; it is not yet connected to HTTP Shortcuts or a Cloudflare status endpoint.
- It has not been end-to-end tested against the user's Drive account yet.

## Required GitHub Actions secrets

In the repository, open **Settings → Secrets and variables → Actions → New repository secret** and add:

- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REFRESH_TOKEN`

Use a Google OAuth client and a refresh token authorized for the same Google account that should receive the files. Enable the Google Drive API in the associated Google Cloud project and grant the OAuth consent screen the Drive scope `https://www.googleapis.com/auth/drive`. Never commit these values into this repository or paste them into an issue or chat.

## Run a test

1. Add the three secrets above.
2. Open **Actions** and select **Download to Google Drive**.
3. Choose **Run workflow** and enter a direct, publicly accessible HTTPS download URL.
4. For a first test, use a small non-sensitive file.
5. Confirm that `Github download` appears in Drive and the file opens correctly.

The first successful run creates `Github download` in the Drive root if it does not already exist. For supported archives, it creates a subfolder named after the archive and uploads only the extracted files, preserving nested directories. Existing names receive a numeric suffix rather than overwriting files.

## Supported archive types

ZIP, TAR, TAR.GZ/TGZ, TAR.BZ2/TBZ2, TAR.XZ/TXZ, standalone GZ, 7Z, and RAR (depending on archive features supported by the runner's installed tools). Encrypted archives and formats requiring a password are not supported by this first version.

## Current limitations

- Login-protected pages, short-lived links, and websites requiring browser cookies or human verification may not work. Prefer a direct download URL.
- HTTP Range is used to continue an interrupted download when the source supports it; otherwise the download may restart.
- The hosted runner has finite disk, runtime, and bandwidth. A 2 GB file is a target to test, not a guarantee.
- Extraction is capped at 12 GiB expanded data and 100,000 files to limit archive-bomb risk.
- GitHub Actions logs show phases and transfer progress. A phone-friendly HTTP status endpoint, push notifications, and persistent cross-run resume state are not implemented yet.
- Do not place highly sensitive files in a public repository. The code is public; secrets should remain in GitHub's encrypted secret store.

## Security

The downloader accepts only HTTP/HTTPS URLs with public IP addresses and checks redirect destinations. Treat source URLs as untrusted. Review the code before using it for important data. Use this only for files you are authorized to download.
