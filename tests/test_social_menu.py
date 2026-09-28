from utils import telegram_utils


def _buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


def test_social_menu_offers_explicit_description_choices_and_short_callbacks():
    for platform in ("tiktok", "instagram"):
        _text, markup = telegram_utils._build_main_menu(
            platform,
            {
                "title": "Recipe",
                "description": "1. Mix ingredients\n2. Bake",
                "uploader": "cook",
                "duration": 12,
            },
            "abc12345",
        )
        buttons = _buttons(markup)
        by_label = {button.text: button.callback_data for button in buttons}
        assert by_label[telegram_utils.BTN_DOWNLOAD_WITHOUT_DESCRIPTION].endswith(
            f"|{platform}_download"
        )
        assert by_label[telegram_utils.BTN_DOWNLOAD_WITH_DESCRIPTION].endswith(
            f"|{platform}_download_desc"
        )
        assert all(len(button.callback_data.encode("utf-8")) <= 64 for button in buttons)


def test_social_menu_distinguishes_empty_and_unavailable_description():
    empty_text, empty_menu = telegram_utils._build_main_menu(
        "instagram",
        {
            "title": "Clip",
            "description": "",
            "_nuvio_description_status": "empty",
        },
        "abc12345",
    )
    unavailable_text, unavailable_menu = telegram_utils._build_main_menu(
        "tiktok", {"title": "Clip"}, "abc12345"
    )

    assert telegram_utils.DESCRIPTION_EMPTY in empty_text
    assert telegram_utils.DESCRIPTION_UNAVAILABLE in unavailable_text
    for markup in (empty_menu, unavailable_menu):
        labels = [button.text for button in _buttons(markup)]
        assert telegram_utils.BTN_DOWNLOAD_WITH_DESCRIPTION not in labels


def test_long_tiktok_title_is_shortened_only_in_the_menu():
    source = "рецепт " * 80
    text, markup = telegram_utils._build_main_menu(
        "tiktok",
        {"title": source, "description": source, "uploader": "cook"},
        "abc12345",
    )

    assert len(text) < 500
    assert "рецепт" in text
    assert any(
        button.text == telegram_utils.BTN_DOWNLOAD_WITH_DESCRIPTION
        for button in _buttons(markup)
    )
