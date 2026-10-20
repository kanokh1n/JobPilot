from __future__ import annotations

from pathlib import Path

from .models import Account, Vacancy


def load_template(letters_dir: Path, account: Account, lang: str) -> str:
    """`<account>_<lang>.txt` wins over the shared `<lang>.txt`."""
    for name in (f"{account.name}_{lang}.txt", f"{lang}.txt"):
        path = letters_dir / name
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    raise FileNotFoundError(f"Cover letter template missing: {letters_dir / (lang + '.txt')}")


def render_letter(template: str, vacancy: Vacancy) -> str:
    """Fills the optional {vacancy} and {employer} placeholders."""
    return template.replace("{vacancy}", vacancy.name).replace("{employer}", vacancy.employer)
