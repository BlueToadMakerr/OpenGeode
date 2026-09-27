# OpenGeode Index API

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

## Assumptions / simplifications

Not everything in this server was fully added. If you like to commit to make this more accurate, feel free to send a pull request!

- **`ModUpdate.replacement`**: Always `null`. This holds replacement mod id(s), but a deprecated mod requested through `GET /v1/mods/updates` is excluded from `updates` and surfaced via the separate `deprecations` array instead.. so there wasn't a real example of what `replacement` was..
- **Loader version downloads** (`POST /v1/loader/versions`): `CreateVersionBody` doesn't include per-platform download URLs, so they're derived from a GitHub release at  `geode-sdk/geode`.
- **`total_geode_downloads`** (`GET /v1/stats`): Because of the above issue, `total_geode_downloads` will **always** return 0

None of these change the responses above, so the server still runs!
