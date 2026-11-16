"""Tests for the pull-complete chime (fyrn/chime.py). The WAV
generation is pure stdlib; the playback test only asserts the
honest no-device behaviour (no audio backend here, so
play_chime returns False instead of crashing). The actual sound
is verifiable only on a machine with audio."""

import io
import unittest
import wave

from fyrn.chime import (AMPLITUDE, GAP_SECONDS, NOTES, SAMPLE_RATE,
                        chime_wav_bytes, play_chime)

_app = None


def setUpModule():
    global _app
    from PySide6.QtWidgets import QApplication
    _app = QApplication.instance()
    if _app is None:
        _app = QApplication([])


class ChimeWavTests(unittest.TestCase):
    def test_wav_is_valid_and_the_right_shape(self):
        data = chime_wav_bytes()
        with wave.open(io.BytesIO(data), "rb") as wav:
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getsampwidth(), 2)
            self.assertEqual(wav.getframerate(), SAMPLE_RATE)
            # Per segment, as the generator counts them (each
            # int() truncates, so the total is 2 frames short of
            # the naive 0.4 s product — 0.09 ms, inaudible).
            expected = sum(
                int(SAMPLE_RATE * seconds)
                + int(SAMPLE_RATE * GAP_SECONDS)
                for _freq, seconds in NOTES)
            self.assertEqual(wav.getnframes(), expected)
            frames = wav.readframes(wav.getnframes())
        # Not silence: the loudest sample is near the amplitude.
        loudest = 0
        for index in range(0, len(frames), 2):
            value = int.from_bytes(frames[index:index + 2],
                                   "little", signed=True)
            loudest = max(loudest, abs(value))
        self.assertGreater(loudest, int(AMPLITUDE * 32767 * 0.9))

    def test_second_note_is_higher_than_the_first(self):
        # Zero crossings rise with pitch: the second note
        # (880 Hz) crosses more often than the first (659 Hz).
        data = chime_wav_bytes()
        with wave.open(io.BytesIO(data), "rb") as wav:
            frames = wav.readframes(wav.getnframes())
        samples = [int.from_bytes(frames[i:i + 2], "little",
                                  signed=True)
                   for i in range(0, len(frames), 2)]
        first_len = int(SAMPLE_RATE * NOTES[0][1])
        gap_len = int(SAMPLE_RATE * GAP_SECONDS)

        def crossings(values):
            count = 0
            for before, after in zip(values, values[1:]):
                if (before < 0) != (after < 0):
                    count += 1
            return count

        first = samples[:first_len]
        second = samples[first_len + gap_len:
                         first_len + gap_len
                         + int(SAMPLE_RATE * NOTES[1][1])]
        self.assertGreater(crossings(second), crossings(first))


class ChimePlayTests(unittest.TestCase):
    def test_play_chime_matches_the_hardware(self):
        # No audio device here: play_chime returns False (and
        # never raises, let alone crashes) rather than starting
        # a player with nowhere to play. On a machine with audio
        # it returns True; either way the return matches whether
        # an output device exists.
        from PySide6.QtMultimedia import QMediaDevices
        expected = bool(QMediaDevices.audioOutputs())
        self.assertIs(play_chime(), expected)


if __name__ == "__main__":
    unittest.main()
