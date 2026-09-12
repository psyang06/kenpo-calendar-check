#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
its-kenpo 施設予約系統 - 空缺監控腳本

功能：
1. 開啟行事曆頁面
2. 只看「金（週五）」「土（週六）」兩欄
3. 若該格有「〇」記號，點進去看是哪個設施有空
4. 用 ntfy.sh 推播通知
5. 用 seen.json 記錄已經通知過的項目，避免重複通知同一個空缺

環境變數（在 GitHub Actions 的 Secrets / Variables 設定）：
  TARGET_URL   - 要監控的行事曆網址（可能會過期，過期時到網站重新產生連結後更新這個變數）
  NTFY_TOPIC   - ntfy.sh 的 topic 名稱（例如 seiyou-kenpo-2026）
"""

import json
import os
import sys
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

TARGET_URL = os.environ.get("TARGET_URL", "").strip()
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()

SEEN_FILE = Path(__file__).parent / "seen.json"
TARGET_WEEKDAYS = {"金", "土"}  # 只看週五、週六


def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def save_seen(seen: set) -> None:
    SEEN_FILE.write_text(
        json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def notify_ntfy(title: str, message: str) -> None:
    if not NTFY_TOPIC:
        print("[跳過] 未設定 NTFY_TOPIC，略過推播通知")
        return
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title.encode("utf-8"), "Priority": "high"},
            timeout=15,
        )
    except Exception as e:
        print(f"[錯誤] ntfy 推播失敗: {e}")


def find_available_slots(page) -> list[dict]:
    """
    掃描行事曆表格，找出週五、週六且標記為「〇」的格子。
    回傳格式: [{"date": "9/18(金)", "weekday": "金", "cell_text": "〇", "link": "..."}]

    ⚠️ 這一段是根據常見的日本設施預約系統版面猜測寫的，
    實際上線後第一次執行請看 Actions 的 log 或 debug 截圖，
    確認選擇器（selector）是否對應正確，需要的話再調整。
    """
    results = []

    page.wait_for_load_state("networkidle")

    # 嘗試找出行事曆表格
    tables = page.query_selector_all("table")
    for table in tables:
        header_cells = table.query_selector_all("thead th, tr:first-child th")
        weekday_index = {}
        for idx, th in enumerate(header_cells):
            text = th.inner_text().strip()
            for wd in TARGET_WEEKDAYS:
                if wd in text:
                    weekday_index[idx] = wd

        if not weekday_index:
            continue  # 這個 table 不是行事曆表格，跳過

        rows = table.query_selector_all("tbody tr")
        for row in rows:
            cells = row.query_selector_all("td")
            for idx, wd in weekday_index.items():
                if idx >= len(cells):
                    continue
                cell = cells[idx]
                cell_text = cell.inner_text().strip()

                # 判斷是否為「有空」標記：文字是〇，或是格子內有 alt="○" 的圖片
                is_available = "〇" in cell_text or "○" in cell_text
                if not is_available:
                    img = cell.query_selector("img")
                    if img:
                        alt = (img.get_attribute("alt") or "").strip()
                        if alt in ("〇", "○"):
                            is_available = True

                if is_available:
                    link_el = cell.query_selector("a")
                    href = link_el.get_attribute("href") if link_el else None
                    results.append(
                        {
                            "weekday": wd,
                            "cell_text": cell_text,
                            "href": href,
                        }
                    )
    return results


def get_facility_detail(page, href: str) -> str:
    """點進空缺格子的連結，抓取設施名稱等細節文字。"""
    if not href:
        return "(沒有可點擊的連結，需人工確認)"
    try:
        with page.expect_navigation(timeout=15000):
            page.click(f'a[href="{href}"]')
    except Exception:
        # 有些連結是 JS 觸發，直接 goto 備援
        try:
            page.goto(href, timeout=15000)
        except Exception as e:
            return f"(無法開啟詳細頁面: {e})"

    page.wait_for_load_state("networkidle")
    # 嘗試抓「設施」相關文字：這裡先抓整個 body 的可見文字，之後可再收斂範圍
    body_text = page.inner_text("body")
    return body_text[:500]  # 先截前 500 字，避免通知內容過長


def main() -> None:
    if not TARGET_URL:
        print("[錯誤] 未設定 TARGET_URL")
        sys.exit(1)

    seen = load_seen()
    new_findings = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(TARGET_URL, timeout=30000)

        slots = find_available_slots(page)
        print(f"掃到 {len(slots)} 個週五/週六的〇空缺格")

        if len(slots) == 0:
            table_count = len(page.query_selector_all("table"))
            if table_count == 0:
                # 完全沒有 table，很可能是網站有公告訊息（例如抽籤期間暫停查詢）
                body_text = page.inner_text("body")
                print("[提示] 頁面上沒有偵測到任何表格，可能是網站有公告訊息，內容如下：")
                print(body_text[:800])

        for slot in slots:
            key = f"{slot['weekday']}|{slot.get('href')}"
            if key in seen:
                continue  # 已經通知過，跳過

            detail = get_facility_detail(page, slot.get("href"))
            page.go_back()
            page.wait_for_load_state("networkidle")

            new_findings.append({**slot, "detail": detail})
            seen.add(key)

        browser.close()

    if new_findings:
        title = f"發現 {len(new_findings)} 個週五/週六空缺！"
        lines = []
        for f in new_findings:
            lines.append(f"[{f['weekday']}] {f['cell_text']}\n{f['detail']}\n連結: {f.get('href')}\n---")
        message = "\n".join(lines)

        print(title)
        print(message)

        notify_ntfy(title, message)

        save_seen(seen)
    else:
        print("沒有新的空缺。")


if __name__ == "__main__":
    main()
