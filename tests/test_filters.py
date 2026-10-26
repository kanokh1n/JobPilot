from jobpilot.filters import Filters, is_lead, split_keywords, text_hash

VACANCY = "Ищем PHP разработчика в команду. Laravel, PostgreSQL, удалённо, оформление по контракту."


def test_matches_include_keyword():
    assert Filters(include=["php"]).match(VACANCY)


def test_word_boundary_does_not_match_inside_words():
    text = "Ищем разработчика в команду Google, нужен опыт с облаками и сервисами, удалённо."
    assert not Filters(include=["go"]).match(text)


def test_exclude_wins():
    assert not Filters(include=["php"], exclude=["laravel"]).match(VACANCY)


def test_requires_vacancy_marker_and_length():
    assert not Filters().match("PHP")
    assert not Filters().match("Продам гараж, недорого, звоните вечером, php не нужен совсем, торг уместен.")


def test_empty_include_accepts_any_vacancy():
    assert Filters().match(VACANCY)


def test_repost_hash_ignores_case_and_punctuation():
    assert text_hash(VACANCY) == text_hash(VACANCY.upper().replace(",", ""))


def test_split_keywords():
    assert split_keywords("PHP, Go\nGolang ,") == ["php", "go", "golang"]


def test_yo_equals_ye():
    assert not Filters(include=["php"], exclude=["стажер"]).match(VACANCY + " Стажёр приветствуется.")


def test_multiword_keyword():
    text = "Ищем разработчика на Go Lang в команду платежей, удалённо, оформление по контракту, зарплата обсуждается."
    assert Filters(include=["go lang"]).match(text)


def test_lead_titles():
    for title in ("Team Lead PHP", "Tech Lead (Go)", "Lead Golang Developer", "Teamlead", "Тимлид PHP",
                  "Техлид Go", "Лид-разработчик Go", "Team-lead backend"):
        assert is_lead(title), title
    for title in ("Senior PHP Developer", "Go developer (leading fintech)", "Backend Go, Lidl"):
        assert not is_lead(title), title
