"""The pull-complete chime: two soft notes, generated in code.

No bundled sound file, no new dependency: the notes are written
as a WAV with the stdlib (wave + math) and played through
QtMultimedia, which ships in the pinned PySide6-Essentials.
If QtMultimedia is unavailable, play_chime returns False and the
caller falls back to the visual completion signal (the status
fade + tray message) — never a fake chime.
"""

import io
import math
import wave

# The sound: two gentle sine notes, E5 then A5, 0.15 s each with
# a short gap, each fading in and out so there is no click. Quiet
# by design (a finished download is good news, not an alarm).
SAMPLE_RATE = 22050
NOTES = ((659.25, 0.15), (880.00, 0.15))
GAP_SECONDS = 0.05
AMPLITUDE = 0.25

# The player, audio output, and buffer for the chime now playing:
# kept alive at module level, because a QMediaPlayer that is
# garbage-collected mid-play stops dead. Cleared when the chime
# ends (or errors), and replaced if another chime starts first.
_active = {}


def chime_wav_bytes():
    """The chime as WAV bytes: 16-bit mono at 22050 Hz."""
    frames = bytearray()
    for frequency, seconds in NOTES:
        count = int(SAMPLE_RATE * seconds)
        for index in range(count):
            # A raised-cosine envelope: 10 ms up, 10 ms down.
            fade = int(SAMPLE_RATE * 0.01)
            if index < fade:
                envelope = 0.5 - 0.5 * math.cos(
                    math.pi * index / fade)
            elif index > count - fade:
                envelope = 0.5 - 0.5 * math.cos(
                    math.pi * (count - index) / fade)
            else:
                envelope = 1.0
            sample = AMPLITUDE * envelope * math.sin(
                2.0 * math.pi * frequency * index / SAMPLE_RATE)
            value = int(sample * 32767)
            frames += value.to_bytes(2, "little", signed=True)
        gap = int(SAMPLE_RATE * GAP_SECONDS)
        frames += b"\x00\x00" * gap
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(bytes(frames))
    return buffer.getvalue()


def _clear_active():
    _active.clear()


def play_chime():
    """Play the chime. True when the player started, False when
    there is no sound backend to play through (QtMultimedia
    missing, no audio output device, or the player could not be
    created). The device check comes first and is a hard gate:
    starting a player with no audio device segfaults the
    process (proven in the sandbox, 2026-11-16), and a chime
    must never be able to crash the app. The caller always
    shows the visual signal too."""
    try:
        from PySide6.QtCore import QBuffer, QCoreApplication, QIODevice
        from PySide6.QtMultimedia import (QAudioOutput,
                                          QMediaDevices, QMediaPlayer)
    except ImportError:
        return False
    if QCoreApplication.instance() is None:
        return False
    try:
        if not QMediaDevices.audioOutputs():
            return False
    except Exception:
        return False
    try:
        player = QMediaPlayer()
        audio = QAudioOutput()
        player.setAudioOutput(audio)
        data = QBuffer()
        data.setData(bytes(chime_wav_bytes()))
        data.open(QIODevice.OpenModeFlag.ReadOnly)
        player.setSourceDevice(data)

        def _on_status(status):
            # Only the end (or a bad file) releases the player:
            # the signal also fires for loading and buffering,
            # and clearing there would stop the chime dead.
            if status in (QMediaPlayer.MediaStatus.EndOfMedia,
                          QMediaPlayer.MediaStatus.InvalidMedia):
                _clear_active()

        player.mediaStatusChanged.connect(_on_status)
        _active["player"] = player
        _active["audio"] = audio
        _active["buffer"] = data
        player.play()
    except Exception:
        _clear_active()
        return False
    return True
