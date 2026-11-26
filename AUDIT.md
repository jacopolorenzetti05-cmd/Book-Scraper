# Publication audit: Books to Scrape

Sanitized record of the pre-publication audit required by the course. No secret values,
keys, project IDs or personal contact details appear in this file.

| | |
|---|---|
| **Audited** | `main` and all remote branches (`v1-sqlite` included), 12 commits, 27 tracked files |
| **Tool** | Claude Code, using the course audit prompt, plus manual review by the team |
| **Rounds** | Round 1 found the issues → fixes applied → round 2 re-ran every check |
| **Result** | ✅ No secrets in files or history. All confirmed issues fixed or documented. |

## What was checked

| Check | How |
|---|---|
| Secrets and credentials | Pattern scan of the **full Git history on every branch** for JWTs (`eyJ…`), Supabase secret/publishable keys, personal access tokens, Supabase project URLs, AWS/Google/GitHub/OpenAI/Anthropic key formats and private-key headers |
| Sensitive files ever committed | List of every path ever tracked, searched for `.env`, `*.db`, `*.sqlite`, `*.pem`, `*.key`, credentials and tokens |
| Personal data | E-mail addresses in file contents and history |
| Exclusions | `.gitignore` compared to the files the project generates (`books.db`, `.env`, caches, virtual envs) |
| Dependencies and setup | `requirements.txt`, `.env.example`, README instructions followed from a clean start |
| README | The 7 required sections: purpose, data collection, persistence, interaction, setup, learning, limitations |
| Code security (extra) | Query building in `app/server.py` (injection), HTML escaping in `index.html` (XSS), where the key is used |

## Round 1: findings

### Confirmed issues (action required)

| # | Issue | Location | Fix | Status |
|---|---|---|---|---|
| R1-1 | `.gitignore` incomplete: no rules for virtual envs (`.venv/`), `.env.*` variants, `*.sqlite`, test caches, editor/OS files | `.gitignore` | Rewritten with commented sections. `.env.example` stays tracked explicitly. | ✅ Fixed |
| R1-2 | **Not reproducible without our private key.** The library app needed Supabase credentials, and the README told readers to "ask the project owner". | `app/server.py`, `README.md` | Added `--sqlite books.db` mode: the app reads the local database the scraper writes, so it runs with no account. Covered by 5 new tests, including injection attempts. | ✅ Fixed |
| R1-3 | README missing required sections (Learning, Limitations and assistance, explicit Data collection / User interaction) and had no screenshots | `README.md` | Rewritten around the 7 required sections, with 3 screenshots from a real run | ✅ Fixed |
| R1-4 | Live demo returns "database unreachable": the free Supabase project auto-paused after 7 idle days | Supabase free tier (outside the repo) | **Accepted as expected free-tier behaviour.** Documented in the README. Setup option A (SQLite) shows the full logic and design with no cloud account. | ✅ Documented |

### Possible concerns (reviewed)

| # | Concern | Decision |
|---|---|---|
| R1-5 | The library server sent no HTTP security headers | **Fixed:** every response now sends `Content-Security-Policy`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`. The page was checked in Chromium with zero CSP violations. |
| R1-6 | The scraper and server use the `service_role` key, which bypasses Row Level Security | **Accepted.** The key exists only server-side (`.env`, Render env vars, GitHub Actions secrets), never in the browser or in git. The server exposes read-only endpoints. Listed under Limitations. |
| R1-7 | Supabase error bodies are logged | **Accepted.** They go to the server log only. The browser gets a generic `database error`. |
| R1-8 | Commit metadata contains the author's Git e-mail | **No change.** This is standard public Git metadata. Rewriting published history would break forks. For future commits we can use GitHub's `noreply` address. |

### Checked, no issue found

- **No secret appears anywhere in the history** (0 matches for every key pattern on every branch).
- `.env` was **never** committed. `.env.example` contains only empty placeholders.
- `render.yaml` uses `sync: false` for both secrets. The workflow reads them from `secrets.*`.
- No database file, cache or dependency folder is tracked.
- SQL/PostgREST injection: sort is a whitelist, numbers are parsed and range-checked, IDs are regex-validated, and SQLite uses bound parameters.
- XSS: every value from the database is HTML-escaped before rendering.
- The scraper can only fetch `books.toscrape.com` (host check in `scraper/fetch.py`).

## Round 2: re-check after fixes

| Check | Result |
|---|---|
| History secret scan (all patterns, all branches) | 0 matches |
| `git status` after a full scrape + test run | Clean: `books.db`, `__pycache__/` correctly ignored |
| Tracked files | Source, tests, fixtures, docs, screenshots only |
| Unit tests | **24 / 24 pass** (19 original + 5 new) |
| Full scrape | 50 pages, 1,000 books, 50 categories, **0 errors** |
| App in SQLite mode | Search, filters, sort, book card and ledger work. Bad input returns HTTP 400. Zero console errors. |
| README | All 7 sections present, commands re-run from a clean start |

## Could not verify

- The values stored in the **Render dashboard** and **GitHub Actions secrets** (outside the repo; we only confirmed the repo references them by name).
- Whether a key was ever shared outside Git (chat, e-mail). If so, rotate it in Supabase → Project Settings → API.
- Live RLS policies on the Supabase project (paused during the audit). The intended policies are in `db/schema.sql`.

*Finding no secrets does not prove the project is safe. It means none of the checks above found one.*
