from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from io import BytesIO
from typing import Any

import altair as alt
import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup


APP_TITLE = "동시간대 편성 체크"

# 크롤링 대상 사이트입니다.
# 사이트 구조가 바뀌면 fetch_tv_schedule(), fetch_homeshopping_schedule()만
# 먼저 확인하면 되도록 나머지 로직은 함수로 분리했습니다.
EPG_GUIDE_PROGRAM_URL = "http://www.epgguide.co.kr/mod/ajax.get_program.php"
ECOMM_SCHEDULE_PAGE_URL = "https://live.ecomm-data.com/schedule/hs"
ECOMM_DATA_BASE_URL = "https://live.ecomm-data.com/_next/data"
HSMOA_SCHEDULE_URL = "https://api.hsmoa.net/v3/schedule"

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": ECOMM_SCHEDULE_PAGE_URL,
}


# EPG Guide 사이트에서 확인한 채널 코드입니다.
TV_CHANNEL_GROUPS = {
    "KBS1": [{"label": "KBS1", "category": "1", "media_code": "00002"}],
    "KBS2": [{"label": "KBS2", "category": "1", "media_code": "00003"}],
    "MBC": [{"label": "MBC", "category": "1", "media_code": "00004"}],
    "SBS": [{"label": "SBS", "category": "1", "media_code": "00005"}],
    "EBS": [{"label": "EBS1", "category": "1", "media_code": "00001"}],
    "tvN": [{"label": "tvN", "category": "4", "media_code": "00230"}],
    "종편": [
        {"label": "JTBC", "category": "13", "media_code": "00771"},
        {"label": "TV조선", "category": "13", "media_code": "00773"},
        {"label": "채널A", "category": "13", "media_code": "00772"},
        {"label": "MBN", "category": "13", "media_code": "00770"},
    ],
}


# 라방바 데이터랩 홈쇼핑 편성표에서 사용하는 홈쇼핑 채널 코드입니다.
HOMESHOPPING_CHANNELS = {
    "CJ온스타일": "hs_cjonstyle",
    "롯데홈쇼핑": "hs_lotteimall",
    "현대홈쇼핑": "hs_hmall",
    "NS홈쇼핑": "hs_nsmall",
    "공영쇼핑": "hs_gongyoung",
    "홈앤쇼핑": "hs_hnsmall",
    "쇼핑엔티": "hs_shopntmall",
}

# 홈쇼핑모아 fallback API에서 사용하는 채널 코드입니다.
HSMOA_CHANNELS = {
    "CJ온스타일": "cjmall",
    "롯데홈쇼핑": "lotteimall",
    "현대홈쇼핑": "hmall",
    "NS홈쇼핑": "nsmall",
    "공영쇼핑": "immall",
    "홈앤쇼핑": "hnsmall",
    "쇼핑엔티": "shopnt",
}

HOMESHOPPING_CODE_TO_NAME = {
    **{v: k for k, v in HOMESHOPPING_CHANNELS.items()},
    **{v: k for k, v in HSMOA_CHANNELS.items()},
}

CORE_TV_CHANNEL_ORDER = {
    "KBS1": 0,
    "KBS2": 1,
    "MBC": 2,
    "SBS": 3,
}

HOMESHOPPING_CHANNEL_ORDER = {
    channel_name: index for index, channel_name in enumerate(HOMESHOPPING_CHANNELS)
}


TV_COLUMNS = [
    "채널",
    "프로그램명",
    "시작시간",
    "방송 예상 종료 시간",
    "내 방송 중 종료 여부",
]

HOMESHOPPING_COLUMNS = [
    "채널",
    "상품명",
    "분류",
    "방송 시작시간",
    "방송 종료시간",
]


def get_user_broadcast_window(
    broadcast_date: date,
    start_time_text: str,
    duration_minutes: int,
) -> tuple[datetime, datetime]:
    """입력한 방송날짜, 시작시간, 방송분으로 시작/종료 datetime을 계산합니다."""
    start_clock = parse_time_text(start_time_text)
    start_dt = datetime.combine(broadcast_date, start_clock)
    end_dt = start_dt + timedelta(minutes=int(duration_minutes))
    return start_dt, end_dt


def parse_time_text(value: str) -> time:
    """'21:00' 같은 문자열을 time 객체로 바꿉니다."""
    value = value.strip()
    if not re.match(r"^\d{1,2}:\d{2}$", value):
        raise ValueError("시간은 21:00 형식으로 입력해주세요.")

    hour_text, minute_text = value.split(":")
    hour = int(hour_text)
    minute = int(minute_text)

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("시간은 00:00부터 23:59 사이로 입력해주세요.")

    return time(hour=hour, minute=minute)


def resolve_tv_channels(selected_options: list[str]) -> list[dict[str, str]]:
    """화면에서 선택한 TV 옵션을 실제 조회할 채널 목록으로 펼칩니다."""
    channels: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for option in selected_options:
        for channel in TV_CHANNEL_GROUPS.get(option, []):
            key = (channel["category"], channel["media_code"])
            if key not in seen:
                channels.append(channel)
                seen.add(key)

    return channels


def get_page(
    url: str,
    *,
    allow_insecure_retry: bool = False,
    **kwargs: Any,
) -> requests.Response:
    """
    웹 페이지/API를 가져옵니다.

    회사 보안망이나 VPN에서 자체 인증서를 끼워 넣으면 SSL 검증 오류가 날 수 있습니다.
    홈쇼핑 조회처럼 필요한 경우에만 allow_insecure_retry=True로 한 번 더 재시도합니다.
    """
    try:
        return requests.get(url, **kwargs)
    except requests.exceptions.SSLError:
        if not allow_insecure_retry:
            raise

        requests.packages.urllib3.disable_warnings(
            requests.packages.urllib3.exceptions.InsecureRequestWarning
        )
        return requests.get(url, verify=False, **kwargs)


def fetch_tv_schedule(
    selected_channels: list[dict[str, str]],
    broadcast_start: datetime,
    broadcast_end: datetime,
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    선택한 TV 채널의 EPG Guide 편성표를 가져옵니다.

    EPG Guide는 JSON 응답 안에 HTML 테이블을 넣어 주므로, 그 HTML을 다시
    BeautifulSoup으로 파싱합니다.
    """
    errors: list[str] = []
    programs: list[dict[str, Any]] = []

    dates_to_fetch = [broadcast_start.date()]

    # 자정을 넘기는 방송은 다음날 00시 이후 편성도 필요합니다.
    # 밤 22시 이후 방송은 마지막 프로그램 종료시간 계산을 위해 다음날 첫 편성도 같이 봅니다.
    if broadcast_end.date() > broadcast_start.date() or broadcast_start.hour >= 22:
        dates_to_fetch.append(broadcast_start.date() + timedelta(days=1))

    for channel in selected_channels:
        for target_date in dates_to_fetch:
            params = {
                "cate_id": channel["category"],
                "media_code": channel["media_code"],
                "ymd": target_date.strftime("%Y%m%d"),
            }

            try:
                response = get_page(
                    EPG_GUIDE_PROGRAM_URL,
                    params=params,
                    headers=REQUEST_HEADERS,
                    timeout=15,
                )
                response.raise_for_status()
                data = response.json()
                html = data.get("html", "")
                programs.extend(parse_tv_schedule_html(html, channel["label"], target_date))
            except Exception as exc:
                errors.append(f"{channel['label']} TV 편성표를 불러오지 못했습니다. ({exc})")

    programs = add_tv_end_times(programs)
    return programs, errors


def parse_tv_schedule_html(
    html: str,
    channel_name: str,
    schedule_date: date,
) -> list[dict[str, Any]]:
    """EPG Guide의 편성표 HTML에서 프로그램명과 시작시간을 뽑습니다."""
    soup = BeautifulSoup(html, "lxml")
    rows = soup.select("tr[id^='time']")
    programs: list[dict[str, Any]] = []

    for row in rows:
        hour = parse_hour_from_row(row.get("id", ""))
        if hour is None:
            continue

        for item in row.select("dl.inner_dl"):
            minute_tag = item.select_one("dt.tit")
            if minute_tag is None:
                continue

            minute_text = minute_tag.get_text(strip=True)
            if not minute_text.isdigit():
                continue

            minute = int(minute_text)
            # EPG Guide는 빈 칸을 61분처럼 표시하는 경우가 있어 실제 편성에서 제외합니다.
            if minute > 59:
                continue

            title = clean_tv_program_title(item)
            if not title:
                continue

            start_dt = datetime.combine(schedule_date, time(hour=hour, minute=minute))
            programs.append(
                {
                    "channel": channel_name,
                    "program_name": title,
                    "start": start_dt,
                }
            )

    return programs


def parse_hour_from_row(row_id: str) -> int | None:
    match = re.search(r"time(\d{2})", row_id)
    if not match:
        return None
    return int(match.group(1))


def clean_tv_program_title(item: BeautifulSoup) -> str:
    """프로그램명에서 재방송/자막 같은 아이콘 텍스트를 제거합니다."""
    content = item.select_one("dd.cont")
    if content is None:
        return ""

    for icon in content.select(".epgicon"):
        icon.decompose()

    return " ".join(content.get_text(" ", strip=True).split())


def add_tv_end_times(programs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """같은 채널의 다음 프로그램 시작시간 -10분을 현재 프로그램 종료 예상 시간으로 사용합니다."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for program in programs:
        grouped[program["channel"]].append(program)

    results: list[dict[str, Any]] = []

    for channel_programs in grouped.values():
        channel_programs.sort(key=lambda item: item["start"])

        for index, program in enumerate(channel_programs):
            if index + 1 < len(channel_programs):
                end_dt = channel_programs[index + 1]["start"] - timedelta(minutes=10)
            else:
                # 다음 편성을 알 수 없을 때의 보수적 fallback입니다.
                end_dt = program["start"] + timedelta(minutes=60)

            if end_dt < program["start"]:
                end_dt = program["start"]

            results.append({**program, "end": end_dt})

    results.sort(key=lambda item: (item["start"], item["channel"]))
    return results


def filter_tv_schedule(
    programs: list[dict[str, Any]],
    broadcast_start: datetime,
    broadcast_end: datetime,
) -> pd.DataFrame:
    """
    내 방송 시간대와 관련 있는 TV 편성을 표 형태로 정리합니다.

    내 방송 중 종료 여부 조건:
    내 방송 시작시간 <= 공중파 프로그램 종료시간 <= 내 방송 종료시간
    """
    grouped_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for program in programs:
        program_start = program["start"]
        program_end = program["end"]

        # 표에는 겹치는 시간 컬럼을 만들지 않지만, 관련 편성을 찾기 위해 내부에서만 사용합니다.
        overlaps_my_broadcast = program_start < broadcast_end and program_end > broadcast_start
        ends_during_my_broadcast = broadcast_start <= program_end <= broadcast_end
        is_related = overlaps_my_broadcast or ends_during_my_broadcast
        if not is_related:
            continue

        grouped_rows[program["channel"]].append(
            {
                "채널": program["channel"],
                "프로그램명": program["program_name"],
                "시작시간": format_table_time(program_start, broadcast_start.date()),
                "방송 예상 종료 시간": format_table_time(program_end, broadcast_start.date()),
                "내 방송 중 종료 여부": "해당" if ends_during_my_broadcast else "비해당",
                "_start_dt": program_start,
                "_end_dt": program_end,
            }
        )

    rows: list[dict[str, Any]] = []
    for channel, channel_rows in grouped_rows.items():
        channel_rows.sort(key=lambda item: item["_end_dt"])
        rows.append(
            {
                "채널": channel,
                "프로그램명": join_table_values(item["프로그램명"] for item in channel_rows),
                "시작시간": join_table_values(item["시작시간"] for item in channel_rows),
                "방송 예상 종료 시간": join_table_values(
                    item["방송 예상 종료 시간"] for item in channel_rows
                ),
                "내 방송 중 종료 여부": join_table_values(
                    item["내 방송 중 종료 여부"] for item in channel_rows
                ),
                "_sort_end": min(item["_end_dt"] for item in channel_rows),
            }
        )

    rows.sort(key=lambda item: get_tv_row_sort_key(item))
    for row in rows:
        row.pop("_sort_end", None)

    return pd.DataFrame(rows, columns=TV_COLUMNS)


def join_table_values(values: Any) -> str:
    """같은 채널에 여러 편성이 있을 때 한 셀 안에 읽기 쉽게 묶습니다."""
    return " / ".join(str(value) for value in values if str(value).strip())


def get_tv_row_sort_key(row: dict[str, Any]) -> tuple[int, Any]:
    """KBS1, KBS2, MBC, SBS를 먼저 고정하고 나머지는 종료 예상 시간이 빠른 순서로 정렬합니다."""
    channel = str(row.get("채널", ""))
    if channel in CORE_TV_CHANNEL_ORDER:
        return (0, CORE_TV_CHANNEL_ORDER[channel])
    return (1, row.get("_sort_end", datetime.max))


def fetch_homeshopping_schedule(
    selected_channel_names: list[str],
    broadcast_start: datetime,
    broadcast_end: datetime,
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    라방바 데이터랩 홈쇼핑 편성표에서 홈쇼핑 편성을 가져옵니다.

    생방 여부 확인 로직 추후 보완 필요:
    현재 페이지 데이터에서는 미래/과거 편성별 생방 여부를 안정적으로 구분할
    필드를 확인하지 못했습니다. 따라서 우선 선택한 시간대와 겹치는 홈쇼핑
    편성을 모두 보여줍니다.
    """
    errors: list[str] = []
    schedules: list[dict[str, Any]] = []

    if not selected_channel_names:
        return schedules, errors

    try:
        page_data = fetch_ecomm_page_data()
        build_id = str(page_data.get("buildId") or "")
        if not build_id:
            raise ValueError("Next.js buildId를 찾지 못했습니다.")
    except Exception as exc:
        return schedules, [f"홈쇼핑 편성표 페이지를 불러오지 못했습니다. ({exc})"]

    page_date_text = get_ecomm_page_date_text(page_data)
    page_schedule_items = extract_ecomm_schedule_items(page_data)

    for target_date in get_schedule_dates(broadcast_start, broadcast_end):
        target_date_text = target_date.strftime("%y%m%d")

        # 기본 페이지에 이미 선택 날짜의 편성표가 들어 있으면 그 데이터를 먼저 사용합니다.
        # JSON 데이터 URL이 빈 목록을 돌려주는 경우를 막기 위한 fallback입니다.
        if is_ecomm_page_data_for_target(page_date_text, target_date, page_schedule_items):
            schedules.extend(page_schedule_items)
            continue

        try:
            fetched_items = fetch_ecomm_schedule_for_date(build_id, target_date)
            if fetched_items:
                schedules.extend(fetched_items)
            elif is_ecomm_page_data_for_target(page_date_text, target_date, page_schedule_items):
                schedules.extend(page_schedule_items)
        except Exception as exc:
            # 선택 날짜가 오늘이고 JSON 데이터 요청이 실패하면 최초 페이지 안의 데이터를 한 번 더 사용합니다.
            if is_ecomm_page_data_for_target(page_date_text, target_date, page_schedule_items):
                schedules.extend(page_schedule_items)
            else:
                errors.append(
                    f"{target_date:%Y-%m-%d} 홈쇼핑 편성표를 불러오지 못했습니다. ({exc})"
                )

    if schedules:
        return schedules, errors

    fallback_schedules, fallback_errors = fetch_hsmoa_schedule(
        selected_channel_names,
        broadcast_start,
        broadcast_end,
    )
    if fallback_schedules:
        return fallback_schedules, []

    if errors:
        errors.extend(fallback_errors)
    else:
        errors.append(
            "홈쇼핑 편성표 데이터가 비어 있습니다. 선택한 날짜의 편성표가 아직 업데이트되지 않았을 수 있습니다."
        )
        errors.extend(fallback_errors)

    return schedules, errors


def fetch_hsmoa_schedule(
    selected_channel_names: list[str],
    broadcast_start: datetime,
    broadcast_end: datetime,
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    라방바 조회가 막히거나 비어 있을 때 홈쇼핑모아 API에서 홈쇼핑 편성을 가져옵니다.

    이 fallback 단계에서는 분류 정보를 사용하지 않고, 채널/상품명/방송시간만 사용합니다.
    """
    if not selected_channel_names:
        return [], []

    base_dt = (broadcast_start - timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
    total_minutes = int((broadcast_end - base_dt).total_seconds() // 60)
    time_size = max(4, min(30, (total_minutes // 60) + 2))
    selected_hsmoa_codes = [HSMOA_CHANNELS[name] for name in selected_channel_names]
    params = {
        "base_hour_datetime": format_hsmoa_datetime(base_dt),
        "time_size": time_size,
        "direction": "down",
        "tv_channel": ",".join(selected_hsmoa_codes),
    }

    try:
        response = get_page(
            HSMOA_SCHEDULE_URL,
            params=params,
            headers=REQUEST_HEADERS,
            timeout=20,
            allow_insecure_retry=True,
        )
        response.raise_for_status()
        response.encoding = "utf-8"
        return extract_hsmoa_schedule_items(response.json()), []
    except Exception as exc:
        return [], [f"홈쇼핑모아 편성표를 불러오지 못했습니다. ({exc})"]


def format_hsmoa_datetime(value: datetime) -> str:
    """홈쇼핑모아 API 요청에 사용할 한국시간 ISO 문자열을 만듭니다."""
    return value.strftime("%Y-%m-%dT%H:%M:%S+09:00")


def extract_hsmoa_schedule_items(data: Any) -> list[dict[str, Any]]:
    """홈쇼핑모아 schedule 응답에서 실제 상품 편성 목록을 평평하게 펼칩니다."""
    if not isinstance(data, dict):
        return []

    items: list[dict[str, Any]] = []
    for section_name in ("before_live", "live", "after_live", "future", "past"):
        section = data.get(section_name)
        if not isinstance(section, list):
            continue

        for block in section:
            if not isinstance(block, dict):
                continue

            candidates = block.get("schedules") or block.get("products") or []
            if not isinstance(candidates, list):
                continue

            for item in candidates:
                if isinstance(item, dict):
                    items.append({**item, "_source": "hsmoa"})

    return items


def fetch_ecomm_page_data() -> dict[str, Any]:
    """홈쇼핑 편성표 페이지의 __NEXT_DATA__ JSON을 읽습니다."""
    response = get_page(
        ECOMM_SCHEDULE_PAGE_URL,
        headers=REQUEST_HEADERS,
        timeout=20,
        allow_insecure_retry=True,
    )
    response.raise_for_status()
    response.encoding = "utf-8"
    return extract_next_data(response.text)


def extract_next_data(html: str) -> dict[str, Any]:
    """Next.js 페이지 HTML에서 __NEXT_DATA__ 스크립트 내용을 파싱합니다."""
    soup = BeautifulSoup(html, "lxml")
    script = soup.find("script", id="__NEXT_DATA__")
    if script is None:
        raise ValueError("__NEXT_DATA__ 스크립트를 찾지 못했습니다.")

    script_text = script.string or script.get_text(strip=True)
    if not script_text:
        raise ValueError("__NEXT_DATA__ 내용이 비어 있습니다.")

    parsed = json.loads(script_text)
    if not isinstance(parsed, dict):
        raise ValueError("__NEXT_DATA__ 형식이 올바르지 않습니다.")
    return parsed


def get_ecomm_page_date_text(data: dict[str, Any]) -> str:
    """라방바 데이터랩 페이지가 들고 있는 조회 날짜를 YYMMDD 형식으로 꺼냅니다."""
    page_props = data.get("pageProps")
    if not isinstance(page_props, dict):
        return ""

    page_date = page_props.get("d")
    return str(page_date or "").strip()


def is_ecomm_page_data_for_target(
    page_date_text: str,
    target_date: date,
    page_schedule_items: list[dict[str, Any]],
) -> bool:
    """기본 페이지 안의 홈쇼핑 데이터가 사용자가 조회한 날짜와 맞는지 확인합니다."""
    if not page_schedule_items:
        return False

    target_date_text = target_date.strftime("%y%m%d")
    if page_date_text == target_date_text:
        return True

    # 사이트가 d 값을 비워도 기본 페이지가 당일 편성을 들고 있는 경우가 있습니다.
    return not page_date_text and target_date == date.today()


def fetch_ecomm_schedule_for_date(build_id: str, target_date: date) -> list[dict[str, Any]]:
    """Next.js 데이터 JSON에서 특정 날짜의 홈쇼핑 편성 목록을 가져옵니다."""
    response = get_page(
        f"{ECOMM_DATA_BASE_URL}/{build_id}/schedule/hs.json",
        params={"date": target_date.strftime("%y%m%d")},
        headers=REQUEST_HEADERS,
        timeout=20,
        allow_insecure_retry=True,
    )
    response.raise_for_status()
    response.encoding = "utf-8"
    payload = response.json()
    return extract_ecomm_schedule_items(payload)


def extract_ecomm_schedule_items(data: Any) -> list[dict[str, Any]]:
    """라방바 데이터랩 응답에서 실제 홈쇼핑 편성 list만 꺼냅니다."""
    if not isinstance(data, dict):
        return []

    page_props = data.get("pageProps")
    if not isinstance(page_props, dict):
        return []

    items = page_props.get("list")
    if not isinstance(items, list):
        return []

    return [item for item in items if isinstance(item, dict)]


def get_schedule_dates(broadcast_start: datetime, broadcast_end: datetime) -> list[date]:
    """자정을 넘기는 방송을 위해 조회해야 할 날짜 목록을 만듭니다."""
    dates: list[date] = []
    current_date = broadcast_start.date()
    last_date = broadcast_end.date()

    while current_date <= last_date:
        dates.append(current_date)
        current_date += timedelta(days=1)

    return dates


def filter_homeshopping_schedule(
    schedules: list[dict[str, Any]],
    selected_channel_names: list[str],
    broadcast_start: datetime,
    broadcast_end: datetime,
) -> pd.DataFrame:
    """
    내 방송 시간대와 겹치는 홈쇼핑 편성을 추출합니다.

    조건:
    홈쇼핑 방송 시작시간 < 내 방송 종료시간
    AND 홈쇼핑 방송 종료시간 > 내 방송 시작시간
    """
    selected_codes = get_selected_homeshopping_codes(selected_channel_names)
    rows: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    for item in schedules:
        channel_code = str(
            item.get("platform_id")
            or item.get("tv_channel")
            or item.get("site")
            or item.get("channel")
            or item.get("channel_code")
            or ""
        )
        channel_name = str(item.get("platform_name") or "").strip()

        if selected_codes and channel_code and channel_code not in selected_codes:
            continue
        if selected_codes and not channel_code and channel_name not in selected_channel_names:
            continue

        start_dt = parse_datetime_value(
            item.get("hsshow_datetime_start")
            or item.get("start_datetime")
            or item.get("broadcast_start_datetime")
            or item.get("startDateTime")
            or item.get("start_time")
        )
        end_dt = parse_datetime_value(
            item.get("hsshow_datetime_end")
            or item.get("end_datetime")
            or item.get("broadcast_end_datetime")
            or item.get("endDateTime")
            or item.get("end_time")
        )

        if start_dt is None or end_dt is None:
            continue

        if not (start_dt < broadcast_end and end_dt > broadcast_start):
            continue

        product_name = str(
            item.get("hsshow_title")
            or item.get("name")
            or item.get("product_name")
            or item.get("productName")
            or item.get("title")
            or ""
        ).strip()
        is_hsmoa_fallback = item.get("_source") == "hsmoa"
        category_name = "" if is_hsmoa_fallback else get_homeshopping_category_name(item)
        unique_key = (
            f"{channel_code}|{product_name}|{start_dt:%Y%m%d%H%M}|{end_dt:%Y%m%d%H%M}"
        )
        if unique_key in seen_keys:
            continue
        seen_keys.add(unique_key)

        display_channel = HOMESHOPPING_CODE_TO_NAME.get(
            channel_code,
            channel_name or channel_code or "확인 필요",
        )
        rows.append(
            {
                "채널": display_channel,
                "상품명": product_name or "상품명 확인 필요",
                "분류": category_name or ("" if is_hsmoa_fallback else "분류 확인 필요"),
                "방송 시작시간": format_table_time(start_dt, broadcast_start.date()),
                "방송 종료시간": format_table_time(end_dt, broadcast_start.date()),
                "_sort_start": start_dt,
            }
        )

    rows.sort(key=lambda row: get_homeshopping_row_sort_key(row))
    for row in rows:
        row.pop("_sort_start", None)

    return pd.DataFrame(rows, columns=HOMESHOPPING_COLUMNS)


def get_selected_homeshopping_codes(selected_channel_names: list[str]) -> set[str]:
    """라방바 코드와 홈쇼핑모아 fallback 코드를 함께 선택 코드로 사용합니다."""
    selected_codes: set[str] = set()
    for name in selected_channel_names:
        if name in HOMESHOPPING_CHANNELS:
            selected_codes.add(HOMESHOPPING_CHANNELS[name])
        if name in HSMOA_CHANNELS:
            selected_codes.add(HSMOA_CHANNELS[name])
    return selected_codes


def build_timeline_chart_data(
    tv_programs: list[dict[str, Any]],
    homeshopping_schedules: list[dict[str, Any]],
    selected_homeshopping_channels: list[str],
    broadcast_start: datetime,
    broadcast_end: datetime,
) -> pd.DataFrame:
    """표에 나온 편성을 시간축 그래프로 그릴 수 있는 데이터로 바꿉니다."""
    rows: list[dict[str, Any]] = []

    for program in tv_programs:
        program_start = program["start"]
        program_end = program["end"]
        overlaps_my_broadcast = program_start < broadcast_end and program_end > broadcast_start
        ends_during_my_broadcast = broadcast_start <= program_end <= broadcast_end
        if not (overlaps_my_broadcast or ends_during_my_broadcast):
            continue

        rows.append(
            {
                "구분": "TV",
                "채널": program["channel"],
                "항목": program["program_name"],
                "시작": program_start,
                "종료": program_end,
                "시작표시": format_table_time(program_start, broadcast_start.date()),
                "종료표시": format_table_time(program_end, broadcast_start.date()),
                "_sort_group": 0,
                "_sort_channel": CORE_TV_CHANNEL_ORDER.get(program["channel"], 99),
            }
        )

    selected_codes = get_selected_homeshopping_codes(selected_homeshopping_channels)
    seen_keys: set[str] = set()
    for item in homeshopping_schedules:
        channel_code = str(
            item.get("platform_id")
            or item.get("tv_channel")
            or item.get("site")
            or item.get("channel")
            or item.get("channel_code")
            or ""
        )
        channel_name = str(item.get("platform_name") or "").strip()

        if selected_codes and channel_code and channel_code not in selected_codes:
            continue
        if selected_codes and not channel_code and channel_name not in selected_homeshopping_channels:
            continue

        start_dt = parse_datetime_value(
            item.get("hsshow_datetime_start")
            or item.get("start_datetime")
            or item.get("broadcast_start_datetime")
            or item.get("startDateTime")
            or item.get("start_time")
        )
        end_dt = parse_datetime_value(
            item.get("hsshow_datetime_end")
            or item.get("end_datetime")
            or item.get("broadcast_end_datetime")
            or item.get("endDateTime")
            or item.get("end_time")
        )
        if start_dt is None or end_dt is None:
            continue
        if not (start_dt < broadcast_end and end_dt > broadcast_start):
            continue

        product_name = str(
            item.get("hsshow_title")
            or item.get("name")
            or item.get("product_name")
            or item.get("productName")
            or item.get("title")
            or ""
        ).strip() or "상품명 확인 필요"
        unique_key = f"{channel_code}|{product_name}|{start_dt:%Y%m%d%H%M}|{end_dt:%Y%m%d%H%M}"
        if unique_key in seen_keys:
            continue
        seen_keys.add(unique_key)

        display_channel = HOMESHOPPING_CODE_TO_NAME.get(
            channel_code,
            channel_name or channel_code or "확인 필요",
        )
        rows.append(
            {
                "구분": "홈쇼핑",
                "채널": display_channel,
                "항목": product_name,
                "시작": start_dt,
                "종료": end_dt,
                "시작표시": format_table_time(start_dt, broadcast_start.date()),
                "종료표시": format_table_time(end_dt, broadcast_start.date()),
                "_sort_group": 1,
                "_sort_channel": HOMESHOPPING_CHANNEL_ORDER.get(display_channel, 99),
            }
        )

    chart_df = pd.DataFrame(rows)
    if chart_df.empty:
        return pd.DataFrame(columns=["구분", "채널", "항목", "시작", "종료", "시작표시", "종료표시", "표시"])

    chart_df = chart_df.sort_values(["_sort_group", "_sort_channel", "시작", "종료", "항목"])
    chart_df["표시"] = chart_df["구분"] + " | " + chart_df["채널"] + " | " + chart_df["항목"]
    return chart_df.drop(columns=["_sort_group", "_sort_channel"])


def get_homeshopping_row_sort_key(row: dict[str, Any]) -> tuple[int, int, Any]:
    """홈쇼핑 채널을 CJ, 롯데, 현대, NS, 공영, 홈앤, 쇼핑엔티 순서로 정렬합니다."""
    channel = str(row.get("채널", ""))
    channel_order = HOMESHOPPING_CHANNEL_ORDER.get(channel)
    if channel_order is not None:
        return (0, channel_order, row.get("_sort_start", datetime.max))
    return (1, 999, row.get("_sort_start", datetime.max))


def get_homeshopping_category_name(item: dict[str, Any]) -> str:
    """홈쇼핑 편성의 분류명을 꺼냅니다."""
    category = item.get("cat")
    if isinstance(category, dict):
        category_name = category.get("cat_name") or category.get("name")
        if category_name:
            return str(category_name).strip()

    return str(item.get("cat_name") or item.get("category") or "").strip()


def parse_datetime_value(value: Any) -> datetime | None:
    """API 응답의 여러 datetime 표현을 Python datetime으로 바꿉니다."""
    if value is None:
        return None

    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None

        # ISO 문자열이 아닌 경우를 대비한 보조 파서입니다.
        candidates = [
            text.replace("Z", "+00:00"),
            text.replace("/", "-"),
        ]
        parsed = None
        for candidate in candidates:
            try:
                parsed = datetime.fromisoformat(candidate)
                break
            except ValueError:
                continue

        if parsed is None:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y%m%d%H%M"):
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue

        if parsed is None:
            return None

    if parsed.tzinfo is not None:
        korea_tz = timezone(timedelta(hours=9))
        parsed = parsed.astimezone(korea_tz).replace(tzinfo=None)

    return parsed


def get_sample_tv_schedule(
    selected_channels: list[dict[str, str]],
    broadcast_start: datetime,
    broadcast_end: datetime,
) -> list[dict[str, Any]]:
    """크롤링 실패 시 앱 화면과 필터링 로직을 확인하기 위한 샘플 TV 편성입니다."""
    samples: list[dict[str, Any]] = []

    for index, channel in enumerate(selected_channels[:8]):
        first_start = broadcast_start - timedelta(minutes=40 + index * 3)
        first_end = broadcast_start + timedelta(minutes=15 + index * 5)
        second_end = min(broadcast_end + timedelta(minutes=20), first_end + timedelta(minutes=50))

        samples.append(
            {
                "channel": channel["label"],
                "program_name": f"샘플 프로그램 {index + 1}",
                "start": first_start,
                "end": first_end,
            }
        )
        samples.append(
            {
                "channel": channel["label"],
                "program_name": f"샘플 뉴스/예능 {index + 1}",
                "start": first_end,
                "end": second_end,
            }
        )

    return samples


def get_sample_homeshopping_schedule(
    selected_channel_names: list[str],
    broadcast_start: datetime,
    broadcast_end: datetime,
) -> list[dict[str, Any]]:
    """크롤링 실패 시 앱 화면과 필터링 로직을 확인하기 위한 샘플 홈쇼핑 편성입니다."""
    samples: list[dict[str, Any]] = []

    for index, channel_name in enumerate(selected_channel_names[:8]):
        start_dt = broadcast_start - timedelta(minutes=20 - index * 5)
        end_dt = min(broadcast_end + timedelta(minutes=15), start_dt + timedelta(minutes=65))

        samples.append(
            {
                "platform_id": HOMESHOPPING_CHANNELS[channel_name],
                "platform_name": channel_name,
                "hsshow_title": f"샘플 상품 {index + 1}",
                "hsshow_datetime_start": start_dt.strftime("%Y%m%d%H%M"),
                "hsshow_datetime_end": end_dt.strftime("%Y%m%d%H%M"),
                "cat": {"cat_name": "샘플 분류"},
            }
        )

    return samples


def format_table_time(value: datetime, reference_date: date) -> str:
    """같은 날짜는 HH:MM, 날짜가 다르면 MM-DD HH:MM으로 표시합니다."""
    if value.date() == reference_date:
        return value.strftime("%H:%M")
    return value.strftime("%m-%d %H:%M")


def format_broadcast_window(start_dt: datetime, end_dt: datetime) -> str:
    if start_dt.date() != end_dt.date():
        return f"{start_dt:%Y-%m-%d %H:%M}~{end_dt:%Y-%m-%d %H:%M}"
    return f"{start_dt:%Y-%m-%d %H:%M}~{end_dt:%H:%M}"


def export_results(
    broadcast_start: datetime,
    broadcast_end: datetime,
    tv_df: pd.DataFrame,
    homeshopping_df: pd.DataFrame,
    chart_df: pd.DataFrame,
) -> bytes:
    """결과를 엑셀 파일의 한 시트에 모아서 만듭니다."""
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        sheet_name = "편성결과"
        summary_df = pd.DataFrame(
            [{"항목": "내 방송 시간", "내용": format_broadcast_window(broadcast_start, broadcast_end)}]
        )
        summary_df.to_excel(writer, index=False, sheet_name=sheet_name, startrow=0)
        worksheet = writer.sheets[sheet_name]

        row = len(summary_df) + 3
        worksheet.cell(row=row, column=1, value="공중파/TV 편성표")
        tv_df.to_excel(writer, index=False, sheet_name=sheet_name, startrow=row)

        row += len(tv_df) + 3
        worksheet.cell(row=row, column=1, value="홈쇼핑 편성표")
        homeshopping_df.to_excel(writer, index=False, sheet_name=sheet_name, startrow=row)

        if not chart_df.empty:
            row += len(homeshopping_df) + 3
            worksheet.cell(row=row, column=1, value="그래프 데이터")
            format_chart_export_df(chart_df).to_excel(
                writer,
                index=False,
                sheet_name=sheet_name,
                startrow=row,
            )

        adjust_excel_sheet_widths(worksheet)

    return output.getvalue()


def adjust_excel_sheet_widths(worksheet: Any) -> None:
    """엑셀 한 시트 안의 열너비를 내용에 맞춰 보기 좋게 조정합니다."""
    from openpyxl.styles import Alignment, Font
    from openpyxl.utils import get_column_letter

    for row in worksheet.iter_rows():
        first_cell_value = row[0].value if row else None
        if first_cell_value in ("공중파/TV 편성표", "홈쇼핑 편성표", "그래프 데이터"):
            row[0].font = Font(bold=True)

        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)

    for column_cells in worksheet.columns:
        column_letter = get_column_letter(column_cells[0].column)
        max_length = 0

        for cell in column_cells:
            value = "" if cell.value is None else str(cell.value)
            longest_line = max((len(line) for line in value.splitlines()), default=0)
            max_length = max(max_length, longest_line)

        adjusted_width = min(max(max_length + 2, 10), 55)
        worksheet.column_dimensions[column_letter].width = adjusted_width

    worksheet.freeze_panes = "A2"


def format_chart_export_df(chart_df: pd.DataFrame) -> pd.DataFrame:
    """그래프 데이터를 엑셀/클립보드에 보기 좋은 문자열 시간으로 변환합니다."""
    export_df = chart_df[["구분", "채널", "항목", "시작", "종료"]].copy()
    export_df["시작"] = export_df["시작"].dt.strftime("%Y-%m-%d %H:%M")
    export_df["종료"] = export_df["종료"].dt.strftime("%Y-%m-%d %H:%M")
    return export_df


def build_clipboard_text(
    broadcast_start: datetime,
    broadcast_end: datetime,
    tv_df: pd.DataFrame,
    homeshopping_df: pd.DataFrame,
    chart_df: pd.DataFrame,
) -> str:
    """엑셀/스프레드시트에 바로 붙여넣기 좋은 탭 구분 텍스트를 만듭니다."""
    chart_text = ""
    if not chart_df.empty:
        chart_text = format_chart_export_df(chart_df).to_csv(
            index=False,
            sep="\t",
            lineterminator="\n",
        ).strip()

    sections = [
        f"내 방송 시간\t{format_broadcast_window(broadcast_start, broadcast_end)}",
        "",
        "[공중파/TV 편성표]",
        tv_df.to_csv(index=False, sep="\t", lineterminator="\n").strip(),
        "",
        "[홈쇼핑 편성표]",
        homeshopping_df.to_csv(index=False, sep="\t", lineterminator="\n").strip(),
        "",
        "[그래프 데이터]",
        chart_text,
    ]
    return "\n".join(sections)


def render_timeline_chart(
    chart_df: pd.DataFrame,
    broadcast_start: datetime,
    broadcast_end: datetime,
) -> None:
    """TV와 홈쇼핑 편성을 한 시간축에서 볼 수 있는 타임라인 그래프를 표시합니다."""
    st.subheader("편성 타임라인 그래프")
    if chart_df.empty:
        st.info("그래프로 표시할 편성 데이터가 없습니다.")
        return

    display_df = chart_df.copy()
    display_df["표시"] = display_df["표시"].str.slice(0, 90)
    sort_order = list(dict.fromkeys(display_df["표시"]))
    chart_height = max(260, min(900, 32 * len(display_df) + 80))
    x_min = min(display_df["시작"].min(), broadcast_start) - timedelta(minutes=10)
    x_max = max(display_df["종료"].max(), broadcast_end) + timedelta(minutes=10)

    bars = (
        alt.Chart(display_df)
        .mark_bar(size=16, clip=True)
        .encode(
            x=alt.X("시작:T", title="시간", scale=alt.Scale(domain=[x_min, x_max])),
            x2="종료:T",
            y=alt.Y("표시:N", sort=sort_order, title=None),
            color=alt.Color(
                "구분:N",
                title="구분",
                scale=alt.Scale(domain=["TV", "홈쇼핑"], range=["#4C78A8", "#F58518"]),
            ),
            tooltip=[
                alt.Tooltip("구분:N"),
                alt.Tooltip("채널:N"),
                alt.Tooltip("항목:N"),
                alt.Tooltip("시작표시:N", title="시작"),
                alt.Tooltip("종료표시:N", title="종료"),
            ],
        )
    )
    rules = (
        alt.Chart(
            pd.DataFrame(
                {
                    "시간": [broadcast_start, broadcast_end],
                    "기준": ["내 방송 시작", "내 방송 종료"],
                }
            )
        )
        .mark_rule(strokeDash=[4, 4], color="#D62728", size=2)
        .encode(
            x="시간:T",
            tooltip=[alt.Tooltip("기준:N"), alt.Tooltip("시간:T", format="%H:%M")],
        )
    )
    st.altair_chart((bars + rules).properties(height=chart_height), use_container_width=True)


def render_results(
    broadcast_start: datetime,
    broadcast_end: datetime,
    tv_df: pd.DataFrame,
    homeshopping_df: pd.DataFrame,
    chart_df: pd.DataFrame,
    tv_errors: list[str],
    homeshopping_errors: list[str],
) -> None:
    """Streamlit 화면에 요약, 표, 다운로드 버튼을 출력합니다."""
    st.subheader("조회 결과")
    st.info(f"내 방송 시간: {format_broadcast_window(broadcast_start, broadcast_end)}")

    if tv_errors or homeshopping_errors:
        st.warning("데이터를 불러오지 못했습니다. 일부 결과가 샘플이거나 표시되지 않을 수 있습니다.")
        with st.expander("크롤링 오류 상세 보기"):
            for message in tv_errors + homeshopping_errors:
                st.write(f"- {message}")

    st.subheader("공중파/TV 편성표")
    st.dataframe(tv_df, use_container_width=True, hide_index=True)

    st.subheader("홈쇼핑 편성표")
    if homeshopping_df.empty and not homeshopping_errors:
        st.info("선택한 방송 시간대와 겹치는 홈쇼핑 편성이 없습니다. 날짜, 시간, 채널 선택을 확인해주세요.")
    st.dataframe(homeshopping_df, use_container_width=True, hide_index=True)

    render_timeline_chart(chart_df, broadcast_start, broadcast_end)

    clipboard_text = build_clipboard_text(
        broadcast_start,
        broadcast_end,
        tv_df,
        homeshopping_df,
        chart_df,
    )

    st.subheader("복사")
    st.caption("아래 칸을 클릭한 뒤 Ctrl+A, Ctrl+C로 복사하세요.")
    st.text_area("클립보드 복사용 텍스트", value=clipboard_text, height=260)

    try:
        excel_bytes = export_results(
            broadcast_start,
            broadcast_end,
            tv_df,
            homeshopping_df,
            chart_df,
        )
        st.download_button(
            label="엑셀 다운로드",
            data=excel_bytes,
            file_name=f"동시간대_편성_체크_{broadcast_start:%Y%m%d_%H%M}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception:
        st.download_button(
            label="CSV 다운로드",
            data=clipboard_text.encode("utf-8-sig"),
            file_name=f"동시간대_편성_체크_{broadcast_start:%Y%m%d_%H%M}.csv",
            mime="text/csv",
        )


def run_search(
    selected_tv_options: list[str],
    selected_homeshopping_channels: list[str],
    broadcast_start: datetime,
    broadcast_end: datetime,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, list[str], list[str]]:
    """조회, fallback, 필터링을 한 번에 실행합니다."""
    tv_channels = resolve_tv_channels(selected_tv_options)

    tv_programs, tv_errors = fetch_tv_schedule(tv_channels, broadcast_start, broadcast_end)
    if tv_errors and not tv_programs:
        tv_programs = get_sample_tv_schedule(tv_channels, broadcast_start, broadcast_end)

    homeshopping_schedules, homeshopping_errors = fetch_homeshopping_schedule(
        selected_homeshopping_channels,
        broadcast_start,
        broadcast_end,
    )

    tv_df = filter_tv_schedule(tv_programs, broadcast_start, broadcast_end)
    homeshopping_df = filter_homeshopping_schedule(
        homeshopping_schedules,
        selected_homeshopping_channels,
        broadcast_start,
        broadcast_end,
    )
    if homeshopping_df.empty:
        fallback_schedules, fallback_errors = fetch_hsmoa_schedule(
            selected_homeshopping_channels,
            broadcast_start,
            broadcast_end,
        )
        fallback_df = filter_homeshopping_schedule(
            fallback_schedules,
            selected_homeshopping_channels,
            broadcast_start,
            broadcast_end,
        )
        if not fallback_df.empty:
            homeshopping_schedules = fallback_schedules
            homeshopping_df = fallback_df
            homeshopping_errors = []
        elif fallback_errors and not homeshopping_errors:
            homeshopping_errors = fallback_errors

    chart_df = build_timeline_chart_data(
        tv_programs,
        homeshopping_schedules,
        selected_homeshopping_channels,
        broadcast_start,
        broadcast_end,
    )

    return tv_df, homeshopping_df, chart_df, tv_errors, homeshopping_errors


def render_input_form() -> tuple[bool, dict[str, Any]]:
    """상단 입력 영역을 렌더링하고 입력값을 반환합니다."""
    with st.form("search_form"):
        st.subheader("조회 조건")

        col1, col2, col3 = st.columns(3)
        broadcast_date = col1.date_input("방송날짜", value=date.today())
        start_time_text = col2.text_input("내 방송 시작시간", value="21:00")
        duration_minutes = col3.number_input("방송분", min_value=1, max_value=600, value=70, step=5)

        st.markdown("**TV 채널 선택**")
        tv_cols = st.columns(7)
        tv_defaults = {
            "KBS1": True,
            "KBS2": True,
            "MBC": True,
            "SBS": True,
            "EBS": False,
            "tvN": False,
            "종편": False,
        }
        selected_tv_options: list[str] = []
        for index, (label, checked) in enumerate(tv_defaults.items()):
            if tv_cols[index].checkbox(label, value=checked):
                selected_tv_options.append(label)

        st.markdown("**홈쇼핑 채널 선택**")
        hs_cols = st.columns(4)
        selected_homeshopping_channels: list[str] = []
        for index, channel_name in enumerate(HOMESHOPPING_CHANNELS):
            if hs_cols[index % 4].checkbox(channel_name, value=True):
                selected_homeshopping_channels.append(channel_name)

        submitted = st.form_submit_button("조회하기", type="primary")

    inputs = {
        "broadcast_date": broadcast_date,
        "start_time_text": start_time_text,
        "duration_minutes": int(duration_minutes),
        "selected_tv_options": selected_tv_options,
        "selected_homeshopping_channels": selected_homeshopping_channels,
    }
    return submitted, inputs


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    st.title(APP_TITLE)

    submitted, inputs = render_input_form()

    if "last_result" in st.session_state and "chart_df" not in st.session_state["last_result"]:
        del st.session_state["last_result"]

    # 첫 화면에서도 기본값으로 한 번 조회되게 하여 초보자가 바로 결과 형태를 볼 수 있게 합니다.
    should_search = submitted or "last_result" not in st.session_state

    if should_search:
        if not inputs["selected_tv_options"]:
            st.error("TV 채널을 1개 이상 선택해주세요.")
            return

        if not inputs["selected_homeshopping_channels"]:
            st.error("홈쇼핑 채널을 1개 이상 선택해주세요.")
            return

        try:
            broadcast_start, broadcast_end = get_user_broadcast_window(
                inputs["broadcast_date"],
                inputs["start_time_text"],
                inputs["duration_minutes"],
            )
        except ValueError as exc:
            st.error(str(exc))
            return

        with st.spinner("편성표를 조회하는 중입니다."):
            tv_df, homeshopping_df, chart_df, tv_errors, homeshopping_errors = run_search(
                inputs["selected_tv_options"],
                inputs["selected_homeshopping_channels"],
                broadcast_start,
                broadcast_end,
            )

        st.session_state["last_result"] = {
            "broadcast_start": broadcast_start,
            "broadcast_end": broadcast_end,
            "tv_df": tv_df,
            "homeshopping_df": homeshopping_df,
            "chart_df": chart_df,
            "tv_errors": tv_errors,
            "homeshopping_errors": homeshopping_errors,
        }

    if "last_result" in st.session_state:
        render_results(**st.session_state["last_result"])


if __name__ == "__main__":
    main()
