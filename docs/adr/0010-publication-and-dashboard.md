# ADR 0010: Publication targets and dashboard

Status: accepted

## Decision

- **Cloudflare R2** holds the published gold tables (`data/<table>/...`), the contracts (`contracts/`) and the dataset card (`README.md`). `publish-r2` syncs by file checksum against `_publish/manifest.json` in the bucket: unchanged files are skipped, changed files are uploaded, the manifest is written last, and `--prune` deletes files that no longer exist. Access uses the S3 API with keys from the environment.
- **Hugging Face** receives the same tree as a dataset repository (`HF_REPO_ID`, default `hsilvosa/entsoe-observatory`) in one commit. The card has YAML front matter with one config per table.
- **Dashboard** is a static site (plain HTML, CSS and JavaScript, no external requests) generated into `STORAGE_ROOT/site` from small aggregates of gold. It can be served by Cloudflare Pages.

## Consequences

The R2 free tier (10 GB) holds the dataset about 25 times over. Credentials never enter the repository, logs or manifests. The licence field of the card is `other`: ENTSO-E allows re-use with a mention of the source and without suggesting endorsement, Open-Meteo is CC BY 4.0, and the ECB allows re-use with attribution. Check ENTSO-E's list of data open for free re-use before making the dataset public.
