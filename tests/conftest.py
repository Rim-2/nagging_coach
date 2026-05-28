"""pytest 공용 설정 — repo 루트를 sys.path 에 추가해 store/tracker/... 를
테스트에서 바로 import 할 수 있게."""

from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
