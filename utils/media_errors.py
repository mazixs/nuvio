"""Ожидаемые ограничения обработки медиа."""


class DurationLimitError(ValueError):
    """Длительность видео превышает установленный лимит бота."""

    def __init__(self, duration: float, max_duration: int):
        self.duration = duration
        self.max_duration = max_duration
        super().__init__(
            f"Видео слишком длинное. Максимальная длительность: {max_duration // 60} минут."
        )
