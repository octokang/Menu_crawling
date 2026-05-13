import asyncio
import os
from datetime import datetime
from urllib.parse import urlparse, parse_qs, unquote
import httpx
from playwright.async_api import async_playwright

# ─────────────────────────────────────────
# 설정
# ─────────────────────────────────────────
PLACE_ID = "1578060862"
FEED_URL = f"https://pcmap.place.naver.com/restaurant/{PLACE_ID}/feed"
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")  # 환경변수로 주입
STORE_NAME = "밥짓는 부엌"


def extract_real_image_url(pstatic_url: str) -> str:
    """
    네이버 pstatic CDN URL에서 실제 이미지 URL을 디코딩합니다.
    예: https://search.pstatic.net/common/?...&src=https%3A%2F%2Fldb-phinf...
    """
    parsed = urlparse(pstatic_url)
    params = parse_qs(parsed.query)
    if "src" in params:
        return unquote(params["src"][0])
    return pstatic_url


def is_weekend() -> bool:
    """
    오늘이 주말(토요일=5, 일요일=6)인지 확인합니다. (한국 시간 기준)
    """
    return datetime.now().weekday() >= 5


def is_today(image_url: str) -> bool:
    """
    이미지 URL에 오늘 날짜(YYYYMMDD)가 포함되어 있는지 확인합니다.
    예: ldb-phinf.pstatic.net/20260512_73/...
    """
    today = datetime.now().strftime("%Y%m%d")
    return today in image_url


async def fetch_todays_images() -> list[str]:
    """
    네이버 지도 소식 피드에서 오늘 날짜의 이미지 URL 목록을 가져옵니다.
    """
    image_urls = []
    seen_urls = set()  # 중복 제거용

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="ko-KR",
        )
        page = await context.new_page()

        print(f"[{datetime.now()}] 페이지 로딩 중...")
        await page.goto(FEED_URL, wait_until="networkidle", timeout=30000)

        # 소식 탭이 로딩될 때까지 대기
        await page.wait_for_selector("a.place_thumb", timeout=15000)

        # 스크롤해서 더 많은 콘텐츠 로드 (당일 글이 여러 개일 경우 대비)
        for _ in range(3):
            await page.evaluate("window.scrollBy(0, 800)")
            await asyncio.sleep(1)

        # 모든 피드 이미지 수집
        img_elements = await page.query_selector_all("a.place_thumb img")
        print(f"[{datetime.now()}] 총 {len(img_elements)}개 이미지 발견")

        for img in img_elements:
            src = await img.get_attribute("src")
            if not src:
                continue

            real_url = extract_real_image_url(src)

            if is_today(real_url):
                if real_url not in seen_urls:
                    seen_urls.add(real_url)
                    image_urls.append(real_url)
                    print(f"  ✓ 오늘 이미지: {real_url}")
                else:
                    print(f"  ⚠ 중복 제외: {real_url[:80]}...")
            else:
                print(f"  - 오늘 아님: {real_url[:80]}...")

        await browser.close()

    return image_urls


async def post_to_slack(image_urls: list[str]):
    """
    Slack Incoming Webhook으로 이미지를 포함한 메시지를 전송합니다.
    """
    today_str = datetime.now().strftime("%Y년 %m월 %d일")

    if not image_urls:
        payload = {
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"🍚 *{STORE_NAME}* ({today_str})\n오늘 올라온 메뉴 사진이 없습니다.",
                    },
                }
            ]
        }
    else:
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"🍚 *{STORE_NAME} 오늘의 메뉴* ({today_str})\n총 {len(image_urls)}장",
                },
            },
            {"type": "divider"},
        ]

        for url in image_urls:
            blocks.append(
                {
                    "type": "image",
                    "image_url": url,
                    "alt_text": f"{today_str} 메뉴",
                }
            )

        payload = {"blocks": blocks}

    async with httpx.AsyncClient() as client:
        response = await client.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
        if response.status_code == 200:
            print(f"[{datetime.now()}] ✅ Slack 전송 완료 ({len(image_urls)}장)")
        else:
            print(f"[{datetime.now()}] ❌ Slack 전송 실패: {response.status_code} {response.text}")


async def main():
    print(f"=== {STORE_NAME} 메뉴 알림 시작 ===")

    if is_weekend():
        print("📅 오늘은 주말이라 실행하지 않습니다.")
        return

    if not SLACK_WEBHOOK_URL:
        print("❌ SLACK_WEBHOOK_URL 환경변수가 설정되지 않았습니다.")
        return

    image_urls = await fetch_todays_images()
    print(f"오늘 이미지 {len(image_urls)}장 수집 완료")

    await post_to_slack(image_urls)


if __name__ == "__main__":
    asyncio.run(main())
