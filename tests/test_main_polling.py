#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты классификации ошибок polling-цикла в main.py."""

import logging

import telegram

import main


def test_polling_error_callback_logs_network_error_as_warning(caplog):
    error = telegram.error.NetworkError("httpx.RemoteProtocolError: peer disconnected")

    with caplog.at_level(logging.WARNING):
        main._polling_error_callback(error)

    assert "REMOTE_DISCONNECT" in caplog.text
    assert "перехватил polling" in caplog.text


def test_polling_error_callback_logs_other_errors_as_warning(caplog):
    error = telegram.error.TelegramError("generic telegram error")

    with caplog.at_level(logging.WARNING):
        main._polling_error_callback(error)

    assert "UNEXPECT" in caplog.text
    assert "Неожиданная ошибка при long polling Bot API" in caplog.text


def test_classify_polling_error_detects_conflict():
    category, summary = main._classify_polling_error(
        telegram.error.Conflict("terminated by other getUpdates request")
    )

    assert category == "POLLING_CONFLICT"
    assert "Параллельный polling" in summary


def test_polling_bad_request_is_api_error_not_network_error():
    category, _summary = main._classify_polling_error(
        telegram.error.BadRequest("chat not found")
    )
    assert category == "API"


def test_prepare_runtime_storage_removes_only_stale_media(monkeypatch):
    cleanup_calls = []
    monkeypatch.setattr(
        main,
        "cleanup_stale_temp_files",
        lambda: (cleanup_calls.append("stale") or (1, 0)),
    )

    main._prepare_runtime_storage()

    assert cleanup_calls == ["stale"]
