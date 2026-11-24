#!/usr/bin/env python3
"""Entry point:  python wae_intel.py [config.toml] [--check] [--inspect-db]"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from waeintel.app import cli  # noqa: E402

if __name__ == "__main__":
    cli()
