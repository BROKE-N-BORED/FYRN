"""Fyrn test package.

Run from the project folder: python3 -m unittest discover -s tests
"""

import os

# GUI tests need a platform plugin. Offscreen is the headless one;
# a real display is never required to run this suite.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
