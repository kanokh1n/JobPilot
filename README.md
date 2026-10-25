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

hh goes through [hh-applicant-tool](https://github.com/s3rgeym/hh-applicant-tool) by Sergey M., installed as
an unchanged package (`hh/client.py`). It talks to hh like the official Android app: the app's API for
search, responses, resumes and the list of responses, and the chat app's API with the site cookies saved at
sign-in. No browser runs anywhere, so the 2 GB server is enough. The token and cookies of each account live in
`data/hh/<account>/` (`token.json`, `cookies.txt`); the token refreshes itself.

There is no LLM: a vacancy gets a response when its title or snippet contains an `include` word and
none of the `exclude` words (whole words, case-insensitive, "ё" = "е"), and the response carries one
template cover letter per language.

```
cp accounts.example.toml accounts.toml       # accounts, queries, resume titles, include/exclude words
cp data/letters/ru.example.txt data/letters/ru.txt   # and write en.txt; <account>_<lang>.txt overrides
jobpilot hh-auth php +79991234567   # once: asks for the SMS code (and a captcha, if hh shows one)
jobpilot hh-whoami php       # who is signed in and which resumes hh sees
jobpilot hh-search php       # prints what the queries find, nothing else
jobpilot hh-explain php      # why each found vacancy is taken or skipped
jobpilot hh-run php          # dry run: prints which vacancies it would respond to
jobpilot hh-run php --live   # really applies
jobpilot hh-chats php        # copies the employer chats into the database once (read-only)
```

Letter placeholders: `{vacancy}` and `{employer}`. The bot command `/resume ru|en` picks the resume
and the letter language.

Safety: the daily limit ramps up from `start_limit` by `ramp_step` a day to `max_limit`, applications
only go out 09:00-21:00 local time with a 1-3 minute pause between them, a captcha or three failures in
a row stop the account, and hh's own daily limit ends the pass. On a captcha the worker does not solve it:
the picture goes to Telegram and both accounts rest 3, then 12, then 24 hours.

## Hirify

With `HIRIFY_API_KEY` in `.env` (hirify.me/mcp-api, API) the worker reads Hirify vacancies every hour from
9:15 to 20:15 through the Agent API (`hirify/`). The filter is the setting `hirify_filter` (a query string from
Hirify's filter guide; by default Go backend anywhere). Where Hirify takes the
application itself (`can_apply_directly`), it applies with the first Hirify profile and the letter
`data/letters/hirify_en.txt` (or `go_en.txt`), at most 10 a pass and 100 a day (Hirify's own daily allowance); titles with the go account's
exclude words are skipped. Every other vacancy goes to the site's Hirify tab and to Telegram like a feed item; up to 10 a pass get their contact revealed (Telegram, e-mail or form, 100 a day at Hirify), and the link then leads there.
The first pass only fills the site.

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

## Chats (v4, v8)

The worker copies the account's hh chats (the chat app's API: the list, then the messages of every chat
whose last message changed) into the tables `chats` and `messages`. Nothing is marked read on hh. A new
employer message is announced in Telegram; a chat state such as "discard" or "interview" also moves the
stage (never over a manual one). From 8:00 to 24:00 a watcher (`hh/live.py`) asks for the unread count
every two minutes and reads the chats when it changes (at most once a minute and 20 times an hour), and
once an hour anyway. With "Чаты hh в реальном времени" off on /settings, chats are read hourly.

Replies are typed on the chat page of the site. They wait in the `outbox` table; the worker checks it
every 15 seconds, sends the text through the chat API and copies the chat back. During a captcha hold
they wait; a failed one shows on the site with "повторить" and is never retried by itself. The chat list
can be filtered by hh status (приглашение, отказ, оффер, без ответа), unread chats and chats where the
employer wrote last.

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

## Site layout (v10)

Five sections: **Обзор** (the day in numbers, "требует внимания", funnels; "Графики" is its second view,
`/?view=charts`), **Вакансии** (`/jobs`: hh applications, Telegram posts and feed items in one list, a tab per
hh account, Telegram and feed country, shared status filters Новые / Интересные / Отклик / В процессе / Оффер /
Отказ / Мимо), **Чаты**, **Календарь** and **Настройки** (Общее, a section per hh account, Капча, Телеграм,
Ленты). «Откликнулся» on a Telegram post or a feed item makes it an application (`applications.source` =
`tg` or `feed`, account = `tg` or the feed country) with stages, a note and interviews like an hh one; only you move
its stage, and "убрать отклик" on its page undoes it. No schema change. The old addresses (`/stages`, `/account/<name>`, `/telegram`, `/feeds`, `/stats`) redirect. Fonts
(Playfair Display, Manrope; SIL Open Font License) are served from `web/static/fonts`, nothing is loaded from
other sites.

## Job-site feeds (v9): Serbia

The worker reads the RSS feeds of job sites and keeps the vacancies in the table `feed_jobs` (database v5).
Read-only: nothing is posted to the sites, you apply there by hand. Built in for Serbia: HelloWorld.rs
(every 30 min) and Startit Poslovi (every 6 h). Poslovi Infostud is in the list but off: its feed has no
categories, only the 20 newest of all its ~12 000 jobs (IT ones of the group are on HelloWorld). Each feed has a country, so other
countries are added the same way: in Настройки → Ленты as `DE | Name | https://feed-url | minutes`.

Politeness: one feed at a time with a few seconds between them, never more often than every 15 minutes
or than the feed's own `<ttl>`, `If-None-Match`/`If-Modified-Since`, `Retry-After` on 429/503, a pause that
doubles after each error (up to a day), at most 2 MB per answer. The next read time is stored, so a restart
does not read the sites again. `FEEDS_USER_AGENT` in `.env` changes the User-Agent.

A vacancy matches when its title or categories contain one of the feed words and none of the excluded
ones (own words, in Настройки → Ленты; in descriptions "go" is mostly the verb). Matching ones go to
Telegram by the mode from Настройки → Телеграм: at once (more than 5 from one read as one list) or in the
daily digest, after the Telegram posts. Others are kept and shown in Вакансии under "Мимо".
👍/👎 work in the bot and on the site. After 3 failed reads in a row the bot says so once.

`jobpilot feeds-check [feed]` reads the feeds once and prints what they hold, writing nothing
(on the server: `docker compose run --rm jobpilot jobpilot feeds-check`).
