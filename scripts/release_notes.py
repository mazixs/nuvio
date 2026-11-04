"""Формирование release notes по диапазону коммитов git.

Скрипт вызывается из `.github/workflows/release.yml` при push тега `v*`.
Классификация идёт по теме коммита (первой строке), а не по всему сообщению:
тело часто содержит слова другой категории, из-за чего `git log --grep`
относил один коммит сразу к двум разделам.
"""

import subprocess
import sys
from pathlib import Path


SECTION_FEAT = "feat"
SECTION_FIX = "fix"
SECTION_OTHER = "other"

# Префиксы conventional commits и слова, которыми темы писались до перехода
# на них. Порядок проверки важен: исправления идут первыми, поэтому тема
# `fix: добавлена валидация` попадает ровно в один раздел.
_FIX_PREFIXES = ("fix", "hotfix")
_FIX_KEYWORDS = ("исправ", "починк")
_FEAT_PREFIXES = ("feat", "add")
_FEAT_KEYWORDS = ("добавл",)

# Символы, отделяющие префикс от остальной темы: `fix:`, `fix(scope):`,
# `fix!:`, `Fix TikTok download`.
_PREFIX_BOUNDARY = ":!( \t/-"

_HEADINGS = (
    (SECTION_FEAT, "### ✨ Новое"),
    (SECTION_FIX, "### 🐛 Исправления"),
    (SECTION_OTHER, "### 📦 Остальные изменения"),
)


def _starts_with_prefix(subject: str, prefixes: tuple[str, ...]) -> bool:
    """Проверяет префикс темы с учётом границы слова."""
    for prefix in prefixes:
        if not subject.startswith(prefix):
            continue
        rest = subject[len(prefix):]
        if not rest or rest[0] in _PREFIX_BOUNDARY:
            return True
    return False


def classify_subject(subject: str) -> str:
    """Относит тему коммита ровно к одному разделу changelog."""
    normalized = subject.strip().casefold()

    if _starts_with_prefix(normalized, _FIX_PREFIXES) or any(
        keyword in normalized for keyword in _FIX_KEYWORDS
    ):
        return SECTION_FIX
    if _starts_with_prefix(normalized, _FEAT_PREFIXES) or any(
        keyword in normalized for keyword in _FEAT_KEYWORDS
    ):
        return SECTION_FEAT
    return SECTION_OTHER


def collect_subjects(commit_range: str) -> list[str]:
    """Возвращает темы коммитов диапазона в порядке вывода `git log`."""
    result = subprocess.run(
        ["git", "log", commit_range, "--pretty=format:%s"],
        capture_output=True,
        check=True,
        text=True,
        encoding="utf-8",
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def render_notes(subjects: list[str], version: str, repository: str) -> str:
    """Собирает текст release notes с разделами и инструкцией установки."""
    grouped: dict[str, list[str]] = {
        SECTION_FEAT: [],
        SECTION_FIX: [],
        SECTION_OTHER: [],
    }
    for subject in subjects:
        grouped[classify_subject(subject)].append(subject)

    lines = [f"## 🚀 Nuvio v{version}", ""]
    for section, heading in _HEADINGS:
        if not grouped[section]:
            continue
        lines.append(heading)
        lines.extend(f"- {subject}" for subject in grouped[section])
        lines.append("")

    lines += [
        "---",
        "",
        "### 📥 Установка через Docker",
        "",
        "```bash",
        f"git clone --branch v{version} --depth 1 https://github.com/{repository}.git",
        "cd nuvio",
        "mkdir -p .secrets",
        "cp .env.example .secrets/.env",
        "# заполнить TELEGRAM_TOKEN, ADMIN_IDS, TELEGRAM_API_ID, TELEGRAM_API_HASH и WEB_PASSWORD",
        f"TAG={version} docker compose --env-file .secrets/.env up -d",
        "```",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    """CLI: <диапазон коммитов> <версия> <owner/repo> <файл вывода>."""
    if len(argv) != 5:
        raise SystemExit(
            "использование: release_notes.py <commit-range> <version> "
            "<owner/repo> <output-path>"
        )

    commit_range, version, repository, output_path = argv[1:]
    notes = render_notes(collect_subjects(commit_range), version, repository)
    Path(output_path).write_text(notes, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
