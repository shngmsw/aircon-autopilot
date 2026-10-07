"""pytest 実行時にリポジトリ直下を import パスに入れるための設定。

pytest の実行ディレクトリやランナーによってはリポジトリ直下が import パスに
入らないことがあるため、tests/ から `from app import logic` が確実に動くようにする。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolate_jev(monkeypatch):
    """注入された実キーがあっても既存テストから外部APIを呼ばない。"""
    from app import config
    monkeypatch.setattr(config, "JEV_MODE", "off")
    monkeypatch.setattr(config, "TYPESAFE_API_KEY", "")
