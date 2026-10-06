"""Типизированное ядро callback FSM и хранилище пользовательских сессий."""

from __future__ import annotations

import time
import uuid
import re
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CallbackEvent:
    """Разобранное событие inline-кнопки."""

    scope: str
    action: str
    session_token: str | None = None
    value: str | None = None

    @classmethod
    def parse(cls, data: str) -> CallbackEvent | None:
        parts = data.split("|")
        match parts:
            case ["s", token, "main", action] if token and action:
                return cls(scope="main", action=action, session_token=token)
            case ["s", token, "format", action, value] if token and action and value:
                return cls(
                    scope="format",
                    action=action,
                    session_token=token,
                    value=value,
                )
            case ["csi", token, rating] if re.fullmatch(r"[a-f0-9]{12}", token) and re.fullmatch(r"[0-9]{1,2}", rating) and 0 <= int(rating) <= 10:
                return cls(scope="csi", action="rate", session_token=token, value=rating)
            case ["csi", rating] if re.fullmatch(r"[0-9]{1,2}", rating) and 0 <= int(rating) <= 10:
                return cls(scope="csi", action="rate", value=rating)
            case ["feedback", "open", value] if value == "general" or re.fullmatch(r"(?:YT|TT|IG|RU|VK|TG|FILE|BOT)-[A-Z_]{1,8}-[A-F0-9]{6}", value):
                return cls(scope="feedback", action="open", value=value)
            case ["feedback", "cancel", token] if re.fullmatch(r"[a-f0-9]{12}", token):
                return cls(scope="feedback", action="cancel", value=token)
            case _:
                return None


class SessionStore:
    """Ограниченное хранилище FSM-сессий внутри ``context.user_data``."""

    def __init__(
        self,
        user_data: MutableMapping[str, Any],
        *,
        key: str = "sessions",
        max_active: int = 5,
        hard_limit: int = 25,
        token_factory: Callable[[], str] | None = None,
        is_disposable: Callable[[str], bool] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self._user_data = user_data
        self._key = key
        self._max_active = max_active
        self._hard_limit = hard_limit
        self._token_factory = token_factory or (lambda: uuid.uuid4().hex[:8])
        self._is_disposable = is_disposable or (lambda _session_id: True)
        self._clock = clock

    @property
    def data(self) -> dict[str, dict[str, Any]]:
        store = self._user_data.get(self._key)
        if not isinstance(store, dict):
            store = {}
            self._user_data[self._key] = store
        return store

    def create(
        self,
        *,
        url: str,
        video_info: dict,
        session_id: str,
        platform: str,
        formats: dict,
    ) -> str:
        store = self.data
        token = self._token_factory()
        while token in store:
            token = self._token_factory()

        store[token] = {
            "url": url,
            "video_info": video_info,
            "session_id": session_id,
            "platform": platform,
            "formats": formats,
            "created_at": self._clock(),
        }

        self._evict(store)
        return token

    def _evict(self, store: dict[str, dict[str, Any]]) -> None:
        """Освобождает место, не задевая сессии, которые ещё работают.

        Вытеснение НЕ удаляет временные файлы. Прежняя версия вызывала
        ``cleanup_temp_files`` для вытесненной сессии, и на проде это сносило
        каталог загрузки, которая шла прямо в этот момент: скачанный файл
        исчезал между готовностью и отправкой. Удаление файлов принадлежит
        владельцу работы, а не соседней сессии, которой понадобилось место.

        Занятые сессии пропускаются ещё и потому, что запись хранит
        ``session_id``: потеряв её, успешный путь не узнает, что удалять, и
        файл утечёт до перезапуска процесса.
        """

        def _age(token: str) -> float:
            return float(store[token].get("created_at", 0.0))

        while len(store) > self._max_active:
            disposable = [
                token
                for token in store
                if self._is_disposable(store[token].get("session_id"))
            ]
            if not disposable:
                break
            store.pop(min(disposable, key=_age))

        # Страховка от неограниченного роста, если занято вообще всё: утечка
        # одного каталога переживается уборкой при старте, а рост памяти — нет.
        while len(store) > self._hard_limit:
            store.pop(min(store, key=_age))

    def get(self, token: str) -> dict[str, Any] | None:
        return self.data.get(token)

    def remove(self, token: str) -> dict[str, Any] | None:
        return self.data.pop(token, None)
