# -*- coding: utf-8 -*-
"""HTML を PDF にする（ヘッドレス Chromium）。

    python html_to_pdf.py 入力.html 出力.pdf

CSS の @page / @media print をそのまま効かせたいので、印刷経路は
ブラウザに任せる。背景色（ヒートマップのセル）を落とさないよう
print_background=True で出す。
"""
import asyncio
import os
import sys

from playwright.async_api import async_playwright


async def render(src, dst):
    url = 'file://' + os.path.abspath(src)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(url, wait_until='networkidle')
        await page.emulate_media(media='print')          # @media print を適用
        await page.pdf(path=dst,
                       print_background=True,            # セルの色分けを残す
                       prefer_css_page_size=True)        # @page の A4 landscape を使う
        await browser.close()


if __name__ == '__main__':
    src = sys.argv[1] if len(sys.argv) > 1 else 'passive_table.html'
    dst = sys.argv[2] if len(sys.argv) > 2 else os.path.splitext(src)[0] + '.pdf'
    asyncio.run(render(src, dst))
    print(f'{dst}: {os.path.getsize(dst):,} bytes')
