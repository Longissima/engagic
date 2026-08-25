"""Logging configuration contracts."""

import logging

import structlog

import config as config_module


def test_development_renderer_owns_exception_formatting(monkeypatch) -> None:
    configured = {}

    monkeypatch.setattr(
        config_module.structlog,
        "configure",
        lambda **kwargs: configured.update(kwargs),
    )
    monkeypatch.setattr(logging, "basicConfig", lambda **_kwargs: None)

    config_module.configure_structlog(is_development=True)

    processors = configured["processors"]
    assert structlog.processors.format_exc_info not in processors
    assert isinstance(processors[-1], structlog.dev.ConsoleRenderer)
