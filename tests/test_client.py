"""HHClient over hh-applicant-tool: what the app API answers turns into our results."""
import asyncio

from hh_applicant_tool.api import errors

from jobpilot.hh.apply import ApplyResult, CaptchaError
from jobpilot.hh.client import HHClient
from jobpilot.hh.models import Vacancy


class Response:
    status_code = 400
    headers = {}


def api_error(cls, *values):
    return cls(Response(), {"errors": [{"type": "negotiations", "value": v} for v in values]})


class FakeApi:
    access_token = "USERTOKEN"

    def __init__(self, answers=None, post_error=None):
        self.answers, self.post_error, self.calls = answers or {}, post_error, []

    def get(self, endpoint, params=None, **kw):
        self.calls.append(("GET", endpoint, params))
        answer = self.answers[endpoint]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def post(self, endpoint, params=None, **kw):
        self.calls.append(("POST", endpoint, params))
        if self.post_error:
            raise self.post_error
        return {}


class FakeTool:
    last_captcha_shot = None

    def __init__(self, api):
        self.api_client = api

    def save_cookies(self):
        pass


def client(api):
    c = HHClient(None, "go")
    c.__dict__["tool"] = FakeTool(api)
    return c


RESUMES = {"items": [{"id": "r1", "title": "Go-разработчик", "status": {"id": "published"},
                      "can_publish_or_update": True},
                     {"id": "r2", "title": "Old", "status": {"id": "not_published"}}]}
VACANCY = Vacancy("7", "Go dev", "Acme", "https://hh.ru/vacancy/7", "", "")


def apply(error=None, resumes=RESUMES, title="go-разработчик"):
    api = FakeApi({"/resumes/mine": resumes}, post_error=error)
    return asyncio.run(client(api).apply(VACANCY, title, "Здравствуйте")), api


def test_apply_sends_the_letter_with_the_resume_by_title():
    result, api = apply()
    assert result is ApplyResult.APPLIED
    assert api.calls[-1] == ("POST", "/negotiations", {"resume_id": "r1", "vacancy_id": "7", "message": "Здравствуйте"})


def test_apply_errors_become_results():
    assert apply(title="Old")[0] is ApplyResult.FAILED  # not published
    assert apply(api_error(errors.LimitExceeded, "limit_exceeded"))[0] is ApplyResult.LIMIT
    assert apply(api_error(errors.Forbidden, "already_applied"))[0] is ApplyResult.ALREADY
    assert apply(api_error(errors.Forbidden, "test_required"))[0] is ApplyResult.NEEDS_TEST
    assert apply(api_error(errors.Redirect))[0] is ApplyResult.NEEDS_TEST
    assert apply(api_error(errors.BadRequest, "something_else"))[0] is ApplyResult.FAILED
    captcha = errors.CaptchaRequired(Response(), {"errors": [{"value": "captcha_required", "captcha_url": "u"}]})
    assert apply(captcha)[0] is ApplyResult.CAPTCHA


def test_search_skips_answered_external_and_archived():
    item = lambda id, **kw: {"id": id, "name": f"Go {id}", "alternate_url": f"https://hh.ru/vacancy/{id}",
                             "employer": {"name": "E"}, "snippet": {}, **kw}
    api = FakeApi({"/vacancies": {"pages": 1, "items": [
        item("1"), item("2", relations=["got_response"]), item("3", response_url="https://x"),
        item("4", archived=True), item("5", has_test=True)]}})
    found = asyncio.run(client(api).search("Go", {"work_format": "REMOTE"}))
    assert [v.id for v in found] == ["1", "5"] and found[1].has_test
    assert api.calls[0][2]["work_format"] == "REMOTE" and api.calls[0][2]["text"] == "Go"


def test_search_captcha_is_our_captcha():
    api = FakeApi({"/vacancies": errors.CaptchaRequired(Response(), {"errors": [{"captcha_url": "u"}]})})
    try:
        asyncio.run(client(api).search("Go"))
    except CaptchaError:
        pass
    else:
        raise AssertionError("no CaptchaError")


def test_negotiations_use_the_old_state_names():
    api = FakeApi({"/negotiations": {"pages": 1, "items": [
        {"vacancy": {"id": "1"}, "state": {"id": "response"}, "viewed_by_opponent": False, "chat_id": 11},
        {"vacancy": {"id": "2"}, "state": {"id": "response"}, "viewed_by_opponent": True, "chat_id": 12},
        {"vacancy": {"id": "3"}, "state": {"id": "discard"}, "chat_id": None}]}})
    assert asyncio.run(client(api).negotiations()) == {
        "1": ("not-viewed", "11"), "2": ("viewed", "12"), "3": ("discard", None)}


def test_raise_resumes_only_where_hh_allows():
    api = FakeApi({"/resumes/mine": RESUMES})
    assert asyncio.run(client(api).raise_resumes()) == 1
    assert ("POST", "/resumes/r1/publish", None) in api.calls


def test_recommended_for_the_resume():
    api = FakeApi({"/resumes/mine": RESUMES, "/resumes/r1/similar_vacancies": {"pages": 1, "items": [
        {"id": "8", "name": "Go", "alternate_url": "u", "employer": {"name": "E"}, "snippet": {}},
        {"id": "9", "name": "Go", "alternate_url": "u", "relations": ["got_response"]}]}})
    found = asyncio.run(client(api).recommended("Go-разработчик"))
    assert [v.id for v in found] == ["8"]
