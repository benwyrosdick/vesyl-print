# Device keys (OTA + Tailscale)

## OTA signing

**Public key** (committed, shipped on devices):

```
keys/update_public.pem
```

**Private key** (never commit): store as GitHub Actions secret **`UPDATE_PRIVATE_KEY`**
(full PEM, including `BEGIN`/`END` lines). Used by `.github/workflows/release.yml`.

## Tailscale auth key (factory)

```
keys/tailscale.key
```

- **Never commit** (gitignored). Used only by `setup.sh` on first provision.
- Contents: a **one-time** Tailscale auth key (single line).
- `setup.sh` installs Tailscale (if needed) and runs:

```bash
tailscale up \
  --hostname="$(hostname)" \
  --auth-key="$(cat keys/tailscale.key)"
```

- On **successful** join, `setup.sh` **deletes** the key file (one-time use).
  On failure the file is left so you can retry.
- After a full factory setup succeeds, `setup.sh` also **removes the entire
  source checkout** used to provision (app runs from `/opt/vesyl-print/current`).
  Lab: `SKIP_SOURCE_CLEANUP=1 sudo ./setup.sh`.
- Not copied into `/opt/vesyl-print/releases/*` (OTA slots).
- Skip Tailscale: `SKIP_TAILSCALE=1 sudo ./setup.sh`
- Override path: `TAILSCALE_AUTH_KEY_FILE=/path/to.key`

## Generate a new key pair

```bash
openssl genpkey -algorithm Ed25519 -out update_private.pem
openssl pkey -in update_private.pem -pubout -out keys/update_public.pem
# Add update_private.pem contents to GH secret UPDATE_PRIVATE_KEY, then delete local private file
```

## Build a release locally

```bash
UPDATE_PRIVATE_KEY_FILE=./update_private.pem ./scripts/build-release.sh 0.4.0
# artifacts in dist/
gh release create v0.4.0 dist/* --generate-notes
```

Or push a tag and let CI do it:

```bash
git tag v0.4.0
git push origin v0.4.0
```

## Canonical signature

`scripts/build-release.sh` signs compact JSON of the manifest **without** the
`signature` field (`sort_keys=True`, separators `,` / `:`). Devices verify the
same form in `update.ReleaseManifest.canonical_bytes()`.

Ship `update_public.pem` on appliances (this directory or
`/etc/vesyl-print/keys/update_public.pem` via `setup.sh`).
