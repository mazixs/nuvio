"""Чистые решения, связывающие пользовательские действия с платформой."""


DIRECT_VIDEO_CACHE_KEY = "direct_video"

# Платформы, у которых основная кнопка отдаёт единственный вариант видео,
# поэтому один ключ кэша на URL достаточен.
_DIRECT_VIDEO_PLATFORMS = frozenset({"tiktok", "instagram", "rutube", "vk"})


def cache_key_for_main_action(platform: str, action: str) -> str | None:
    """Возвращает ключ кэша для основной кнопки платформы."""
    video_actions = {f"{platform}_download"}
    if platform in {"tiktok", "instagram"}:
        video_actions.add(f"{platform}_download_desc")
    if platform in _DIRECT_VIDEO_PLATFORMS and action in video_actions:
        return DIRECT_VIDEO_CACHE_KEY
    if platform == "youtube" and action in {"tg_video", "audio_m4a"}:
        # Значения совпадают с ключами, под которыми записи уже лежат в кэше.
        return action
    if platform in _DIRECT_VIDEO_PLATFORMS and action == f"{platform}_audio":
        # Значение совпадает с ключами, под которыми записи уже лежат в кэше.
        return action
    return None


def cache_key_for_format_selection(
    content_type: str,
    format_id: str,
) -> str | None:
    """Возвращает ключ кэша для расширенного формата YouTube."""
    if content_type == "combined":
        return f"combined:{format_id}"
    return None
