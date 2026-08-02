# -*- coding: utf-8 -*-
"""
drive_backend.py — 学習用レースログを Google ドライブから読むためのバックエンド。

Streamlit Community Cloud はファイルを保存できない（再起動で消える）ので、
ログはドライブに置いておき、アプリ起動時に毎回ダウンロードして学習する。
ドライブ上のファイルを差し替えれば、次の再学習から新しいログが使われる。

認証は Google スプレッドシート（sheets_backend.py）と**同じサービスアカウント**を使う。
追加で必要なのは、ログを置いたフォルダ（かファイル）をそのサービスアカウントの
メールアドレスに共有しておくことだけ。

必要な secrets:
    [gcp_service_account]     … サービスアカウントJSONの中身（Sheetsと共用）
    [gdrive]
        folder_id = "..."     … ログを入れたフォルダのID（またはURL）
        # もしくは
        file_ids  = ["...", "..."]   … 個別のファイルID（またはURL）
        pattern   = ".txt"    … 任意。フォルダ内でこの文字列を含む名前だけ読む（既定 .txt）

Drive API は REST を直接叩くので google-api-python-client は不要
（google-auth と requests だけで動く）。
"""
from __future__ import annotations

import re

DRIVE_API = 'https://www.googleapis.com/drive/v3/files'
SCOPES = ['https://www.googleapis.com/auth/drive.readonly']
_ID_RE = re.compile(r'/(?:folders|d)/([A-Za-z0-9_-]{10,})')
_QID_RE = re.compile(r'[?&]id=([A-Za-z0-9_-]{10,})')


def extract_id(value):
    """ドライブのURLでもIDでも受け取れるようにする。"""
    v = str(value or '').strip()
    if not v:
        return None
    m = _ID_RE.search(v) or _QID_RE.search(v)
    return m.group(1) if m else v


class DriveLogSource:
    """ドライブ上のテキストログを読むだけの小さなクライアント。"""

    def __init__(self, credentials, folder_id=None, file_ids=None, pattern='.txt'):
        self.creds = credentials
        self.folder_id = extract_id(folder_id) if folder_id else None
        self.file_ids = [extract_id(x) for x in (file_ids or []) if extract_id(x)]
        self.pattern = pattern or ''

    # --- 内部 ---
    def _session(self):
        import requests
        from google.auth.transport.requests import Request
        if not self.creds.valid:
            self.creds.refresh(Request())
        s = requests.Session()
        s.headers.update({'Authorization': f'Bearer {self.creds.token}'})
        return s

    # --- 公開API ---
    def list_files(self):
        """[{id, name, modifiedTime, size}] を返す。差分検知（キャッシュ鍵）にも使う。"""
        s = self._session()
        out = []
        if self.folder_id:
            params = {
                'q': f"'{self.folder_id}' in parents and trashed=false",
                'fields': 'files(id,name,modifiedTime,size,mimeType)',
                'pageSize': 200,
                'supportsAllDrives': 'true',
                'includeItemsFromAllDrives': 'true',
                'orderBy': 'name',
            }
            r = s.get(DRIVE_API, params=params, timeout=30)
            r.raise_for_status()
            for f in r.json().get('files', []):
                if f.get('mimeType') == 'application/vnd.google-apps.folder':
                    continue
                if self.pattern and self.pattern not in f.get('name', ''):
                    continue
                out.append(f)
        for fid in self.file_ids:
            r = s.get(f'{DRIVE_API}/{fid}',
                      params={'fields': 'id,name,modifiedTime,size',
                              'supportsAllDrives': 'true'}, timeout=30)
            r.raise_for_status()
            out.append(r.json())
        # 同じファイルを二重に読まない
        seen, uniq = set(), []
        for f in out:
            if f['id'] not in seen:
                seen.add(f['id'])
                uniq.append(f)
        return uniq

    def fingerprint(self):
        """ファイル構成＋更新時刻の指紋。これが変わったら再ダウンロード・再学習する。"""
        return tuple(sorted((f['id'], f.get('modifiedTime', ''), str(f.get('size', '')))
                            for f in self.list_files()))

    def download_texts(self):
        """[(ファイル名, 本文), ...] を返す。"""
        s = self._session()
        out = []
        for f in self.list_files():
            r = s.get(f'{DRIVE_API}/{f["id"]}',
                      params={'alt': 'media', 'supportsAllDrives': 'true'}, timeout=120)
            r.raise_for_status()
            raw = r.content
            for enc in ('utf-8', 'utf-8-sig', 'cp932'):
                try:
                    out.append((f['name'], raw.decode(enc)))
                    break
                except UnicodeDecodeError:
                    continue
            else:
                out.append((f['name'], raw.decode('utf-8', errors='replace')))
        return out


def build_source_from_secrets(secrets):
    """st.secrets から DriveLogSource を作る。設定が無ければ None。"""
    try:
        if 'gcp_service_account' not in secrets or 'gdrive' not in secrets:
            return None
    except Exception:
        return None

    cfg = dict(secrets['gdrive'])
    folder = cfg.get('folder_id') or cfg.get('folder_url')
    files = cfg.get('file_ids') or cfg.get('file_id') or cfg.get('file_urls')
    if isinstance(files, str):
        files = [files]
    if not folder and not files:
        return None

    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        dict(secrets['gcp_service_account']), scopes=SCOPES)
    return DriveLogSource(creds, folder_id=folder, file_ids=files,
                          pattern=cfg.get('pattern', '.txt'))
