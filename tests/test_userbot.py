import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon.tl.types import InputPeerChannel, InputPeerChat

from jobpilot.config import Config
from jobpilot.db import Database
from jobpilot.tg import sources
from jobpilot.tg.userbot import resolve_sources, source_titles


class FakeClient:
    def __init__(self, folders, known):
        self._folders = folders
        self._known = known

    async def __call__(self, request):
        return SimpleNamespace(filters=self._folders)

    async def get_peer_id(self, source):
        if source not in self._known:
            raise ValueError(source)
        return self._known[source]


def cfg(folder=None, sources=()):
    return Config(api_id=1, api_hash="h", bot_token="", owner_id=0, tz="UTC", folder=folder,
                  sources=tuple(sources), data_dir=Path("."), hh_user_agent="ua", hh_token=None)


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "t.db")


def resolve(client, config, db):
    return asyncio.run(resolve_sources(client, config, db))


def folder(title, *peers):
    return SimpleNamespace(title=SimpleNamespace(text=title), include_peers=list(peers))


def test_folder_chats_become_marked_ids(db):
    client = FakeClient([folder("Other", InputPeerChat(5)), folder("Вакансии", InputPeerChannel(10, 1), InputPeerChat(7))], {})
    assert resolve(client, cfg(folder="Вакансии"), db) == ({-1000000000010, -7}, [])


def test_new_chat_in_folder_appears_on_next_resolve(db):
    client = FakeClient([folder("Вакансии", InputPeerChannel(10, 1))], {})
    first, _ = resolve(client, cfg(folder="Вакансии"), db)
    client._folders = [folder("Вакансии", InputPeerChannel(10, 1), InputPeerChannel(11, 2))]
    second, _ = resolve(client, cfg(folder="Вакансии"), db)
    assert second - first == {-1000000000011}


def test_explicit_sources_and_unknown_ones(db):
    client = FakeClient([], {"@jobs": -100123})
    assert resolve(client, cfg(sources=["@jobs", "@nope"]), db) == ({-100123}, ["@nope"])


def test_missing_folder_gives_no_sources(db):
    assert resolve(FakeClient([], {}), cfg(folder="Вакансии"), db) == (set(), ["папка «Вакансии»"])


def test_sources_and_folder_from_the_site(db):
    client = FakeClient([folder("Вакансии", InputPeerChat(7)), folder("Другая", InputPeerChat(8))],
                        {"@jobs": -100123, "@more": -100124})
    db.set(sources.EXTRA, '["@more"]')
    assert resolve(client, cfg(folder="Вакансии", sources=["@jobs"]), db)[0] == {-100123, -100124, -7}
    db.set(sources.FOLDER, "Другая")
    assert resolve(client, cfg(folder="Вакансии"), db)[0] == {-100124, -8}
    db.set(sources.FOLDER, "")  # no folder at all, even if .env names one
    assert resolve(client, cfg(folder="Вакансии"), db)[0] == {-100124}


def test_normalize_source():
    assert sources.normalize(" @remote_jobs ") == "@remote_jobs"
    assert sources.normalize("https://t.me/remote_jobs") == "@remote_jobs"
    assert sources.normalize("t.me/s/remote_jobs/123") == "@remote_jobs"
    assert sources.normalize("-1001234567") == "-1001234567"
    assert sources.normalize("https://t.me/+AbCdEf") is None
    assert sources.normalize("https://t.me/joinchat/AbCdEf") is None
    assert sources.normalize("not a channel") is None


def test_source_titles_lists_names_and_ids_of_unknown_chats(db):
    class Client(FakeClient):
        async def iter_dialogs(self):
            for dialog in (SimpleNamespace(id=-7, name="Remote jobs"), SimpleNamespace(id=-99, name="Other")):
                yield dialog

    client = Client([folder("Вакансии", InputPeerChat(7), InputPeerChat(8))], {})
    assert asyncio.run(source_titles(client, cfg(folder="Вакансии"), db)) == ["Remote jobs", "-8"]
