# -*- coding: utf-8 -*-
"""
run_app.py — exe化したときの起動エントリ。
PyInstaller でバンドルし、内部で Streamlit サーバを起動して oasis_app.py を実行する。
（Streamlitはサーバ型なので、単純なスクリプトのexe化ではなくこのランチャ経由にする）
"""
import os
import sys


def _resource(rel):
    """PyInstallerバンドル時は _MEIPASS、通常実行時はこのファイルのフォルダを基準に解決。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def main():
    # 使用統計の確認プロンプトを抑止し、ブラウザを自動で開く
    os.environ.setdefault("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false")
    os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "false")

    app_path = _resource("oasis_app.py")
    sys.argv = [
        "streamlit", "run", app_path,
        "--server.headless=false",
        "--browser.gatherUsageStats=false",
    ]
    from streamlit.web import cli as stcli
    sys.exit(stcli.main())


if __name__ == "__main__":
    main()
