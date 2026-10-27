# -*- coding: utf-8 -*-
"""おあしすっち ダッシュボード（見るだけ・購入しない）。

    python3 oasis_dashboard.py            # http://127.0.0.1:8787 をブラウザで開く
    python3 oasis_dashboard.py --port 8800 --no-open
    python3 oasis_dashboard.py --local ..  # model.js / model.json を GitHub ではなく手元のリポジトリから読む（開発用）

- 計算は bot と同じ model.js（区間シミュレータ）と model.json を使う。既定では GitHub の main から取る
  （bot が使っているのと同じもの）。
- レースサイトの API への問い合わせは、このプログラムが代わりに行う（ブラウザから直接だと制限で弾かれるため）。
  通すのは読み取り3種類だけ（レース情報・3連単のオッズ・3連単のプール）。購入の API は通さない。
- トークンは使わない（読み取りの API には要らない）。届いても捨てる。
- このパソコンの中からしか開けない（127.0.0.1 だけで待つ）。Python 3 の標準機能だけで動く。
"""
import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
API = os.environ.get('OASIS_API', 'https://api.oasis.red').rstrip('/')
RAW = 'https://raw.githubusercontent.com/footao/oasis-app/main'
REMOTE = {'/model.js': RAW + '/bookmarklets/src/model.js', '/model.json': RAW + '/model.json'}
LOCAL = {'/model.js': 'bookmarklets/src/model.js', '/model.json': 'model.json'}
ALLOWED = ('/api/race/by-id/', '/api/trifecta/odds', '/api/trifecta/pool')   # 読み取りだけ
UA = 'oasis-dashboard/1.0'
CACHE_SEC = 60
_cache, _lock = {}, threading.Lock()
ARGS = None


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def model_file(path):
    """model.js / model.json。GitHub から取り、60秒は使い回す。取れなければ前回の分。"""
    if ARGS.local:
        with open(os.path.join(ARGS.local, LOCAL[path]), 'rb') as f:
            return f.read()
    with _lock:
        hit = _cache.get(path)
    if hit and time.time() - hit[0] < CACHE_SEC:
        return hit[1]
    try:
        _, body = _get(REMOTE[path] + f'?t={int(time.time())}')
        with _lock:
            _cache[path] = (time.time(), body)
        return body
    except Exception:
        if hit:
            return hit[1]
        raise


def safe_api_path(path_qs):
    """通してよい読み取りの API か。トークンが混ざっていたら外す。だめなら None。"""
    u = urllib.parse.urlsplit(path_qs)
    if not any(u.path.startswith(p) for p in ALLOWED) or '..' in u.path:
        return None
    q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True) if k.lower() != 'token']
    return u.path + ('?' + urllib.parse.urlencode(q) if q else '')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):      # URL（user 番号入り）を画面に出さない
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path in ('/', '/index.html'):
                with open(os.path.join(HERE, 'dashboard.html'), 'rb') as f:
                    return self._send(200, f.read(), 'text/html; charset=utf-8')
            if path in REMOTE:
                ctype = 'application/javascript; charset=utf-8' if path.endswith('.js') else 'application/json; charset=utf-8'
                return self._send(200, model_file(path), ctype)
            if path.startswith('/api/'):
                p = safe_api_path(self.path)
                if p is None:
                    return self._send(403, b'{"error":"not allowed"}', 'application/json')
                try:
                    code, body = _get(API + p)
                except urllib.error.HTTPError as e:
                    code, body = e.code, (e.read() or b'{}')
                return self._send(code, body, 'application/json; charset=utf-8')
            return self._send(404, b'not found', 'text/plain')
        except Exception as e:
            return self._send(502, json.dumps({'error': type(e).__name__}).encode(), 'application/json')

    def do_POST(self):                   # 購入などの書き込みは一切通さない
        self._send(405, b'{"error":"read only"}', 'application/json')


def selftest():
    assert safe_api_path('/api/race/by-id/1/2?user=3&token=SECRET') == '/api/race/by-id/1/2?user=3'
    assert safe_api_path('/api/trifecta/odds?guild=1&schedule_id=2&first=3&second=4&third=5').startswith('/api/trifecta/odds?')
    assert safe_api_path('/api/bet') is None and safe_api_path('/api/balance?user=1') is None
    assert safe_api_path('/api/race/by-id/../bet') is None
    print('selftest OK')


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8787)
    ap.add_argument('--no-open', action='store_true')
    ap.add_argument('--local', help='model.js / model.json を読むリポジトリのフォルダ（開発用）')
    ap.add_argument('--test', action='store_true')
    ARGS = ap.parse_args()
    if ARGS.test:
        return selftest()
    srv = ThreadingHTTPServer(('127.0.0.1', ARGS.port), Handler)
    url = f'http://127.0.0.1:{ARGS.port}/'
    print(f'おあしすっち ダッシュボード: {url}  （終了は Ctrl+C）')
    if not ARGS.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    sys.exit(main())
