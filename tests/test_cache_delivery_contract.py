"""Проверяет исправленные контракты кеша и отправок на настоящей SQLite."""

import asyncio
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import telegram

from utils import analytics_db, telegram_utils as delivery
from utils.artifact_cache import Artifact, artifact_from_message, canonical_source, recipe_for
from utils.cache_access import cache_call
from utils.cache_policy import cleanup_cache, get_cache_policy, set_cache_policy
from utils.db_worker import run_db
from utils.delivery_journal import DeliveryJournal
from utils.video_cache import TelegramVideoCache, CachedVideo

pytestmark = pytest.mark.anyio


@pytest.fixture
def storage(tmp_path, monkeypatch):
    analytics_db.close_connection()
    monkeypatch.setattr(analytics_db, "_DB_PATH", tmp_path / "analytics.db")
    analytics_db.init_db()
    cache = TelegramVideoCache(tmp_path / "telegram_cache.db")
    monkeypatch.setattr(delivery, "telegram_cache", cache)
    journal = DeliveryJournal(tmp_path / "journal.db")
    monkeypatch.setattr(delivery, "journal", journal)
    yield cache, journal
    analytics_db.close_connection()


def media_message(kind="photo", index=1):
    message = SimpleNamespace(message_id=index, video=None, audio=None, document=None, photo=())
    item = SimpleNamespace(file_id=f"file-{index}", file_unique_id=f"unique-{index}", width=1280, height=720, file_size=42, duration=5)
    setattr(message, kind, (item,) if kind == "photo" else item)
    return message


def session():
    return {"_bot_id": 17, "_recipient_id": 123, "_operation_id": "test-operation",
            "platform": "instagram", "url": "https://www.instagram.com/p/example/", "session_id": "test-session"}


async def test_old_references_do_not_expire_and_disabled_job_keeps_them(storage):
    cache, _ = storage
    cache.set(CachedVideo("https://example.com/a", "file", "unique", "tiktok", "audio", datetime.now()))
    with cache._write_transaction() as connection:
        connection.execute("UPDATE video_cache SET cached_at=?", ((datetime.now() - timedelta(days=365)).isoformat(),))
    assert get_cache_policy()["enabled"] is False
    assert await run_db(cleanup_cache, cache) == 0
    assert await cache_call(cache.get, "https://example.com/a", format_id="audio") is not None


async def test_cleanup_preserves_active_shared_artifact_and_removes_only_old_aliases(storage):
    cache, _ = storage
    item = Artifact("same", "unique", "photo", 1920, 1080)
    cache.artifacts.put(17, "https://example.com/old", "recipe", [(item, "media")], {})
    cache.artifacts.put(17, "https://example.com/recent", "recipe", [(item, "media")], {})
    with cache.artifacts.connection() as connection:
        connection.execute("UPDATE manifests SET last_used=? WHERE source=?", (time.time() - 100 * 86400, "https://example.com/old"))
    await run_db(set_cache_policy, True, 90)
    assert await run_db(cleanup_cache, cache) == 1
    assert cache.artifacts.get(17, "https://example.com/old", "recipe") is None
    assert cache.artifacts.get(17, "https://example.com/recent", "recipe") is not None
    assert cache.artifacts.stats()["artifacts"] == 1
    # Отложенный запуск повторно читает выключенную политику.
    await run_db(set_cache_policy, False, 1)
    assert await run_db(cleanup_cache, cache) == 0
    assert get_cache_policy()["days"] == 1


def test_aliases_are_independent_and_bot_and_recipe_isolated(storage):
    cache, _ = storage
    item = Artifact("file", "same-unique", "document")
    cache.artifacts.put(17, "https://youtu.be/reference?si=one", "r1", [(item, "media")], {})
    hit = cache.artifacts.get(17, "https://www.youtube.com/watch?v=reference&si=two", "r1")
    assert hit["items"][0][0].kind == "document"
    assert cache.artifacts.stats()["artifact_aliases"] == 2
    assert cache.artifacts.get(18, "https://youtu.be/reference", "r1") is None
    assert cache.artifacts.get(17, "https://youtu.be/reference", "r2") is None
    cache.set(CachedVideo("a", "file", "unique", "youtube", "audio", datetime.now()))
    cache.set(CachedVideo("b", "file", "unique", "youtube", "audio", datetime.now()))
    assert cache.get("a", "audio") and cache.get("b", "audio")


def test_unknown_host_query_is_preserved():
    assert canonical_source("https://example.com/file?si=signature&utm_source=meaningful") == "https://example.com/file?si=signature&utm_source=meaningful"


def test_photo_keeps_largest_returned_dimensions():
    message = media_message()
    small = SimpleNamespace(file_id="small", file_unique_id="small", width=90, height=90)
    message.photo = (small, *message.photo)
    artifact = artifact_from_message(message)
    assert (artifact.file_id, artifact.width, artifact.height, artifact.kind) == ("file-1", 1280, 720, "photo")


@pytest.mark.parametrize("count", [1, 2, 10, 11, 21])
async def test_cached_album_preserves_order_grouping_and_related_audio(storage, count):
    cache, _ = storage
    data = session()
    items = [(Artifact(f"photo-{i}", f"u-{i}", "photo", 1000, 800), "media") for i in range(count)]
    items.append((Artifact("audio", "audio-unique", "audio"), "audio"))
    cache.artifacts.put(17, data["url"], recipe_for("instagram", "photo_post"), items, {})
    order = []
    groups = []

    async def single(**kwargs):
        kind = "photo" if "photo" in kwargs else "audio"
        order.append(kwargs[kind])
        return media_message(kind, len(order))

    async def album(**kwargs):
        group = kwargs["media"]
        groups.append(len(group))
        order.extend(item.media for item in group)
        return tuple(media_message("photo", index + 1) for index in range(len(group)))

    query = SimpleNamespace(message=SimpleNamespace(reply_photo=single, reply_audio=single, reply_media_group=album))
    assert await delivery._deliver_cached_post(query, data)
    assert order == [f"photo-{i}" for i in range(count)] + ["audio"]
    assert all(2 <= size <= 10 for size in groups)
    assert data["_delivery_progress"] == count + 1
    assert data["_audio_delivered"] is True


async def test_mixed_album_uses_actual_document_and_video_methods(storage):
    cache, _ = storage
    data = session()
    items = [(Artifact("photo", "p", "photo"), "media"), (Artifact("document", "d", "document"), "media"), (Artifact("video", "v", "video"), "media")]
    cache.artifacts.put(17, data["url"], recipe_for("instagram", "photo_post"), items, {})
    message = SimpleNamespace(**{f"reply_{kind}": AsyncMock(return_value=media_message(kind, index)) for index, kind in enumerate(("photo", "document", "video"), 1)})
    assert await delivery._deliver_cached_post(SimpleNamespace(message=message), data)
    message.reply_document.assert_awaited_once()
    assert message.reply_document.call_args.kwargs["document"] == "document"


async def test_partial_album_never_becomes_complete_hit(storage):
    cache, _ = storage
    data = session()
    data["video_info"] = {"_nuvio_instagram_images": ["a", "b"]}
    data["_media_messages"] = [media_message()]
    await delivery._cache_complete_post(data)
    assert cache.artifacts.get(17, data["url"], recipe_for("instagram", "photo_post")) is None


async def test_invalid_second_group_does_not_restart_confirmed_first_group(storage):
    cache, _ = storage
    data = session()
    items = [(Artifact(f"p-{index}", f"u-{index}", "photo"), "media") for index in range(11)]
    cache.artifacts.put(17, data["url"], recipe_for("instagram", "photo_post"), items, {})
    message = SimpleNamespace(reply_media_group=AsyncMock(return_value=tuple(media_message(index=i) for i in range(10))),
                              reply_photo=AsyncMock(side_effect=telegram.error.BadRequest("wrong file identifier")))
    with pytest.raises(telegram.error.BadRequest):
        await delivery._deliver_cached_post(SimpleNamespace(message=message), data)
    message.reply_media_group.assert_awaited_once()
    assert data["_delivery_progress"] == 10
    assert cache.artifacts.get(17, data["url"], recipe_for("instagram", "photo_post")) is None
    assert cache.artifacts.stats()["artifact_aliases"] == 1
    assert cache.artifacts.stats()["artifacts"] >= 11


async def test_cache_lock_wait_does_not_block_event_loop(storage):
    cache, _ = storage
    started = threading.Event()

    def hold_lock():
        with cache._write_transaction():
            started.set()
            time.sleep(.2)

    blocker = threading.Thread(target=hold_lock)
    blocker.start()
    assert await asyncio.to_thread(started.wait, 1)
    task = asyncio.create_task(cache_call(cache.artifacts.get, 17, "url", "recipe"))
    await asyncio.sleep(.03)
    assert not task.done()
    # Работа таймера при незавершенном SQLite ожидании является проверкой отзывчивости.
    assert await task is None
    blocker.join()


async def test_cache_failure_is_a_miss_and_does_not_hide_programming_errors():
    def broken():
        raise sqlite3.OperationalError("locked")

    assert await cache_call(broken) is None
    with pytest.raises(TypeError):
        await cache_call(lambda: 1 + None)


@pytest.mark.parametrize("kind", ["photo", "video", "audio", "document"])
async def test_confirmed_send_records_progress_before_optional_storage(storage, monkeypatch, kind):
    _, journal = storage
    data = session()
    message = media_message(kind)
    old_finish = journal.finish

    def finish(*args):
        assert data["_delivery_progress"] == 1
        old_finish(*args)

    monkeypatch.setattr(journal, "finish", finish)
    result = await delivery._call_telegram_with_retry_after(AsyncMock(return_value=message), data)
    assert result is message
    assert data["_first_media_message"] is message
    assert not journal.uncertain(data["_operation_id"])


@pytest.mark.parametrize("cause,expected_attempts,unknown", [(httpx.PoolTimeout("pool"), 2, False), (httpx.ReadTimeout("read"), 1, True)])
async def test_transport_evidence_controls_retry(storage, cause, expected_attempts, unknown):
    _, journal = storage
    data = session()
    error = telegram.error.NetworkError("backend failure")
    error.__cause__ = cause
    call = AsyncMock(side_effect=error)
    with pytest.raises(telegram.error.NetworkError):
        await delivery._call_telegram_with_retry_after(call, data)
    assert call.await_count == expected_attempts
    assert data["_delivery_outcome_unknown"] is unknown
    assert journal.uncertain(data["_operation_id"]) is unknown


async def test_pre_dispatch_journal_failure_prevents_send(storage, monkeypatch):
    _, journal = storage
    monkeypatch.setattr(journal, "begin", lambda *_: (_ for _ in ()).throw(sqlite3.OperationalError("full")))
    call = AsyncMock()
    with pytest.raises(sqlite3.OperationalError):
        await delivery._call_telegram_with_retry_after(call, session())
    call.assert_not_awaited()


async def test_post_response_journal_failure_retains_confirmed_evidence(storage, monkeypatch):
    _, journal = storage
    monkeypatch.setattr(journal, "finish", lambda *_: (_ for _ in ()).throw(sqlite3.OperationalError("full")))
    data = session()
    assert await delivery._call_telegram_with_retry_after(AsyncMock(return_value=media_message()), data)
    assert data["_delivery_progress"] == 1
    assert journal.recover() == 1
    assert journal.uncertain(data["_operation_id"])


def test_restart_retains_dispatch_uncertainty_and_confirmed_ids(storage):
    _, journal = storage
    journal.begin("unfinished", 1, 1, 123)
    journal.begin("finished", 1, 1, 123)
    journal.finish("finished", 1, 1, "sent", [17, 18])
    reader = DeliveryJournal(journal.path)
    assert reader.recover() == 1
    assert reader.recover() == 0
    assert reader.uncertain("unfinished")
    assert not reader.uncertain("finished")
    with reader.connection() as connection:
        assert connection.execute("SELECT messages FROM attempts WHERE operation='finished'").fetchone()[0] == "[17, 18]"


async def test_csi_double_tap_is_one_vote_even_after_new_connection(storage):
    token = await run_db(analytics_db.create_csi_poll, 123)
    await run_db(analytics_db.bind_csi_poll, token, 77)
    results = await asyncio.gather(*(run_db(analytics_db.save_csi_vote, 123, rating, token, 123, 77) for rating in (5, 9)))
    assert results[0][0] == results[1][0]
    assert sum(created for _, created in results) == 1
    with pytest.raises(ValueError):
        await run_db(analytics_db.save_csi_vote, 124, 8, token, 123, 77)
    assert analytics_db.get_csi_metrics()["total_responses"] == 1


def make_old_cache(path):
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("""CREATE TABLE video_cache (
        url TEXT NOT NULL, file_id TEXT NOT NULL, file_unique_id TEXT NOT NULL UNIQUE,
        platform TEXT NOT NULL, format_id TEXT NOT NULL, cached_at TEXT NOT NULL,
        file_size INTEGER, duration INTEGER, title TEXT, PRIMARY KEY(url, format_id))""")
    connection.execute("PRAGMA user_version=2")
    connection.execute("INSERT INTO video_cache VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL)",
                       ("original", "old-file", "old-unique", "youtube", "audio", datetime.now().isoformat()))
    connection.commit()
    return connection


def test_legacy_migration_has_consistent_backup_and_old_reader_works(tmp_path):
    path = tmp_path / "cache.db"
    source = make_old_cache(path)
    migrated = TelegramVideoCache(path)
    assert migrated.get("original", "audio").file_id == "old-file"
    backups = list(tmp_path.glob("*.bak"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("SELECT file_id FROM video_cache").fetchone()[0] == "old-file"
    # Прежний читатель SELECT и прежний писатель по позициям столбцов совместимы.
    with sqlite3.connect(path) as old_reader:
        assert old_reader.execute("SELECT * FROM video_cache WHERE url=? AND format_id=?", ("original", "audio")).fetchone()[1] == "old-file"
        old_reader.execute("INSERT OR REPLACE INTO video_cache VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL)",
                           ("new-legacy", "another", "another-unique", "youtube", "audio", datetime.now().isoformat()))
    assert migrated.get("new-legacy", "audio") is not None
    assert migrated.artifacts.get(17, "original", "recipe") is None
    source.close()


def test_interrupted_migration_rolls_back_original_schema(tmp_path, monkeypatch):
    path = tmp_path / "cache.db"
    source = make_old_cache(path)
    source.close()
    monkeypatch.setattr(TelegramVideoCache, "_drop_stale_video_documents", lambda *_: (_ for _ in ()).throw(RuntimeError("interrupted")))
    with pytest.raises(RuntimeError):
        TelegramVideoCache(path)
    with sqlite3.connect(path) as reader:
        assert reader.execute("SELECT file_id FROM video_cache").fetchone()[0] == "old-file"
        assert "UNIQUE" in reader.execute("SELECT sql FROM sqlite_master WHERE name='video_cache'").fetchone()[0]
        assert reader.execute("SELECT 1 FROM sqlite_master WHERE name='video_cache_legacy'").fetchone() is None


async def test_feedback_overlap_requires_reply_to_current_prompt(storage, monkeypatch):
    from utils import feedback
    from telegram.ext import ApplicationHandlerStop

    context = SimpleNamespace(user_data={})
    chat = SimpleNamespace(id=123, type="private")
    user = SimpleNamespace(id=123, username="test", first_name="Test")
    old_started = asyncio.Event()
    release_old = asyncio.Event()
    prompts = [SimpleNamespace(message_id=10, edit_text=AsyncMock()), SimpleNamespace(message_id=20, edit_text=AsyncMock())]

    async def old_reply(*_args, **_kwargs):
        old_started.set()
        await release_old.wait()
        return prompts[0]

    def update(message):
        return SimpleNamespace(effective_chat=chat, effective_user=user, effective_message=message)

    old = asyncio.create_task(feedback._start_form(update(SimpleNamespace(reply_text=old_reply)), context))
    await old_started.wait()
    await feedback._start_form(update(SimpleNamespace(reply_text=AsyncMock(return_value=prompts[1]))), context)
    release_old.set()
    await old
    prompts[0].edit_text.assert_awaited_once()
    saved = []
    monkeypatch.setattr(feedback, "save_feedback", lambda _user, text, **kwargs: saved.append((text, kwargs)) or 1)
    stale = SimpleNamespace(text="old answer", message_id=30, reply_to_message=prompts[0], reply_text=AsyncMock())
    with pytest.raises(ApplicationHandlerStop):
        await feedback._consume_feedback_text(update(stale), context)
    assert not saved
    current = SimpleNamespace(text="new answer", message_id=31, reply_to_message=prompts[1], reply_text=AsyncMock())
    with pytest.raises(ApplicationHandlerStop):
        await feedback._consume_feedback_text(update(current), context)
    assert len(saved) == 1 and saved[0][0] == "new answer"


async def test_cancel_after_intent_commit_does_not_dispatch(storage, monkeypatch):
    _, journal = storage
    data = session()
    original = journal.begin
    cancelled = False

    def begin(*args):
        nonlocal cancelled
        original(*args)
        cancelled = True

    monkeypatch.setattr(journal, "begin", begin)
    monkeypatch.setattr(delivery, "is_cancelled", lambda _session: cancelled)
    call = AsyncMock()
    from utils.cancellation import CancelledByUser
    with pytest.raises(CancelledByUser):
        await delivery._call_telegram_with_retry_after(call, data)
    call.assert_not_awaited()
    assert not journal.uncertain(data["_operation_id"])


async def test_queued_cleanup_reads_disabled_policy_after_lock_release(storage):
    cache, _ = storage
    cache.artifacts.put(17, "old", "recipe", [(Artifact("old", "u", "photo"), "media")], {})
    with cache.artifacts.connection() as connection:
        connection.execute("UPDATE manifests SET last_used=?", (time.time() - 100 * 86400,))
    await run_db(set_cache_policy, True, 1)
    connection = sqlite3.connect(analytics_db._DB_PATH)
    connection.execute("BEGIN IMMEDIATE")
    started = threading.Event()

    def cleanup():
        started.set()
        return cleanup_cache(cache)

    task = asyncio.create_task(run_db(cleanup))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        await asyncio.sleep(.03)
        assert not task.done()
        connection.execute("UPDATE settings SET value='false' WHERE key='cache_auto_cleanup_enabled'")
        connection.commit()
        assert await task == 0
        assert cache.artifacts.get(17, "old", "recipe") is not None
    finally:
        connection.close()
        await task


async def test_optional_cache_wait_does_not_occupy_authoritative_workers():
    started = threading.Event()
    release = threading.Event()

    def blocked_cache():
        started.set()
        release.wait(2)

    task = asyncio.create_task(cache_call(blocked_cache))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        assert await asyncio.wait_for(run_db(lambda: "access-checked"), .2) == "access-checked"
        assert not task.done()
    finally:
        release.set()
        await task


async def test_cancelled_rpc_keeps_unknown_outcome_and_recovery_evidence(storage):
    _, journal = storage
    data = session()
    started = asyncio.Event()

    async def unresolved_send():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(delivery._call_telegram_with_retry_after(unresolved_send, data))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert data["_delivery_outcome_unknown"] is True
    assert data["_delivery_request_in_flight"] is False
    assert journal.uncertain(data["_operation_id"])
    assert journal.recover() == 1


async def test_description_is_journaled_without_counting_it_as_media(storage):
    _, journal = storage
    data = session()
    message = SimpleNamespace(message_id=25, photo=(), video=None, audio=None, document=None)
    await delivery._call_telegram_with_retry_after(AsyncMock(return_value=message), data, track_delivery_outcome=False)
    assert data.get("_delivery_progress", 0) == 0
    assert not journal.uncertain(data["_operation_id"])
    with journal.connection() as connection:
        assert connection.execute("SELECT state, messages FROM attempts").fetchone() == ("sent", "[25]")


async def test_cancelled_preparation_stops_worker_before_cleanup(monkeypatch):
    from utils import work_budget
    from utils.cancellation import forget_cancellation

    started = threading.Event()
    finished = threading.Event()
    cleaned = threading.Event()
    session_id = "cancel-worker-contract"
    monkeypatch.setattr(delivery, "cleanup_temp_files", lambda _session_id: cleaned.set())

    def prepare():
        started.set()
        try:
            work_budget.pause(5)
        finally:
            finished.set()

    task = asyncio.create_task(delivery.run_blocking(prepare, session_id=session_id))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        delivery._cleanup_session_when_idle(session_id)
        assert await asyncio.to_thread(finished.wait, 1)
        assert await asyncio.to_thread(cleaned.wait, 1)
        assert session_id not in delivery.active_download_sessions()
    finally:
        forget_cancellation(session_id)


def test_unknown_signed_source_preserves_parameter_order_and_raw_encoding():
    source = "https://example.com/file?token=abc%2fdef&z=2&a=one%20two&a=three+four"
    assert canonical_source(source) == source
    reordered = "https://example.com/file?a=one%20two&a=three+four&z=2&token=abc%2fdef"
    assert canonical_source(source) != canonical_source(reordered)


async def test_actual_telegram_invalid_reference_response_invalidates_cached_manifest(storage):
    cache, _ = storage
    data = session()
    data['platform'] = 'instagram'
    item = Artifact('invalid-file-id', '', 'video')
    cache.artifacts.put(17, data['url'], recipe_for('instagram', 'direct_video'), [(item, 'media')], {})
    query = SimpleNamespace(message=SimpleNamespace(reply_video=AsyncMock(side_effect=telegram.error.BadRequest(
        'Wrong remote file identifier specified: wrong padding in the string'))))
    context_token = delivery._active_delivery_session.set(data)
    try:
        assert await delivery._deliver_cached_video(query, data['url'], 'direct_video') is False
    finally:
        delivery._active_delivery_session.reset(context_token)
    assert cache.artifacts.get(17, data['url'], recipe_for('instagram', 'direct_video')) is None
    assert cache.artifacts.stats()['artifact_aliases'] == 1
    assert not data.get('_cached_manifest_id')
    query.message.reply_video.assert_awaited_once()
