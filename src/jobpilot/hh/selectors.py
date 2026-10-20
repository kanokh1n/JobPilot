"""CSS selectors of the hh.ru web pages the browser automation touches.

Verified against live pages 2026-10-19: RESPOND_BUTTON, LETTER_INPUT, SUBMIT_BUTTON, POPUP_CLOSE,
RESUME_OPTION (selected resume only), LOGGED_IN_MARKER. Still unverified: the resume dropdown options, CAPTCHA.
Originally written from memory of hh's `data-qa` attributes, not checked against the live site
(no hh access from the build environment). Confirm each one on a real vacancy page before the
first live run; `jobpilot hh-run` without --live only prints what it would do.
"""

RESPOND_BUTTON = '[data-qa="vacancy-response-link-top"]'
LETTER_INPUT = '[data-qa="vacancy-response-popup-form-letter-input"]'
SUBMIT_BUTTON = '[data-qa="vacancy-response-submit-popup"]'
POPUP_CLOSE = '[data-qa="response-popup-close"]'
ANOTHER_COUNTRY_CONFIRM = "Все равно откликнуться"  # button text of the other-country warning
ADD_LETTER = "Добавить сопроводительное"  # button text that opens the hidden letter field
RESUME_OPTION = '[data-qa="resume-title"]'  # the selected resume of the response popup
LOGGED_IN_MARKER = '[data-qa="mainmenu_profileAndResumes"]'
CAPTCHA = 'iframe[src*="captcha"], [data-qa="account-captcha-picture"]'
RESUMES_URL = "https://hh.ru/applicant/resumes"
RAISE_RESUME = "Поднять в поиске"  # UNVERIFIED button text; hh allows it once per 4 hours per resume
NEGOTIATIONS_URL = "https://hh.ru/applicant/negotiations"
