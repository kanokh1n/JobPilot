import asyncio
from pathlib import Path
from types import SimpleNamespace

from telethon.tl.types import InputPeerChannel, InputPeerChat

from jobpilot.config import Config
from jobpilot.tg.userbot import resolve_source_ids


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


def folder(title, *peers):
    return SimpleNamespace(title=SimpleNamespace(text=title), include_peers=list(peers))


def test_folder_chats_become_marked_ids():
    client = FakeClient([folder("Other", InputPeerChat(5)), folder("Вакансии", InputPeerChannel(10, 1), InputPeerChat(7))], {})
    assert asyncio.run(resolve_source_ids(client, cfg(folder="Вакансии"))) == {-1000000000010, -7}


def test_new_chat_in_folder_appears_on_next_resolve():
    client = FakeClient([folder("Вакансии", InputPeerChannel(10, 1))], {})
    first = asyncio.run(resolve_source_ids(client, cfg(folder="Вакансии")))
    client._folders = [folder("Вакансии", InputPeerChannel(10, 1), InputPeerChannel(11, 2))]
    second = asyncio.run(resolve_source_ids(client, cfg(folder="Вакансии")))
    assert second - first == {-1000000000011}


def test_explicit_sources_and_unknown_ones():
    client = FakeClient([], {"@jobs": -100123})
    assert asyncio.run(resolve_source_ids(client, cfg(sources=["@jobs", "@nope"]))) == {-100123}


def test_missing_folder_gives_no_sources():
    assert asyncio.run(resolve_source_ids(FakeClient([], {}), cfg(folder="Вакансии"))) == set()
