# JobPilot

Telegram vacancy monitor and (next) hh.ru auto-apply. Plan:
https://claude.ai/code/artifact/94a59c7f-279c-46b0-9fde-1135bdf7a76d

## Telegram module

A userbot (a separate Telegram account) reads the chats and channels you list, drops posts
that do not match the filters, removes reposts, and your own bot sends you the rest either
immediately or as one digest a day.

### Setup

1. Create `api_id` / `api_hash` at https://my.telegram.org for the userbot account.
2. Create a bot with @BotFather and get your numeric id (e.g. from @userinfobot).
3. `cp .env.example .env` and fill it in. Put the chats into a Telegram folder and set
   `TG_FOLDER` to its title, and/or list `@usernames` in `TG_SOURCES`.
4. Install and sign the userbot in once:

```
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
jobpilot login      # asks for the userbot phone number and code
jobpilot run
```

### Bot commands

`/include php, go` · `/exclude junior` · `/mode realtime|digest` · `/digest_time 09:00` ·
`/digest` · `/pause` · `/unpause` · `/filters` · `/status`

Filters match whole words, so `go` does not match "Google". A post must also look like a
vacancy (contains "вакансия", "hiring", "ищем", etc.) and be at least 80 characters.

### Tests

`pytest`

### Server

```
docker compose run --rm jobpilot jobpilot login   # once, interactive
docker compose up -d
```

The userbot session and the SQLite database live in `./data`.

## hh.ru module

hh closed the applicant API in December 2025, so it works in two halves:

- **Search** uses the public `api.hh.ru/vacancies` (read-only, still served).
- **Applying and reminders** drive a real Chromium (Playwright) logged into each account;
  one persistent browser profile per account in `data/browser/<account>`.

There is no LLM: a vacancy gets a response when its title or snippet contains an `include` word and
none of the `exclude` words (whole words, case-insensitive, "ё" = "е"), and the response carries one
template cover letter per language.

```
cp accounts.example.toml accounts.toml       # accounts, queries, resume titles, include/exclude words
cp data/letters/ru.example.txt data/letters/ru.txt   # and write en.txt; <account>_<lang>.txt overrides
playwright install chromium
jobpilot hh-login php        # opens a window: sign in by hand once
jobpilot hh-search php       # prints what the queries find, nothing else
jobpilot hh-dump php <vacancy-url>   # saves the vacancy, response window and negotiations pages to data/dumps (sends nothing)
jobpilot hh-run php          # dry run: prints which vacancies it would respond to
jobpilot hh-run php --live   # really applies
```

Letter placeholders: `{vacancy}` and `{employer}`. The bot command `/resume ru|en` picks the resume
and the letter language.

Safety: the daily limit ramps up from `start_limit` by `ramp_step` a day to `max_limit`, applications
only go out 09:00-21:00 local time with a 1-4 minute pause between them, a captcha or three failures in
a row stop the account.

Status: search, keyword rules, limits, letters, bookkeeping and the dry run are covered by tests. The page
selectors (`hh/selectors.py`) and the negotiations scan / reminder sending (`hh/followup.py`) still have
to be written and checked against the live site.
