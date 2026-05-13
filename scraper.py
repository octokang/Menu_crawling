import asyncio
import os
from datetime import datetime
from urllib.parse import urlparse, parse_qs, unquote
import httpx
from playwright.async_api import async_playwright

# ─────────────────────────────────────────
# 설정
# ─────────────────────────────────────────
PLACE_ID         = "1578060862"
FEED_URL         = f"https://pcmap.place.naver.com/restaurant/{PLACE_ID}/feed"
STORE_NAME       = "밥짓는 부엌"

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
KMA_API_KEY       = os.environ.get("KMA_API_KEY", "")
AIR_KOREA_API_KEY = os.environ.get("AIR_KOREA_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# 마곡 사이언스파크 기상청 격자 좌표
NX, NY = 57, 127
# 에어코리아 측정소
AIR_STATION = "강서구"


# ─────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────
def is_weekend() -> bool:
    return datetime.now().weekday() >= 5

def is_today(image_url: str) -> bool:
    today = datetime.now().strftime("%Y%m%d")
    return today in image_url

def extract_real_image_url(pstatic_url: str) -> str:
    parsed = urlparse(pstatic_url)
    params = parse_qs(parsed.query)
    if "src" in params:
        return unquote(params["src"][0])
    return pstatic_url

def weekday_korean() -> str:
    days = ["월요일","화요일","수요일","목요일","금요일","토요일","일요일"]
    return days[datetime.now().weekday()]


# ─────────────────────────────────────────
# 1. 메뉴 사진 수집
# ─────────────────────────────────────────
async def fetch_todays_images() -> list:
    image_urls, seen_urls = [], set()

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
        print(f"[{datetime.now()}] 메뉴 페이지 로딩 중...")
        await page.goto(FEED_URL, wait_until="networkidle", timeout=30000)
        await page.wait_for_selector("a.place_thumb", timeout=15000)

        for _ in range(3):
            await page.evaluate("window.scrollBy(0, 800)")
            await asyncio.sleep(1)

        img_elements = await page.query_selector_all("a.place_thumb img")
        for img in img_elements:
            src = await img.get_attribute("src")
            if not src:
                continue
            real_url = extract_real_image_url(src)
            if is_today(real_url) and real_url not in seen_urls:
                seen_urls.add(real_url)
                image_urls.append(real_url)
        await browser.close()

    print(f"[{datetime.now()}] 오늘 메뉴 이미지 {len(image_urls)}장")
    return image_urls


# ─────────────────────────────────────────
# 2. 기상청 단기예보
# ─────────────────────────────────────────
async def fetch_weather() -> dict:
    date = datetime.now().strftime("%Y%m%d")
    url  = "http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst"
    params = {
        "serviceKey": KMA_API_KEY,
        "pageNo": 1, "numOfRows": 300, "dataType": "JSON",
        "base_date": date, "base_time": "0800",
        "nx": NX, "ny": NY,
    }
    weather = {"temp_min":"?","temp_max":"?","temp_now":"?",
               "rain_prob":"?","sky":"?","humidity":"?","wind_speed":"?"}
    sky_map = {"1":"맑음","3":"구름많음","4":"흐림"}

    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, params=params, timeout=10)
        items = res.json()["response"]["body"]["items"]["item"]
        for item in items:
            cat, val, t = item["category"], item["fcstValue"], item["fcstTime"]
            if cat == "TMN": weather["temp_min"] = val
            if cat == "TMX": weather["temp_max"] = val
            if cat == "TMP" and t == "1000": weather["temp_now"] = val
            if cat == "POP" and t == "1000": weather["rain_prob"] = val
            if cat == "SKY" and t == "1000": weather["sky"] = sky_map.get(val, val)
            if cat == "REH" and t == "1000": weather["humidity"] = val
            if cat == "WSD" and t == "1000": weather["wind_speed"] = val
        print(f"[{datetime.now()}] 날씨 수집 완료")
    except Exception as e:
        print(f"[{datetime.now()}] 날씨 수집 실패: {e}")
    return weather


# ─────────────────────────────────────────
# 3. 기상청 자외선지수
# ─────────────────────────────────────────
async def fetch_uv_index() -> str:
    date = datetime.now().strftime("%Y%m%d")
    url  = "http://apis.data.go.kr/1360000/LivingWthrIdxServiceV5/getUVIdxV5"
    params = {
        "serviceKey": KMA_API_KEY,
        "pageNo": 1, "numOfRows": 10, "dataType": "JSON",
        "areaNo": "1150000000",  # 강서구
        "time": date + "06",
    }
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, params=params, timeout=10)
        uv = res.json()["response"]["body"]["items"]["item"][0]["h6"]
        print(f"[{datetime.now()}] 자외선 지수: {uv}")
        return str(uv)
    except Exception as e:
        print(f"[{datetime.now()}] 자외선 수집 실패: {e}")
        return "?"


# ─────────────────────────────────────────
# 4. 에어코리아 미세먼지
# ─────────────────────────────────────────
async def fetch_air_quality() -> dict:
    url = "http://apis.data.go.kr/B552584/ArpltnInforInqireSvc/getMsrstnAcctoRltmMesureDnsty"
    params = {
        "serviceKey": AIR_KOREA_API_KEY,
        "returnType": "json", "numOfRows": 1, "pageNo": 1,
        "stationName": AIR_STATION, "dataTerm": "DAILY", "ver": "1.0",
    }
    grade_map = {"1":"좋음 😊","2":"보통 🙂","3":"나쁨 😷","4":"매우나쁨 🚨"}
    air = {"pm10":"?","pm10_grade":"?","pm25":"?","pm25_grade":"?"}
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, params=params, timeout=10)
        item = res.json()["response"]["body"]["items"][0]
        air["pm10"]       = item.get("pm10Value","?")
        air["pm10_grade"] = grade_map.get(item.get("pm10Grade",""),"?")
        air["pm25"]       = item.get("pm25Value","?")
        air["pm25_grade"] = grade_map.get(item.get("pm25Grade",""),"?")
        print(f"[{datetime.now()}] 미세먼지 수집 완료")
    except Exception as e:
        print(f"[{datetime.now()}] 미세먼지 수집 실패: {e}")
    return air


# ─────────────────────────────────────────
# 5. Claude 날씨 멘트 생성
# ─────────────────────────────────────────
async def generate_weather_comment(weather: dict, air: dict, uv: str) -> str:
    today_str = datetime.now().strftime("%Y년 %m월 %d일") + " " + weekday_korean()
    prompt = f"""오늘은 {today_str}이고, 서울 마곡 사이언스파크 기준 날씨 데이터야.

[날씨 데이터]
- 현재 기온: {weather.get('temp_now')}°C
- 최저/최고: {weather.get('temp_min')}°C / {weather.get('temp_max')}°C
- 하늘 상태: {weather.get('sky')}
- 강수 확률: {weather.get('rain_prob')}%
- 습도: {weather.get('humidity')}%
- 풍속: {weather.get('wind_speed')}m/s
- 미세먼지(PM10): {air.get('pm10')}㎍/㎥ ({air.get('pm10_grade')})
- 초미세먼지(PM2.5): {air.get('pm25')}㎍/㎥ ({air.get('pm25_grade')})
- 자외선 지수: {uv}

이 데이터를 바탕으로 직장인 팀원들에게 유용한 오늘의 날씨 브리핑을 작성해줘.
조건:
- 3~5줄 이내로 간결하게
- 딱딱하지 않고 친근한 말투
- 우산/선크림/마스크/겉옷 등 실용적인 조언 포함
- 점심 산책 가능 여부 한마디 포함
- 이모지 적절히 사용
- 날씨 수치는 자연스럽게 녹여서 (숫자 나열 금지)"""

    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 400,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=30,
            )
        comment = res.json()["content"][0]["text"]
        print(f"[{datetime.now()}] Claude 멘트 생성 완료")
        return comment
    except Exception as e:
        print(f"[{datetime.now()}] Claude 멘트 생성 실패: {e}")
        return f"🌤️ 오늘 기온 {weather.get('temp_min')}~{weather.get('temp_max')}°C, 강수확률 {weather.get('rain_prob')}%"


# ─────────────────────────────────────────
# 6. Slack 전송
# ─────────────────────────────────────────
async def post_to_slack(images: list, weather: dict, air: dict, uv: str, comment: str):
    today_str = datetime.now().strftime("%Y년 %m월 %d일") + " " + weekday_korean()

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*📅 {today_str} — 마곡 날씨 브리핑*"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": comment}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"🌡️ *기온*\n{weather.get('temp_min')}°C ~ {weather.get('temp_max')}°C"},
                {"type": "mrkdwn", "text": f"🌧️ *강수확률*\n{weather.get('rain_prob')}%"},
                {"type": "mrkdwn", "text": f"😷 *미세먼지*\n{air.get('pm10_grade')}"},
                {"type": "mrkdwn", "text": f"🌫️ *초미세먼지*\n{air.get('pm25_grade')}"},
                {"type": "mrkdwn", "text": f"☀️ *자외선*\n{uv}"},
                {"type": "mrkdwn", "text": f"💧 *습도*\n{weather.get('humidity')}%"},
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*🍚 {STORE_NAME} 오늘의 메뉴* ({len(images)}장)"
                        if images else f"*🍚 {STORE_NAME}*\n오늘 메뉴 사진이 아직 없어요.",
            },
        },
    ]

    for url in images:
        blocks.append({"type": "image", "image_url": url, "alt_text": "오늘의 메뉴"})

    async with httpx.AsyncClient() as client:
        res = await client.post(SLACK_WEBHOOK_URL, json={"blocks": blocks}, timeout=10)
        status = "✅ 전송 완료" if res.status_code == 200 else f"❌ 실패: {res.text}"
        print(f"[{datetime.now()}] Slack {status}")


# ─────────────────────────────────────────
# 메인
# ─────────────────────────────────────────
async def main():
    print(f"=== {STORE_NAME} 메뉴 & 날씨 알림 시작 ===")

    if is_weekend():
        print("📅 오늘은 주말이라 실행하지 않습니다.")
        return

    missing = [k for k, v in {
        "SLACK_WEBHOOK_URL": SLACK_WEBHOOK_URL,
        "KMA_API_KEY":       KMA_API_KEY,
        "AIR_KOREA_API_KEY": AIR_KOREA_API_KEY,
        "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
    }.items() if not v]
    if missing:
        print(f"❌ 환경변수 미설정: {', '.join(missing)}")
        return

    # 날씨/미세먼지/메뉴 병렬 수집
    images, weather, air, uv = await asyncio.gather(
        fetch_todays_images(),
        fetch_weather(),
        fetch_air_quality(),
        fetch_uv_index(),
    )

    comment = await generate_weather_comment(weather, air, uv)
    await post_to_slack(images, weather, air, uv, comment)


if __name__ == "__main__":
    asyncio.run(main())
