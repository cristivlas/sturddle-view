"""fit_to_screen: the window and its minimum never outgrow the screen."""
from __future__ import annotations

from types import SimpleNamespace

from sturddle_view.desktop import _MIN_WINDOW_HEIGHT, _MIN_WINDOW_WIDTH, _SCREEN_FIT, fit_to_screen

REQUESTED = (1280, 1000)


def test_no_screen_keeps_requested_size_and_minimum():
    assert fit_to_screen(*REQUESTED, None) == (REQUESTED, (_MIN_WINDOW_WIDTH, _MIN_WINDOW_HEIGHT))


def test_large_screen_keeps_requested_size_and_minimum():
    screen = SimpleNamespace(width=2560, height=1440)
    assert fit_to_screen(*REQUESTED, screen) == (REQUESTED, (_MIN_WINDOW_WIDTH, _MIN_WINDOW_HEIGHT))


def test_small_screen_caps_size_and_minimum():
    screen = SimpleNamespace(width=1024, height=768)
    cap = (int(1024 * _SCREEN_FIT), int(768 * _SCREEN_FIT))
    assert fit_to_screen(*REQUESTED, screen) == (cap, (min(_MIN_WINDOW_WIDTH, cap[0]), cap[1]))


def test_small_request_keeps_screen_based_minimum():
    screen = SimpleNamespace(width=2560, height=1440)
    assert fit_to_screen(800, 600, screen) == ((800, 600), (_MIN_WINDOW_WIDTH, _MIN_WINDOW_HEIGHT))
