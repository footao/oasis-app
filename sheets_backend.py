# -*- coding: utf-8 -*-
"""
sheets_backend.py — ベットログの保存先を Google スプレッドシートにするためのバックエンド。

oasis_core.BetLog(store=...) に渡す `store` オブジェクトを提供する。
store は次の2メソッドだけ持てばよい:
    - read_df() -> pandas.DataFrame   （シート全体を読む。空ならヘッダだけ/空DF）
    - write_df(df)                    （シート全体を df で置き換える）

Community Cloud では st.secrets に資格情報とシート情報を入れておけば、
build_store_from_secrets(st.secrets) が SheetsStore を返す。
secrets が無い（ローカル等）場合は None を返すので、呼び出し側は
従来どおりローカルCSVにフォールバックできる。
"""
import pandas as pd

# BetLog と同じ列順（oasis_core.LOG_COLUMNS と一致させる）
LOG_COLUMNS = ['bet_id', 'time', 'race_id', 'combo', 'model_prob',
               'odds', 'stake', 'status', 'result', 'payout', 'pnl']


class SheetsStore:
    """1つのワークシートを CSV のように丸ごと読み書きするラッパ。

    BetLog は毎回シート全体を読み、全体を書き戻す（元のCSV実装と同じ挙動）。
    個人利用なら十分。複数人が同時に精算すると後勝ちで上書きされる点だけ注意。
    """

    def __init__(self, worksheet):
        self.ws = worksheet

    def read_df(self):
        values = self.ws.get_all_values()
        if not values:
            return pd.DataFrame(columns=LOG_COLUMNS)
        header = values[0]
        rows = values[1:]
        if not header:
            return pd.DataFrame(columns=LOG_COLUMNS)
        if not rows:
            return pd.DataFrame(columns=header)
        # 行ごとの列数のブレを吸収してから DataFrame 化
        width = len(header)
        norm = [(r + [''] * width)[:width] for r in rows]
        return pd.DataFrame(norm, columns=header)

    def write_df(self, df):
        header = list(df.columns) if len(df.columns) else LOG_COLUMNS
        body = df.fillna('').astype(object).values.tolist()
        self.ws.clear()
        # gspread 5.x / 6.x どちらでも動くようキーワード引数で呼ぶ
        self.ws.update(values=[header] + body, range_name='A1',
                       value_input_option='RAW')


def build_store_from_secrets(secrets, *, default_worksheet='bet_log'):
    """st.secrets から SheetsStore を構築。設定が無ければ None を返す。

    必要な secrets:
        [gcp_service_account]   … サービスアカウントJSONの中身
        [gsheets]
            spreadsheet_id  = "..."   （または spreadsheet_url）
            worksheet       = "bet_log"   （任意・既定 bet_log）
    """
    # サービスアカウントが無ければ Sheets モードにしない
    try:
        has_sa = 'gcp_service_account' in secrets
    except Exception:
        has_sa = False
    if not has_sa:
        return None

    import gspread
    from google.oauth2.service_account import Credentials

    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    info = dict(secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    gc = gspread.authorize(creds)

    cfg = {}
    try:
        if 'gsheets' in secrets:
            cfg = dict(secrets["gsheets"])
    except Exception:
        cfg = {}

    sheet_id = cfg.get("spreadsheet_id") or cfg.get("spreadsheet_key")
    sheet_url = cfg.get("spreadsheet_url")
    ws_name = cfg.get("worksheet", default_worksheet)

    if sheet_id:
        sh = gc.open_by_key(sheet_id)
    elif sheet_url:
        sh = gc.open_by_url(sheet_url)
    else:
        raise ValueError(
            "secrets の [gsheets] に spreadsheet_id か spreadsheet_url を入れてください。")

    try:
        ws = sh.worksheet(ws_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=ws_name, rows=2000, cols=len(LOG_COLUMNS))
        ws.update(values=[LOG_COLUMNS], range_name='A1', value_input_option='RAW')

    return SheetsStore(ws)
