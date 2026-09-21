"""hub/ 를 sys.path 에 — 테스트가 ``import server`` / ``import db`` 를 평탄하게 쓴다(배포 단위와 같은 모양)."""
import os
import sys

HUB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HUB_DIR not in sys.path:
    sys.path.insert(0, HUB_DIR)
