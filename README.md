# OpenGeode Index API

OpenGeode is a Python/FastAPI reimplementation of the [Geode SDK server](https://github.com/geode-sdk/server) API.

**Upstream compatibility target:** Geode Index Server **v0.59.1**.

The project keeps the upstream `/v1/...` API shape while using SQLite and local filesystem storage instead of the upstream server's PostgreSQL/S3 production stack.

## Quick start

### Requirements

- Python 3.10+
- `pip`
- A writable working directory

### Install

```bash
git clone https://github.com/BlueToadMakerr/OpenGeode.git
cd OpenGeode

python3 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### Configure

Copy the example environment file:

```bash
cp .env.example .env
```

For a local development server, the defaults are enough. For GitHub login, set `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, and `GITHUB_CALLBACK_URL`.

Generate a JWT secret with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Then put the generated value in `JWT_SECRET`.

### Start

```bash
source .venv/bin/activate
python main.py
```

By default the API listens on `127.0.0.1:3000`.

Useful endpoints:

- API documentation: `/docs`
- OpenAPI JSON: `/openapi.json`
- Health check: `/`

To expose the server to other machines on your LAN, set:

```env
HOST=0.0.0.0
PORT=3000
BASE_URL=http://YOUR_SERVER_IP:3000
```

## Environment variables

All settings are read from `.env` (or the process environment).

| Variable | Default | Function |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | Interface/address Uvicorn binds to. Use `0.0.0.0` for LAN/container access. |
| `PORT` | `3000` | HTTP port used by the API. |
| `ENV` | `development` | General environment label. |
| `BASE_URL` | `http://localhost:3000` | Public base URL used when generating API/download/attachment URLs. |
| `FRONT_URL` | `https://geode-sdk.org` | Frontend base URL used by mod status badge links. |
| `GITHUB_CLIENT_ID` | empty | GitHub OAuth application client ID. |
| `GITHUB_CLIENT_SECRET` | empty | GitHub OAuth application client secret. |
| `GITHUB_CALLBACK_URL` | `http://localhost:3000/v1/login/github/callback` | OAuth callback URL registered with GitHub. |
| `JWT_SECRET` | empty | Secret used to sign access and refresh JWTs. Set this in production. |
| `JWT_ALGORITHM` | `HS256` | JWT signing algorithm. |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `60` | Lifetime of access tokens. |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `30` | Lifetime of refresh tokens. |
| `DEVICE_FLOW_EXPIRE_SECONDS` | `900` | How long a device login attempt remains valid. |
| `DEVICE_FLOW_POLL_INTERVAL` | `5` | Device-flow polling interval in seconds. |
| `DATABASE_DIR` | `database` | Directory used for database-related files. |
| `DATABASE_SQLITE_FILE` | `database/database.sqlite3` | SQLite database file. |
| `DATABASE_FILE` | `database/database.json` | Legacy JSON database path used only for first-run migration. |
| `MODS_STORAGE_DIR` | `database/mods_storage` | Root directory for uploaded/local mod assets. |
| `LOGOS_DIR` | `database/mods_storage/logos` | Directory containing mod logos. |
| `ATTACHMENTS_DIR` | `database/mods_storage/attachments` | Directory containing submission comment attachments. |
| `ATTACHMENT_PERMISSIONS` | `1` | Attachment upload policy: 1 anyone, 2 admins/verified, 3 admins/developers with an accepted mod, 4 admins only, 5 disabled. |
| `MAX_ATTACHMENT_SIZE_BYTES` | `8388608` | Maximum size of one uploaded attachment. |
| `MAX_ATTACHMENTS_PER_COMMENT` | `5` | Maximum number of attachments on one comment. |
| `MAX_GEODE_FILE_SIZE_BYTES` | `67108864` | Maximum downloaded `.geode` package size accepted when submitting a mod/version. |
| `DEFAULT_PER_PAGE` | `10` | Default API pagination size. |
| `MAX_PER_PAGE` | `100` | Maximum API pagination size. |
| `BOOTSTRAP_ADMINS` | empty | Comma-separated GitHub usernames that are bootstrapped as administrators. |

### Attachment permission levels

`ATTACHMENT_PERMISSIONS` uses the same five-level concept as the current Geode server:

1. **Full** — anyone who can comment can upload attachments.
2. **Verified** — admins and verified developers.
3. **Approved Developers** — admins and developers with at least one accepted mod.
4. **Admins Only** — administrators only.
5. **Off** — attachment uploads are disabled.

## Storage

OpenGeode uses SQLite for its database and local filesystem storage for uploaded assets.

The first startup creates the configured directories automatically. If an older installation contains the legacy JSON database, it is migrated into SQLite and renamed with a `.migrated` suffix.

For production, back up at least:

- `DATABASE_SQLITE_FILE`
- `MODS_STORAGE_DIR`

Do not expose the SQLite database or storage directories directly over HTTP. The application serves the required upload paths itself.

## GitHub login

Create a GitHub OAuth application in GitHub's developer settings and configure:

```env
GITHUB_CLIENT_ID=...
GITHUB_CLIENT_SECRET=...
GITHUB_CALLBACK_URL=https://your-domain.example/v1/login/github/callback
BASE_URL=https://your-domain.example
```

The callback URL must exactly match the URL configured in the OAuth application.

## Compatibility notes

OpenGeode tracks the public API of the upstream Geode Index server and is currently based on **v0.59.1**.

The Python implementation intentionally differs in infrastructure:

- **Database:** SQLite instead of PostgreSQL.
- **File storage:** local filesystem instead of the upstream S3-compatible storage layer.
- **Runtime:** FastAPI/Uvicorn instead of Actix Web.
- **Production reverse-proxy/CDN setup:** not required for local use.

API behavior is kept separate from those infrastructure choices. When the upstream API changes, update the compatibility target and port the corresponding behavior rather than replacing the Python implementation with the Rust server.

## Development

Run with reload enabled using:

```bash
python main.py
```

The FastAPI interactive documentation is available at `/docs`.

For API compatibility work, compare OpenGeode against the upstream repository and update the compatibility version whenever the upstream release being mirrored changes.

## License

See the repository's license and upstream Geode server licensing for the applicable terms.
