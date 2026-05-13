import asyncio
import os
from datetime import datetime
from urllib.parse import urlparse, parse_qs, unquote
import httpx
from playwright.async_api import async_playwright

# ─────────────────────────────────────────
# 설정
# ─────────────────────────────────────────
PLACE_ID            = "1578060862"
FEED_URL            = f"https://pcmap.place.naver.com/restaurant/{PLACE_ID}/feed"
STORE_NAME          = "밥짓는 부엌"

SLACK_WEBHOOK_URL   = os.environ.get("SLACK_WEBHOOK_URL", "")
OPENWEATHER_API_KEY = os.environ.get("OPENWEATHER_API_KEY", "")
KMA_API_KEY         = os.environ.get("KMA_API_KEY", "")         # 자외선 전용
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")

# 마곡 사이언스파크 좌표
LAT, LON = 37.5607, 126.8281
# 강서구 자외선 지역코드
UV_AREA_NO = "1150000000"


# ─────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────
def is_weekend() -> bool:
    return datetime.now().weekday() >= 5

def is_today(image_url: str) -> bool:
    return datetime.now().strftime("%Y%m%d") in image_url

def extract_real_image_url(pstatic_url: str) -> str:
    parsed = urlparse(pstatic_url)
    params = parse_qs(parsed.query)
    if "src" in params:
        return unquote(params["src"][0])
    return pstatic_url

def weekday_korean() -> str:
    days = ["월요일","화요일","수요일","목요일","금요일","토요일","일요일"]
    return days[datetime.now().weekday()]

def pm10_grade(v: float) -> str:
    if v <= 30:  return "좋음 😊"
    if v <= 80:  return "보통 🙂"
    if v <= 150: return "나쁨 😷"
    return "매우나쁨 🚨"

def pm25_grade(v: float) -> str:
    if v <= 15:  return "좋음 😊"
    if v <= 35:  return "보통 🙂"
    if v <= 75:  return "나쁨 😷"
    return "매우나쁨 🚨"

def uv_label(v: int) -> str:
    if v < 3:  return f"{v} (낮음)"
    if v < 6:  return f"{v} (보통)"
    if v < 8:  return f"{v} (높음 ⚠️)"
    if v < 11: return f"{v} (매우높음 🚨)"
    return f"{v} (위험 ☠️)"


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
# 2. OpenWeatherMap 날씨 + 미세먼지
# ─────────────────────────────────────────
async def fetch_weather_and_air() -> dict:
    result = {
        "temp_now": "?", "temp_min": "?", "temp_max": "?",
        "rain_prob": "?", "sky": "?", "humidity": "?", "wind_speed": "?",
        "pm10": "?", "pm10_grade": "?",
        "pm25": "?", "pm25_grade": "?",
    }
    base = {"lat": LAT, "lon": LON, "appid": OPENWEATHER_API_KEY, "units": "metric", "lang": "kr"}

    async with httpx.AsyncClient() as client:

        # 날씨 예보 (5일 / 3시간 간격)
        try:
            res = await client.get(
                "https://api.openweathermap.org/data/2.5/forecast",
                params=base, timeout=10
            )
            items = res.json()["list"]
            today = datetime.now().strftime("%Y-%m-%d")
            today_items = [i for i in items if i["dt_txt"].startswith(today)]

            if today_items:
                temps = [i["main"]["temp"] for i in today_items]
                result["temp_min"]   = f"{min(temps):.1f}"
                result["temp_max"]   = f"{max(temps):.1f}"
                nearest              = today_items[0]
                result["temp_now"]   = f"{nearest['main']['temp']:.1f}"
                result["humidity"]   = f"{nearest['main']['humidity']}"
                result["wind_speed"] = f"{nearest['wind']['speed']:.1f}"
                result["sky"]        = nearest["weather"][0]["description"]
                rain_probs           = [i.get("pop", 0) * 100 for i in today_items]
                result["rain_prob"]  = f"{max(rain_probs):.0f}"
            print(f"[{datetime.now()}] ✅ 날씨 수집 완료")
        except Exception as e:
            print(f"[{datetime.now()}] ❌ 날씨 수집 실패: {e}")

        # 대기질 (PM10, PM2.5)
        try:
            res = await client.get(
                "https://api.openweathermap.org/data/2.5/air_pollution",
                params={"lat": LAT, "lon": LON, "appid": OPENWEATHER_API_KEY},
                timeout=10
            )
            comp = res.json()["list"][0]["components"]
            pm10_val = comp.get("pm10", 0)
            pm25_val = comp.get("pm2_5", 0)
            result["pm10"]       = f"{pm10_val:.0f}"
            result["pm10_grade"] = pm10_grade(pm10_val)
            result["pm25"]       = f"{pm25_val:.0f}"
            result["pm25_grade"] = pm25_grade(pm25_val)
            print(f"[{datetime.now()}] ✅ 대기질 수집 완료")
        except Exception as e:
            print(f"[{datetime.now()}] ❌ 대기질 수집 실패: {e}")

    return result


# ─────────────────────────────────────────
# 3. 기상청 자외선 (V5, 강서구 기준)
#    base_time=06, h6 = 낮 12시 자외선
# ─────────────────────────────────────────
async def fetch_uv() -> str:
    date = datetime.now().strftime("%Y%m%d")
    url  = "http://apis.data.go.kr/1360000/LivingWthrIdxServiceV5/getUVIdxV5"
    params = {
        "serviceKey": KMA_API_KEY,
        "pageNo": 1, "numOfRows": 10, "dataType": "JSON",
        "areaNo": UV_AREA_NO,
        "time": date + "06",
    }
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, params=params, timeout=10)
        item = res.json()["response"]["body"]["items"]["item"][0]
        # h6 = 기준시각(06시) + 6시간 = 낮 12시 자외선
        uv_val = int(item.get("h6", 0))
        label  = uv_label(uv_val)
        print(f"[{datetime.now()}] ✅ 자외선 수집 완료: {uv_val}")
        return label
    except Exception as e:
        print(f"[{datetime.now()}] ❌ 자외선 수집 실패: {e}")
        return "?"


# ─────────────────────────────────────────
# 4. Claude 날씨 멘트 생성
# ─────────────────────────────────────────
async def generate_comment(w: dict, uv: str) -> str:
    today_str = datetime.now().strftime("%Y년 %m월 %d일") + " " + weekday_korean()
    prompt = f"""오늘은 {today_str}이고, 서울 마곡 사이언스파크 기준 날씨 데이터야.

[날씨 데이터]
- 현재 기온: {w['temp_now']}°C
- 최저/최고: {w['temp_min']}°C / {w['temp_max']}°C
- 날씨 상태: {w['sky']}
- 강수 확률: {w['rain_prob']}%
- 습도: {w['humidity']}%
- 풍속: {w['wind_speed']}m/s
- 미세먼지(PM10): {w['pm10']}㎍/㎥ ({w['pm10_grade']})
- 초미세먼지(PM2.5): {w['pm25']}㎍/㎥ ({w['pm25_grade']})
- 자외선 지수(낮 12시): {uv}

이 데이터를 바탕으로 직장인 팀원들에게 유용한 오늘의 날씨 브리핑을 작성해줘.
조건:
- 3~5줄 이내로 간결하게
- 딱딱하지 않고 친근한 말투
- 우산/선크림/마스크/겉옷 등 실용적인 조언 포함
- 점심 산책 가능 여부 한마디 포함
- 이모지 적절히 사용
- 날씨 수치는 자연스럽게 녹여서 작성 (숫자 나열 금지)"""

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
        print(f"[{datetime.now()}] ✅ Claude 멘트 생성 완료")
        return comment
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Claude 멘트 생성 실패: {e}")
        # Claude 실패 시 조건문으로 기본 멘트 생성
        msgs = []
        rain = int(float(w["rain_prob"])) if w["rain_prob"] != "?" else 0
        if rain >= 60:
            msgs.append("☂️ 비 올 확률이 높으니 우산 꼭 챙기세요!")
        elif rain >= 30:
            msgs.append("🌂 오후에 비가 올 수도 있어요. 우산 챙기시면 좋아요.")
        if "나쁨" in w.get("pm10_grade", ""):
            msgs.append("😷 미세먼지가 나쁘니 마스크 착용 추천!")
        elif "좋음" in w.get("pm10_grade", ""):
            msgs.append("😊 미세먼지 좋음! 점심 산책하기 딱 좋아요.")
        if uv != "?" and "높음" in uv:
            msgs.append("🌞 자외선이 강해요. 선크림 꼭 바르세요!")
        return "\n".join(msgs) if msgs else f"🌤️ 오늘 기온 {w['temp_min']}~{w['temp_max']}°C, 강수확률 {w['rain_prob']}%"


# ─────────────────────────────────────────
# 5. Slack 전송
# ─────────────────────────────────────────
async def post_to_slack(images: list, w: dict, uv: str, comment: str):
    today_str = datetime.now().strftime("%Y년 %m월 %d일") + " " + weekday_korean()

    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*📅 {today_str} — 마곡 날씨 브리핑*"},
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": comment},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"🌡️ *기온*\n{w['temp_min']}°C ~ {w['temp_max']}°C"},
                {"type": "mrkdwn", "text": f"🌧️ *강수확률*\n{w['rain_prob']}%"},
                {"type": "mrkdwn", "text": f"😷 *미세먼지*\n{w['pm10_grade']}"},
                {"type": "mrkdwn", "text": f"🌫️ *초미세먼지*\n{w['pm25_grade']}"},
                {"type": "mrkdwn", "text": f"☀️ *자외선(정오)*\n{uv}"},
                {"type": "mrkdwn", "text": f"💧 *습도*\n{w['humidity']}%"},
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
        "SLACK_WEBHOOK_URL":   SLACK_WEBHOOK_URL,
        "OPENWEATHER_API_KEY": OPENWEATHER_API_KEY,
        "KMA_API_KEY":         KMA_API_KEY,
        "ANTHROPIC_API_KEY":   ANTHROPIC_API_KEY,
    }.items() if not v]
    if missing:
        print(f"❌ 환경변수 미설정: {', '.join(missing)}")
        return

    # 날씨/자외선/메뉴 병렬 수집
    (images, weather, uv) = await asyncio.gather(
        fetch_todays_images(),
        fetch_weather_and_air(),
        fetch_uv(),
    )

    comment = await generate_comment(weather, uv)
    await post_to_slack(images, weather, uv, comment)


if __name__ == "__main__":
    asyncio.run(main())
