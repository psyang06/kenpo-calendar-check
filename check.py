#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
its-kenpo 施設予約系統 - 空缺監控腳本

功能：
1. 用手動匯出的 cookie 帶入瀏覽器 session，避開需要每次手動過 Cloudflare 驗證
2. 開啟行事曆頁面，只看「金（週五）」「土（週六）」兩欄
3. 若該格有「○」記號，點進去看是哪個設施有空
4. 用 ntfy.sh 推播通知
5. 用 seen.json 記錄已經通知過的項目，避免重複通知同一個空缺
6. 如果偵測到 session 過期（又跳回 Cloudflare 驗證頁），主動推播提醒你重新匯出 cookie

環境變數（在 GitHub Actions 的 Secrets / Variables 設定）：
  TARGET_URL     - 要監控的行事曆網址
  NTFY_TOPIC     - ntfy.sh 的 topic 名稱（例如 seiyou-kenpo-2026）
  KENPO_COOKIES  - 用 Cookie-Editor 匯出的 JSON 字串（存成 Secret，不是 Variable）
"""

import json
import os
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests
from playwright.sync_api import sync_playwright

TARGET_URL = os.environ.get("TARGET_URL", "").strip()
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
KENPO_COOKIES_RAW = os.environ.get("KENPO_COOKIES", "").strip()

SEEN_FILE = Path(__file__).parent / "seen.json"
TARGET_WEEKDAYS = {"金", "土"}  # 只看週五、週六

# Cloudflare 驗證頁面常見的關鍵字，用來判斷 session 是否已經過期
CHALLENGE_MARKERS = ["私はロボットではありません", "Cloudflare", "cf-turnstile", "Just a moment"]


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


def notify_ntfy(title: str, message: str, priority: str = "high") -> None:
    if not NTFY_TOPIC:
        print("[跳過] 未設定 NTFY_TOPIC，略過推播通知")
        return
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title.encode("utf-8"), "Priority": priority},
            timeout=15,
        )
    except Exception as e:
        print(f"[錯誤] ntfy 推播失敗: {e}")


def parse_cookies_for_playwright(raw_json: str) -> list[dict]:
    """
    把用 Cookie-Editor 這類擴充功能匯出的 JSON cookie 陣列，
    轉成 Playwright context.add_cookies() 需要的格式。
    """
    if not raw_json:
        return []
    try:
        raw_cookies = json.loads(raw_json)
    except Exception as e:
        print(f"[錯誤] KENPO_COOKIES 不是有效的 JSON: {e}")
        return []

    samesite_map = {
        "lax": "Lax",
        "strict": "Strict",
        "no_restriction": "None",
        "none": "None",
        "unspecified": "Lax",
    }

    cookies = []
    for c in raw_cookies:
        name = c.get("name")
        value = c.get("value")
        domain = c.get("domain")
        if not (name and value and domain):
            continue
        cookie = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": c.get("path", "/"),
        }
        expires = c.get("expirationDate")
        cookie["expires"] = expires if expires else -1
        if "httpOnly" in c:
            cookie["httpOnly"] = bool(c["httpOnly"])
        if "secure" in c:
            cookie["secure"] = bool(c["secure"])
        samesite = c.get("sameSite")
        if samesite:
            cookie["sameSite"] = samesite_map.get(str(samesite).lower(), "Lax")
        cookies.append(cookie)
    return cookies


def is_challenge_page(page) -> bool:
    """判斷目前頁面是不是 Cloudflare 驗證頁（代表 session 過期或沒帶 cookie）。"""
    try:
        body_text = page.inner_text("body")
    except Exception:
        return False
    return any(marker in body_text for marker in CHALLENGE_MARKERS)


def find_available_slots(page) -> list[dict]:
    """
    掃描行事曆表格，找出週五、週六且標記為「○」的格子。
    回傳格式: [{"weekday": "金", "cell_text": "18 ○", "href": "絕對網址或 None"}]
    """
    results = []
    page.wait_for_load_state("networkidle")

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
                if not cell_text:
                    continue  # 空格子（跨月份留白），跳過

                is_available = "〇" in cell_text or "○" in cell_text
                if not is_available:
                    img = cell.query_selector("img")
                    if img:
                        alt = (img.get_attribute("alt") or "").strip()
                        if alt in ("〇", "○"):
                            is_available = True

                if is_available:
                    link_el = cell.query_selector("a")
                    href = None
                    if link_el:
                        raw_href = link_el.get_attribute("href")
                        if raw_href:
                            href = urljoin(page.url, raw_href)
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
        page.goto(href, timeout=15000)
        page.wait_for_load_state("networkidle")
        body_text = page.inner_text("body")
        return body_text[:500]  # 先截前 500 字，避免通知內容過長
    except Exception as e:
        return f"(無法開啟詳細頁面: {e})"


def main() -> None:
    if not TARGET_URL:
        print("[錯誤] 未設定 TARGET_URL")
        sys.exit(1)

    seen = load_seen()
    new_findings = []
    cookies = parse_cookies_for_playwright(KENPO_COOKIES_RAW)
    print(f"[資訊] 載入 {len(cookies)} 個 cookie")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context()
        if cookies:
            context.add_cookies(cookies)
        page = context.new_page()
        page.goto(TARGET_URL, timeout=30000)
        page.wait_for_load_state("networkidle")

        print(f"[Debug] 目前網址: {page.url}")
        body_preview = page.inner_text("body")[:600]
        print(f"[Debug] 頁面文字開頭: {body_preview}")

        if is_challenge_page(page):
            print("[警告] 目前頁面是 Cloudflare 驗證頁，session 可能已過期")
            notify_ntfy(
                "⚠️ kenpo 監控：session 已過期",
                "偵測到目前 session 已失效（跳回機器人驗證頁），"
                "請手動打開網址通過驗證，重新匯出 cookie 並更新 GitHub Secret：KENPO_COOKIES",
                priority="high",
            )
            browser.close()
            return  # 這次不繼續掃描

        slots = find_available_slots(page)
        print(f"掃到 {len(slots)} 個週五/週六的○空缺格")

        # --- Debug 資訊：方便確認 selector 是否抓對 ---
        tables = page.query_selector_all("table")
        print(f"[Debug] 頁面上共有 {len(tables)} 個 <table>")
        for i, table in enumerate(tables):
            header_cells = table.query_selector_all("thead th, tr:first-child th")
            header_texts = [th.inner_text().strip() for th in header_cells]
            row_count = len(table.query_selector_all("tbody tr"))
            print(f"[Debug] table #{i}: 標頭={header_texts} / 資料列數={row_count}")

        for slot in slots:
            key = f"{slot['weekday']}|{slot.get('href')}"
            if key in seen:
                continue  # 已經通知過，跳過

            detail = get_facility_detail(page, slot.get("href"))
            new_findings.append({**slot, "detail": detail})
            seen.add(key)

            # 回到行事曆頁面繼續找下一個
            page.goto(TARGET_URL, timeout=30000)
            page.wait_for_load_state("networkidle")

        browser.close()

    if new_findings:
        title = f"發現 {len(new_findings)} 個週五/週六空缺！"
        lines = []
        for f in new_findings:
            lines.append(
                f"[{f['weekday']}] {f['cell_text']}\n{f['detail']}\n連結: {f.get('href')}\n---"
            )
        message = "\n".join(lines)

        print(title)
        print(message)

        notify_ntfy(title, message)
        save_seen(seen)
    else:
        print("沒有新的空缺。")


if __name__ == "__main__":
    main()
