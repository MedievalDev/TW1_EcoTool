r"""Decompiler for TW1 EarthC .eco files.

Taken from Desktop\TwStuff\EcoAnalysis_27_07_2026\tools (same author, July 2026). The modules import each
other by plain name, so this package puts its own folder on sys.path. Data tables live in data\.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
