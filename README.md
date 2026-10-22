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
jobpilot hh-chats php        # copies the employer chats into the database once (read-only)
jobpilot hh-chats-dump php   # saves what the hh chat app loads to data/dumps/php/chats (sends nothing)
```

Letter placeholders: `{vacancy}` and `{employer}`. The bot command `/resume ru|en` picks the resume
and the letter language.

Safety: the daily limit ramps up from `start_limit` by `ramp_step` a day to `max_limit`, applications
only go out 09:00-21:00 local time with a 1-4 minute pause between them, a captcha or three failures in
a row stop the account.

Status: search, keyword rules, limits, letters, bookkeeping and the dry run are covered by tests. The page
selectors (`hh/selectors.py`) and the negotiations scan / reminder sending (`hh/followup.py`) still have
to be written and checked against the live site.

## Site (v3)

`jobpilot web` serves a small site over the same database: an overview of both accounts
(funnel by stage, applications per day, latest stage changes), a page per account and the
stage table of every application. A stage picked in the table is marked as manual (✎) and the
hh worker no longer changes it; "вернуть автомат" hands it back to the system. Notes are saved
when the field loses focus.

Set `WEB_PASSWORD` in `.env`; with docker compose the site runs as the `web` service on port 8080
(`http://SERVER_IP:8080`). The traffic is plain HTTP, so on an untrusted network either put
`WEB_PUBLISH=127.0.0.1:8080` in `.env` and open it through `ssh -L 8080:localhost:8080 root@SERVER_IP`,
or add HTTPS in front later. Five wrong passwords from one address lock it out for 15 minutes.

Locally: `WEB_PASSWORD=... jobpilot web`, then http://localhost:8080.

## Chats (v4)

Every hour from 08:20 to 23:50 the worker reads the account's chats on hh (the page hh.ru/chat and the
chat app's own API) and copies new messages into the tables `chats` and `messages`. Only chats whose
last message changed are opened, and nothing is posted to hh, so chats stay unread there. A new employer
message is announced in Telegram; a chat state such as "discard" or "interview" also moves the stage
(never over a manual one). The site's "Чаты" page lists the chats with the stage of the matching
application and shows the open chat; the application card shows its chat too. Replies are still sent on
hh: sending from the site is v8.

## Live chats and replies (v8)

Each hh account now has one shared browser (`hh/browsers.py`): the apply, reminder, resume and chat jobs
borrow it instead of opening the profile themselves, so they no longer clash over it. From 8:00 to 24:00
a watcher (`hh/live.py`) keeps https://hh.ru/chat open in that browser; when the chat app's websocket
reports a chat event, the chats are read again at once (at most once a minute and 20 times an hour per
account), otherwise once an hour as before. A captcha hold, the global pause or the "Чаты hh в реальном
времени" switch on /settings closes the watcher; with the switch off chats are read hourly again (use it
if the server runs short of memory: the watcher keeps two Chromiums open in the daytime).

Replies are typed on the chat page of the site. They wait in the `outbox` table; the worker checks it
every 15 seconds and types the text into the hh chat like a person, then copies the chat back. During a
captcha hold they wait; a failed one shows on the site with "повторить" and is never retried by itself.
The chat list can be filtered by hh status (приглашение, отказ, оффер, без ответа), unread chats and
chats where the employer wrote last. Database v4 adds `chats.state`.

Not verified on live hh yet: the frames of the chat websocket (the first 40 of each session go to
`data/dumps/<account>/ws_frames.txt`, and `jobpilot hh-chats-dump <account>` saves them to `ws.txt`) and the
message box on hh.ru/chat/<id> (the same `data-qa="text-input"` as in the response iframe is assumed).

## Calendar and charts (v5)

"Календарь" shows interviews by month and the next ones; an interview has a date and time (in `TZ`),
an optional application, a call link and a note, and is added or edited by hand (also from the
application card). Adding one to an application that is still "sent", "viewed" or "invitation" moves it
to "interview". On the first start of the site, interview dates written in notes of applications at the
interview stage ("собес 24.10 в 15:00 https://...") are copied into the calendar; notes stay as they
were. Applications at that stage still without a date are listed on top of the calendar.
`jobpilot interviews-import --dry-run` shows what would be copied, without `--dry-run` it copies again
(applications that already have an interview are skipped).

"Графики": applications per day by account, employer answers per day (invitation or rejection) and the
funnel of the applications sent in the last 7, 30 or 90 days, in total and per account. The overview
also lists the next interviews.

## Telegram and settings (v6)

"Телеграм" lists the vacancies found in Telegram chats (filter by mark, source, text; 👍/👎 like the
bot's buttons), counts per source, and what the userbot is listening to right now (it reports after each
start and whenever the set changes). The same page changes the mode (realtime or daily digest), the
digest time, the include/exclude words, a Telegram-only pause, the folder name and extra channels
(@username, t.me link or id; on top of `TG_FOLDER`/`TG_SOURCES` from `.env`, which stay as the start),
and asks the bot for the digest now.

"Настройки": the global pause (same as /pause), RU/EN resume, lifting the captcha hold, and per hh
account: a pause of applications and reminders (chats are still read), start and daily limit, applications
per pass, search queries, include/exclude words and excluded companies. Only what differs from
`accounts.toml` is stored (settings key `hh_account:<name>`); "Вернуть как в accounts.toml" drops it.

Everything lives in the `settings` table, so no restart is needed: the bot checks the digest time and a
digest request every 30 s, the userbot re-reads the sources every 5 minutes, and an hh pass loads the
account's settings when it starts (the `hh-*` commands use them too). The bot commands keep working.

## Job-site feeds (v9): Serbia

The worker reads the RSS feeds of job sites and keeps the vacancies in the table `feed_jobs` (database v5).
Read-only: nothing is posted to the sites, you apply there by hand. Built in for Serbia: HelloWorld.rs
(every 30 min) and Startit Poslovi (every 6 h). Poslovi Infostud is in the list but off: its feed has no
categories, only the 20 newest of all its ~12 000 jobs (IT ones of the group are on HelloWorld). Each feed has a country, so other
countries are added the same way: on the "Ленты" page as `DE | Name | https://feed-url | minutes`.

Politeness: one feed at a time with a few seconds between them, never more often than every 15 minutes
or than the feed's own `<ttl>`, `If-None-Match`/`If-Modified-Since`, `Retry-After` on 429/503, a pause that
doubles after each error (up to a day), at most 2 MB per answer. The next read time is stored, so a restart
does not read the sites again. `FEEDS_USER_AGENT` in `.env` changes the User-Agent.

A vacancy matches when its title or categories contain one of the feed words and none of the excluded
ones (own words, on the "Ленты" page; in descriptions "go" is mostly the verb). Matching ones go to
Telegram by the mode from the Telegram page: at once (more than 5 from one read as one list) or in the
daily digest, after the Telegram posts. Others are kept and shown on the site under "все из лент".
👍/👎 work in the bot and on the site. After 3 failed reads in a row the bot says so once.

`jobpilot feeds-check [feed]` reads the feeds once and prints what they hold, writing nothing
(on the server: `docker compose run --rm jobpilot jobpilot feeds-check`).
