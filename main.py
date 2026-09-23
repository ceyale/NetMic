#!/usr/bin/env python3
"""Entry point: python gui_client/main.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gui_client.main_gui import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())