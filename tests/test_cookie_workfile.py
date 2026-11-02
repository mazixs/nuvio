"""Тесты рабочей копии cookie-файла.

yt-dlp перезаписывает файл, который ему отдали в `cookiefile`, и при записи
теряет сессионные cookies. Замерено на проде: один прогон убрал `YSC`, файл
похудел с 15 записей до 14. За недели работы от загруженного админом набора
осталась одна auth-cookie из шести — YouTube начал отвечать «Sign in to confirm
you're not a bot».

Поэтому yt-dlp получает не оригинал, а рабочую копию: пусть портит её.
"""

import os

import pytest
import yt_dlp

from utils.cookie_workfile import cleanup_old_workfiles, working_cookie_file


pytestmark = pytest.mark.unit

ORIGINAL = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tYSC\tabc\n"
# Срок задан явно: без него yt-dlp сохранил бы другое значение, и файл бы отличался.
SAVED_BY_YT_DLP = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t4102444800\tSID\tabc\n"
    ".youtube.com\tTRUE\t/\tTRUE\t4102444800\tHSID\tdef\n"
)


@pytest.fixture
def original(tmp_path):
    path = tmp_path / "www.youtube.com_cookies.txt"
    path.write_text(ORIGINAL, encoding="utf-8")
    return path


@pytest.fixture
def work_dir(tmp_path):
    return tmp_path / "work"


def test_missing_original_gives_nothing(tmp_path, work_dir):
    assert working_cookie_file(tmp_path / "нет.txt", work_dir=work_dir) is None


def test_copy_repeats_the_original(original, work_dir):
    copy = working_cookie_file(original, work_dir=work_dir)

    assert copy is not None
    assert copy != original
    assert copy.read_text(encoding="utf-8") == ORIGINAL


def test_original_survives_what_ytdlp_does_to_the_copy(original, work_dir):
    """Ровно та защита, ради которой всё это существует."""
    copy = working_cookie_file(original, work_dir=work_dir)
    copy.write_text("# yt-dlp выкинул половину\n", encoding="utf-8")

    assert original.read_text(encoding="utf-8") == ORIGINAL


def test_second_call_keeps_the_rotated_cookies(original, work_dir):
    """YouTube обновляет cookies в ответах — затирать копию значит терять их."""
    copy = working_cookie_file(original, work_dir=work_dir)
    copy.write_text("# обновлённые cookies\n", encoding="utf-8")

    again = working_cookie_file(original, work_dir=work_dir)

    assert again == copy
    assert again.read_text(encoding="utf-8") == "# обновлённые cookies\n"


def test_freshly_uploaded_original_replaces_the_copy(original, work_dir):
    """Админ загрузил новый файл — рабочая копия обязана уступить."""
    copy = working_cookie_file(original, work_dir=work_dir)
    copy.write_text("# устаревшее\n", encoding="utf-8")

    original.write_text("# новый набор от админа\n", encoding="utf-8")
    os.utime(original, (copy.stat().st_mtime + 10, copy.stat().st_mtime + 10))

    again = working_cookie_file(original, work_dir=work_dir)

    assert again.read_text(encoding="utf-8") == "# новый набор от админа\n"
    assert again != copy


def test_new_original_with_older_mtime_uses_new_copy(original, work_dir):
    old = working_cookie_file(original, work_dir=work_dir)
    old.write_text("# загрузка еще работает\n", encoding="utf-8")
    old_mtime = original.stat().st_mtime
    original.write_text("# новые cookies\n", encoding="utf-8")
    os.utime(original, (old_mtime, old_mtime))

    current = working_cookie_file(original, work_dir=work_dir)

    assert current != old
    assert current.read_text(encoding="utf-8") == "# новые cookies\n"
    assert old.read_text(encoding="utf-8") == "# загрузка еще работает\n"


def test_copy_is_readable_only_by_owner(original, work_dir):
    """В файле живая сессия аккаунта — режим обязан совпадать с оригиналом."""
    copy = working_cookie_file(original, work_dir=work_dir)

    assert copy.stat().st_mode & 0o077 == 0


def test_fresh_restores_the_copy_of_the_same_file(original, work_dir):
    """Имя копии зависит от хэша: без `fresh` админ получил бы её же, уже испорченную."""
    copy = working_cookie_file(original, work_dir=work_dir)
    copy.write_text("# yt-dlp выкинул половину\n", encoding="utf-8")

    again = working_cookie_file(original, work_dir=work_dir, fresh=True)

    assert again == copy
    assert again.read_text(encoding="utf-8") == ORIGINAL
    assert [item.name for item in work_dir.iterdir()] == [copy.name]


def test_fresh_creates_a_missing_copy(original, work_dir):
    copy = working_cookie_file(original, work_dir=work_dir, fresh=True)

    assert copy.read_text(encoding="utf-8") == ORIGINAL


def test_fresh_keeps_the_whole_old_copy_until_the_swap(original, work_dir, monkeypatch):
    """Загрузчики читают копию параллельно и не должны увидеть её недописанной."""
    copy = working_cookie_file(original, work_dir=work_dir)
    copy.write_text("# старая копия\n", encoding="utf-8")
    seen = []
    really_replace = os.replace

    def spy(source, target):
        seen.append((copy.read_text(encoding="utf-8"), open(source, encoding="utf-8").read()))
        really_replace(source, target)

    monkeypatch.setattr(os, "replace", spy)
    working_cookie_file(original, work_dir=work_dir, fresh=True)

    assert seen == [("# старая копия\n", ORIGINAL)]


def test_refused_fresh_keeps_the_old_copy_and_uses_the_original(
    original, work_dir, monkeypatch, caplog
):
    """Так Windows отвечает на замену файла, открытого другим потоком."""
    copy = working_cookie_file(original, work_dir=work_dir)
    copy.write_text("# старая копия\n", encoding="utf-8")

    def refuse(source, target):
        raise PermissionError("файл занят")

    monkeypatch.setattr(os, "replace", refuse)

    assert working_cookie_file(original, work_dir=work_dir, fresh=True) == original
    assert copy.read_text(encoding="utf-8") == "# старая копия\n"
    assert [item.name for item in work_dir.iterdir()] == [copy.name]
    assert "файл занят" in caplog.text


@pytest.mark.skipif(os.name == "nt", reason="На Windows нет режимов POSIX")
def test_fresh_copy_is_readable_only_by_owner(original, work_dir):
    original.chmod(0o644)

    copy = working_cookie_file(original, work_dir=work_dir, fresh=True)

    assert copy.stat().st_mode & 0o777 == 0o600


def test_cleanup_preserves_current_copy_and_removes_replaced_copy(original, tmp_path, monkeypatch):
    import config

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config, "YOUTUBE_COOKIES_PATH", original)
    monkeypatch.setattr(config, "INSTAGRAM_COOKIES_PATH", tmp_path / "missing-ig")
    monkeypatch.setattr(config, "TIKTOK_COOKIES_PATH", tmp_path / "missing-tt")

    current = working_cookie_file(original)
    current.write_text("# обновленная рабочая сессия\n", encoding="utf-8")
    old_time = current.stat().st_mtime - 2 * 24 * 60 * 60
    os.utime(current, (old_time, old_time))

    assert cleanup_old_workfiles() == (0, 0)
    assert current.read_text(encoding="utf-8") == "# обновленная рабочая сессия\n"

    original.write_text("# новый набор\n", encoding="utf-8")
    replacement = working_cookie_file(original)
    assert replacement != current
    assert cleanup_old_workfiles() == (1, 0)
    assert not current.exists()
    assert replacement.exists()


# ── Атомарная запись ──────────────────────────────────────────────


@pytest.fixture
def jar(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text(SAVED_BY_YT_DLP, encoding="utf-8")
    loaded = yt_dlp.cookies.load_cookies(str(path), None, None)
    return loaded, path


def test_yt_dlp_creates_the_atomic_jar(tmp_path):
    """Падает, если yt-dlp переименует класс или перестанет брать его по имени."""
    path = tmp_path / "cookies.txt"
    path.write_text(SAVED_BY_YT_DLP, encoding="utf-8")

    with yt_dlp.YoutubeDL({"cookiefile": str(path), "quiet": True}) as ydl:
        assert type(ydl.cookiejar).__name__ == "AtomicCookieJar"
        assert len(ydl.cookiejar) == 2

    assert "SID" in path.read_text(encoding="utf-8")
    assert [item.name for item in tmp_path.iterdir()] == ["cookies.txt"]


def test_reader_sees_the_whole_old_file_while_saving(jar, monkeypatch):
    loaded, path = jar
    before = path.read_bytes()
    seen = []
    really_save = type(loaded)._really_save

    def spy(self, handle, *args):
        seen.append(path.read_bytes())
        return really_save(self, handle, *args)

    monkeypatch.setattr(type(loaded), "_really_save", spy)
    loaded.save()

    assert seen == [before]


def test_failed_save_keeps_the_file_and_leaves_no_temporary(jar, monkeypatch):
    loaded, path = jar
    before = path.read_bytes()

    def fail(self, handle, *args):
        handle.write("# обрыв")
        raise OSError("диск заполнен")

    monkeypatch.setattr(type(loaded), "_really_save", fail)
    with pytest.raises(OSError, match="диск заполнен"):
        loaded.save()

    assert path.read_bytes() == before
    assert [item.name for item in path.parent.iterdir()] == ["cookies.txt"]


def test_refused_replace_skips_the_save(jar, monkeypatch, caplog):
    """Так Windows отвечает на замену файла, открытого другим потоком."""
    loaded, path = jar
    before = path.read_bytes()

    def refuse(source, target):
        raise PermissionError("файл занят")

    monkeypatch.setattr(os, "replace", refuse)
    loaded.save()

    assert path.read_bytes() == before
    assert [item.name for item in path.parent.iterdir()] == ["cookies.txt"]
    assert "файл занят" in caplog.text


@pytest.mark.skipif(os.name == "nt", reason="На Windows нет режимов POSIX")
def test_saved_file_stays_private(jar):
    loaded, path = jar
    path.chmod(0o644)
    loaded.save()
    assert path.stat().st_mode & 0o777 == 0o600
