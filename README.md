# OpenGeode Index API (Python reimplementation)

A Python reimplementation of the [Geode SDK server](https://github.com/geode-sdk/server) API.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# fill in JWT_SECRET (see comment in .env.example for how to generate one)
# and GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET to enable GitHub login

python main.py
```
Once started, use `http://localhost:3000/docs` for API routes

## How auth works

- **Access tokens** are short-lived JWTs (`ACCESS_TOKEN_EXPIRE_MINUTES`, default 1h) signed with `JWT_SECRET`, carrying the developer id and a session id.
- **Refresh tokens** are random strings. The server only stores a SHA-256 hash of each one (in the `tokens` table) alongside a session id, so `DELETE /v1/me/token` and `/v1/me/tokens` can remove sessions without needing a token blocklist.
- Logging in via device flow, web flow, or a personal access token all funnel through the same `_get_or_create_developer` helper in `routers/auth.py`, which creates a `Developer` row on first login. Usernames listed in `BOOTSTRAP_ADMINS` are promoted to admin on that first login, so you have a way to get an initial admin account!

## How mod submission works

`POST /v1/mods` and `POST /v1/mods/{id}/versions` only take a `download_link` the server downloads that file, unzips the `.geode` file, and reads `mod.json` from it to add the mod's metadata. If the mod already exists and the version number is higher than the last, the new version will be listed

Like Geode, every new version starts in the `pending` status with a submission record attached (unless the submitter is a **verified** developer -- see below), which is where the review/comment/attachment workflow (`/submission`, `/submission/comments`, `.../attachments`) comes in. Geode's Index API uses these comments for public discussion, however it can be used as a way to comment on mods :P Anyways, an admin moves it to `accepted` or `rejected` via `PUT /v1/mods/{id}/versions/{version}` and can unlist the mod using the same endpoint.

**Verified developers skip review entirely**: if the submitting developer is verified, their new mod/version is created with `status: accepted` immediately instead of `pending`, making their mod instantly public! A submission record still gets created either way, just starting from an already-accepted version.

## Mod visibility

Anyone -- authenticated or not -- can view any mod and any version of it, *except* rejected ones, which are hidden from every public read endpoint. **Admins are the exception**: they can see and fetch rejected mods/versions everywhere a non-admin would get blocked.

`direct_download_link` and a version's `info` (moderation notes) stay
gated to the mod's own developers and admins regardless of all of the
above. Being open to everyone will not show them.

This can be changed in `mod_queries.resolve_visible_statuses()`

## Managing a mod's developers

`POST /v1/mods/{id}/developers` and `DELETE /v1/mods/{id}/developers/{username}` are open to **any** of the mod's developers and admins.

Nobody can remove themselves through the `DELETE` route though.
## Assumptions / simplifications

Not everything  in this server was fully added. If you like to commit to make this more accurate, feel free to send a pull request!

- **`ModUpdate.replacement`**: Always `null`. This holds replacement mod id(s), but a deprecated mod requested through `GET /v1/mods/updates` is excluded from `updates` and surfaced via the separate `deprecations` array instead.. so there wasn't a real example of what `replacement` was..
- **Loader version downloads** (`POST /v1/loader/versions`): `CreateVersionBody` doesn't include per-platform download URLs, so they're derived from a GitHub release at  `geode-sdk/geode`.
- **`total_geode_downloads`** (`GET /v1/stats`): Because of the above issue, `total_geode_downloads` will **always** return 0

None of these change the responses above, so the server still runs!