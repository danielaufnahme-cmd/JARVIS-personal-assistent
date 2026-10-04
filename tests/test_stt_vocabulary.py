"""Whisper mishearings of the user's app names are fixed before the agent sees them (2026-09-28)."""

import pytest

from jarvis.stt import contact_prompt, fix_vocabulary


@pytest.mark.parametrize("heard,fixed", [
    ("Open near him.", "Open Neovim."),
    ("Open knee of him.", "Open Neovim."),
    ("Go to the fifth desktop and open near them.", "Go to the fifth desktop and open Neovim."),
    ("JARVIS, open near Vim.", "JARVIS, open Neovim."),
    ("Open, near them.", "Open, Neovim."),
    ("open my code editor near him", "open my code editor Neovim"),
    ("Open NeoVim.", "Open Neovim."),
])
def test_neovim_mishearings(heard, fixed):
    assert fix_vocabulary(heard) == fixed


@pytest.mark.parametrize("text", ["I was sitting near him yesterday.", "The shop is near them.", "What time is it?"])
def test_ordinary_speech_is_left_alone(text):
    assert fix_vocabulary(text) == text


def test_prompt_names_the_apps_and_not_messaging():
    prompt = contact_prompt()
    assert "Neovim" in prompt and "text message" not in prompt
