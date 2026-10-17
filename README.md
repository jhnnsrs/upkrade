# updates

A minimal [Expo Updates protocol v1](https://docs.expo.dev/technical-specs/expo-updates-1/)
server for arkitektio/pokket's over-the-air updates. It replaces `eas update`; EAS Build and
EAS credentials stay. It serves `https://updates.arkitekt.live`.

## How it works

Everything is stored in the RustFS bucket `pokket-updates`:

| key | what |
| --- | --- |
| `assets/<md5>` | file bytes, content-addressed, written once |
| `updates/<uuid>.json` | one published update (manifest without URLs, plus `expoClient`) |
| `pointers/<channel>/<platform>/<runtime>.json` | what that runtime gets now, plus a history of 100 entries |

A publish writes the assets first, then the update, then the pointer, so no client ever sees
a manifest whose files are missing. The service is stateless, and pointers are read on every
check, so a rollback takes effect at once.

| endpoint | auth | |
| --- | --- | --- |
| `GET /api/manifest` | none | the app's update check, signed with `keyid="main"` (`rsa-v1_5-sha256`) |
| `GET /assets/<md5>` | none | streamed from RustFS, cached for a year (`immutable`) |
| `POST /api/publish` | bearer | `archive` (tar.gz of `expo export` output plus `expoConfig.json`), `platform`, `runtimeVersion`, `channel`, `message` |
| `POST /api/rollback` | bearer | `platform`, `runtimeVersion`, `channel`, plus `to=<update id>` or `embedded=true` |
| `GET /api/status` | bearer | pointer and history for `?platform=&runtimeVersion=&channel=` |
| `GET /healthz` | none | |

If a request has no `expo-channel-name` header, the service falls back to `default_channel`
and logs a warning. Watch for that warning on the first real check-ins.

## Operating it

- Config: `../configs/updates.yaml`. It holds the publish token (GitHub secret `UPDATES_TOKEN`
  on arkitektio/pokket) and the RustFS keys of the `updates` user from `rustfs_init.yaml`. The
  file is a single-file mount, so after editing it run `docker compose restart updates`.
- Signing key: `../data/updates/keys/private-key.pem`, mounted read-only at `/keys`. **Back it
  up.** Its `certificate.pem` is compiled into the app, so losing or rotating the key means
  shipping a new binary. The certificate is valid until 2036.
- After a code change: `docker compose build updates && docker compose up -d updates`.
- Gateway site in `../configs/Caddyfile`:
  ```
  updates.arkitekt.live {
  	encode zstd gzip
  	reverse_proxy updates:80
  }
  ```
- Check what a client would get (verifies the signature and re-hashes every asset):
  `uv run --with cryptography python3 scripts/verify.py https://updates.arkitekt.live ../data/updates/keys/certificate.pem android <runtime> production`
- Roll back:
  `curl -H "Authorization: Bearer $TOKEN" -F platform=android -F runtimeVersion=<rv> -F channel=production -F embedded=true https://updates.arkitekt.live/api/rollback`

## Switching pokket over

`updates.url` is compiled into each binary. Installs made before the switch keep polling
u.expo.dev, keep their last EAS update, and move over when people install the next build.

1. Copy `../data/updates/keys/certificate.pem` to `certs/certificate.pem` in pokket.
2. In `app.config.ts`, set:
   ```ts
   updates: {
     url: "https://updates.arkitekt.live/api/manifest",
     checkAutomatically: "ON_LOAD",
     fallbackToCacheTimeout: 0,
     codeSigningCertificate: "./certs/certificate.pem",
     codeSigningMetadata: { keyid: "main", alg: "rsa-v1_5-sha256" },
   },
   ```
3. Run `npx expo-updates runtimeversion:resolve --platform android` (and `ios`) before and
   after the change. The fingerprint must change, or `native-build.sh check` decides that no
   new binary is needed. If it doesn't change, delete the `native/<platform>/<rv>` tag.
4. Copy `scripts/publish-update.sh` to `scripts/ci/` in pokket. Then, in `release.yaml`, replace
   both "Publish Over-the-Air Update" steps (android and ios) with:
   ```yaml
   - name: Publish Over-the-Air Update
     env:
       UPDATES_TOKEN: ${{ secrets.UPDATES_TOKEN }}
     run: |
       eas env:pull --environment production --non-interactive
       scripts/ci/publish-update.sh <android|ios> "${{ steps.native.outputs.runtime_version }}" production "v$VERSION"
   ```
   `eas env:pull` keeps the `EXPO_PUBLIC_*` values that `eas update --environment production`
   used to inject.
5. Add the `UPDATES_TOKEN` secret to the pokket repository.
