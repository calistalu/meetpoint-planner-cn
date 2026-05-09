#!/usr/bin/env python3
"""Plan fair meetup itineraries (2-4 people, multi-stop) with AMap APIs."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

AMAP_BASE = "https://restapi.amap.com"
WALK_TRANSFER_LIMIT_MINUTES = 18
COMPACT_WALK_PREFERENCE_SLACK_MINUTES = 4
COMPACT_TRANSFER_SLACK_MINUTES = 6
COMPACT_SOFT_MAX_TRANSFER_MINUTES = 20

SEMANTIC_KEYWORD_MAP = {
    "猫咖": ["猫咖", "猫咪咖啡", "撸猫"],
    "撸猫": ["猫咖", "猫咪咖啡", "撸猫"],
    "按摩": ["按摩", "推拿", "SPA"],
    "咖啡": ["咖啡", "咖啡馆"],
    "吃饭": ["餐厅", "美食", "中餐", "西餐"],
    "餐厅": ["餐厅", "美食", "中餐", "西餐"],
    "甜品": ["甜品", "蛋糕", "糖水", "冰淇淋"],
    "剧本杀": ["剧本杀", "桌游", "推理馆"],
    "桌游": ["桌游", "棋牌", "剧本杀"],
    "电影": ["电影院", "影城"],
    "商场": ["购物中心", "商场"],
}


class AMapClient:
    def __init__(self, web_key: str, timeout: int = 18):
        self.web_key = web_key
        self.timeout = timeout

    def _get(self, path: str, params: dict) -> dict:
        merged = {"key": self.web_key}
        merged.update(params)
        url = f"{AMAP_BASE}{path}?{urllib.parse.urlencode(merged)}"
        req = urllib.request.Request(url, headers={"User-Agent": "meetpoint-planner-cn/2.0"})

        last_info = "unknown error"
        for attempt in range(6):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            except urllib.error.URLError as err:
                raise RuntimeError(f"AMap request failed: {err}") from err

            if payload.get("status") == "1":
                return payload

            info = payload.get("info", "unknown error")
            last_info = info
            if info in {"CUQPS_HAS_EXCEEDED_THE_LIMIT", "QPS_HAS_EXCEEDED_THE_LIMIT"} and attempt < 5:
                time.sleep(0.35 * (attempt + 1))
                continue
            break

        raise RuntimeError(f"AMap API error: {last_info}")

    def geocode(self, address: str, city: str | None = None) -> dict:
        params = {"address": address}
        if city:
            params["city"] = city

        try:
            data = self._get("/v3/geocode/geo", params)
            geocodes = data.get("geocodes") or []
            if geocodes:
                top = geocodes[0]
                lng, lat = parse_location(top["location"])
                return {
                    "input": address,
                    "name": top.get("formatted_address") or address,
                    "location": top["location"],
                    "lng": lng,
                    "lat": lat,
                    "citycode": normalize_citycode(top.get("citycode")),
                }
        except RuntimeError:
            pass

        data = self._get(
            "/v3/place/text",
            {"keywords": address, "city": city or "", "offset": 1, "extensions": "all"},
        )
        pois = data.get("pois") or []
        if not pois or not pois[0].get("location"):
            raise RuntimeError(f"Address not found: {address}")

        top = pois[0]
        lng, lat = parse_location(top["location"])
        return {
            "input": address,
            "name": top.get("name") or address,
            "location": top["location"],
            "lng": lng,
            "lat": lat,
            "citycode": normalize_citycode(top.get("citycode")),
        }

    def search_around(self, location: str, keyword: str, radius: int, offset: int = 20) -> list[dict]:
        data = self._get(
            "/v3/place/around",
            {
                "location": location,
                "keywords": keyword,
                "radius": radius,
                "offset": max(1, min(offset, 25)),
                "page": 1,
                "extensions": "all",
                "sortrule": "distance",
            },
        )
        return data.get("pois") or []

    def route_minutes(self, origin: str, destination: str, mode: str, city: str | None = None) -> float:
        if mode == "driving":
            data = self._get("/v3/direction/driving", {"origin": origin, "destination": destination})
            paths = (data.get("route") or {}).get("paths") or []
            if not paths:
                raise RuntimeError("No driving path")
            return int(paths[0]["duration"]) / 60.0

        if mode == "walking":
            data = self._get("/v3/direction/walking", {"origin": origin, "destination": destination})
            paths = (data.get("route") or {}).get("paths") or []
            if not paths:
                raise RuntimeError("No walking path")
            return int(paths[0]["duration"]) / 60.0

        if mode == "transit":
            if not city:
                raise RuntimeError("Transit mode requires city")
            data = self._get(
                "/v3/direction/transit/integrated",
                {"origin": origin, "destination": destination, "city": city, "strategy": 0},
            )
            transits = (data.get("route") or {}).get("transits") or []
            if transits:
                return int(transits[0]["duration"]) / 60.0
            raise RuntimeError("No transit path")

        raise RuntimeError(f"Unsupported mode: {mode}")


def normalize_citycode(citycode) -> str | None:
    if citycode is None:
        return None
    if isinstance(citycode, list):
        return citycode[0] if citycode else None
    v = str(citycode).strip()
    return v or None


def parse_location(loc: str) -> tuple[float, float]:
    lng_str, lat_str = loc.split(",")
    return float(lng_str), float(lat_str)


def split_tokens(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"[,，/|、\s]+", text or "") if p.strip()]


def expand_intent_keywords(intent: str) -> list[str]:
    words = split_tokens(intent)
    if not words:
        words = ["咖啡"]
    out: list[str] = []
    seen = set()

    def add(w: str):
        if w and w not in seen:
            seen.add(w)
            out.append(w)

    for w in words:
        add(w)
        if w in SEMANTIC_KEYWORD_MAP:
            for m in SEMANTIC_KEYWORD_MAP[w]:
                add(m)
    return out


def parse_stage_sequence(stages_text: str, preference_text: str) -> list[str]:
    src = (stages_text or "").strip()
    if not src:
        src = (preference_text or "").strip()

    if not src:
        return ["咖啡"]

    splitters = r"(?:->|=>|然后|再去|再|接着|之后|,|，|、)"
    parts = [p.strip() for p in re.split(splitters, src) if p.strip()]
    if not parts:
        parts = [src]

    # Keep itinerary concise in v1 implementation.
    return parts[:5]


def centroid_location(points: list[dict]) -> str:
    lng = sum(p["lng"] for p in points) / len(points)
    lat = sum(p["lat"] for p in points) / len(points)
    return f"{lng:.6f},{lat:.6f}"


def dedupe_pois(items: list[dict]) -> list[dict]:
    by_key: dict[str, dict] = {}
    out = []
    for poi in items:
        key = poi.get("id") or f"{poi.get('name', '')}:{poi.get('location', '')}"
        if key in by_key:
            current = by_key[key]
            if int(poi.get("distance") or 999999) < int(current.get("distance") or 999999):
                current.update(poi)
            continue
        by_key[key] = poi
        out.append(poi)
    return out


def try_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def extract_cost(poi: dict) -> float:
    biz = poi.get("biz_ext")
    if isinstance(biz, dict):
        cost = biz.get("cost")
        if cost:
            return try_float(cost, 0.0)
    return try_float(poi.get("cost"), 0.0)


def extract_rating(poi: dict) -> float:
    biz = poi.get("biz_ext")
    if isinstance(biz, dict):
        rating = biz.get("rating")
        if rating:
            return try_float(rating, 0.0)
    return try_float(poi.get("rating"), 0.0)


def extract_reviews(poi: dict, limit: int = 2) -> list[str]:
    reviews = poi.get("reviews") or poi.get("review") or poi.get("comments") or []
    out: list[str] = []

    if isinstance(reviews, list):
        for item in reviews:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content") or item.get("comment") or item.get("summary")
            else:
                text = item
            text = str(text or "").strip()
            if text:
                out.append(text)
            if len(out) >= limit:
                break

    return out[:limit]


def extract_tags(poi: dict) -> list[str]:
    raw = []
    for key in ("tag", "business_area", "type"):
        value = str(poi.get(key) or "").strip()
        if value:
            raw.extend(split_tokens(value.replace(";", " ").replace("|", " ")))
    out = []
    seen = set()
    for item in raw:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
        if len(out) >= 4:
            break
    return out


def first_photo_url(poi: dict) -> str:
    photos = poi.get("photos")
    if isinstance(photos, list) and photos:
        first = photos[0]
        if isinstance(first, dict):
            url = first.get("url", "")
            if url:
                return str(url)
        if isinstance(first, str) and first:
            return first
    if isinstance(photos, str) and photos:
        return photos.split("|")[0]
    return ""


def text_of(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(text_of(v) for v in value)
    if isinstance(value, dict):
        return " ".join(text_of(v) for v in value.values())
    return str(value)


def semantic_bonus(keyword_list: list[str], poi: dict) -> float:
    haystack = " ".join(
        [
            text_of(poi.get("name", "")),
            text_of(poi.get("address", "")),
            text_of(poi.get("type", "")),
            text_of(poi.get("business_area", "")),
            text_of(poi.get("tag", "")),
        ]
    ).lower()
    bonus = 0.0
    for kw in keyword_list:
        if kw.lower() in haystack:
            bonus += 0.42
    return min(bonus, 2.4)


def budget_bonus(cost: float, pref: str) -> float:
    if pref == "any" or cost <= 0:
        return 0.0
    if pref == "economy":
        if cost <= 80:
            return 0.8
        if cost <= 120:
            return 0.3
        return -0.4
    if pref == "mid":
        if 60 <= cost <= 180:
            return 0.8
        if 40 <= cost <= 220:
            return 0.2
        return -0.2
    if pref == "premium":
        if cost >= 120:
            return 0.7
        if cost >= 90:
            return 0.2
        return -0.3
    return 0.0


def vibe_bonus(vibe_keywords: list[str], poi: dict) -> float:
    if not vibe_keywords:
        return 0.0
    haystack = " ".join(
        [
            text_of(poi.get("name", "")),
            text_of(poi.get("address", "")),
            text_of(poi.get("type", "")),
            text_of(poi.get("business_area", "")),
            text_of(poi.get("tag", "")),
        ]
    ).lower()
    score = 0.0
    for kw in vibe_keywords:
        if kw.lower() in haystack:
            score += 0.35
    return min(score, 1.2)


def score_candidate(c: dict, variant: str, stage_index: int) -> float:
    if variant == "fairness":
        if stage_index == 0:
            return c["gap"] * 2.6 + c["std"] * 1.0 + c["avg_time"] * 0.2 - c["quality"]
        walk_bonus = -0.8 if c.get("transfer_mode") == "walking" else 0.0
        return c["transfer_minutes"] * 1.35 - c["quality"] + walk_bonus

    if variant == "custom":
        if stage_index == 0:
            return c["gap"] * 1.7 + c["std"] * 0.65 + c["avg_time"] * 0.14 - c["quality"] * 1.8
        walk_bonus = -1.2 if c.get("transfer_mode") == "walking" else 0.0
        return c["transfer_minutes"] * 1.2 - c["quality"] * 1.9 + walk_bonus

    # compact variant
    if stage_index == 0:
        return c["gap"] * 2.0 + c["std"] * 0.8 + c["avg_time"] * 0.22 - c["quality"] * 1.08
    transfer = c["transfer_minutes"]
    walk_bonus = -4.0 if c.get("transfer_mode") == "walking" else 0.0
    long_transfer_penalty = max(0.0, transfer - WALK_TRANSFER_LIMIT_MINUTES) * 8.0
    non_walk_penalty = 12.0 if transfer > WALK_TRANSFER_LIMIT_MINUTES and c.get("transfer_mode") != "walking" else 0.0
    return transfer * 5.0 + long_transfer_penalty + non_walk_penalty - c["quality"] * 0.8 + walk_bonus


def estimate_stage_transfer(client: AMapClient, origin: str, destination: str, city: str | None) -> dict:
    walking_minutes: float | None = None
    try:
        walking_minutes = round(client.route_minutes(origin, destination, mode="walking", city=city), 1)
    except RuntimeError:
        walking_minutes = None

    if walking_minutes is not None and walking_minutes <= WALK_TRANSFER_LIMIT_MINUTES:
        return {
            "mode": "walking",
            "label": "步行",
            "minutes": walking_minutes,
            "note": "两站距离适合步行衔接",
        }

    if city:
        try:
            transit_minutes = round(client.route_minutes(origin, destination, mode="transit", city=city), 1)
            note = "步行偏久，建议改用地铁/公交"
            if walking_minutes is not None:
                note = f"步行约 {walking_minutes} 分钟，建议改用地铁/公交"
            return {
                "mode": "transit",
                "label": "地铁/公交",
                "minutes": transit_minutes,
                "note": note,
            }
        except RuntimeError:
            pass

    if walking_minutes is not None:
        return {
            "mode": "walking",
            "label": "步行",
            "minutes": walking_minutes,
            "note": "暂无稳定公共交通结果，先按步行估算",
        }

    try:
        driving_minutes = round(client.route_minutes(origin, destination, mode="driving", city=city), 1)
    except RuntimeError:
        driving_minutes = 999.0

    return {
        "mode": "driving",
        "label": "打车备选",
        "minutes": driving_minutes,
        "note": "步行和公共交通结果不足，作为兜底估算",
    }


def evaluate_candidate(
    client: AMapClient,
    poi: dict,
    participants: list[dict],
    mode: str,
    city: str | None,
    max_each_minutes: float,
    keyword_list: list[str],
    budget_pref: str,
    vibe_keywords: list[str],
    previous_location: str | None,
    consider_commute: bool,
) -> dict | None:
    destination = poi.get("location")
    if not destination:
        return None

    times = []
    times_by_label = {}
    if consider_commute:
        for p in participants:
            try:
                t = client.route_minutes(p["location"], destination, mode=mode, city=city)
            except RuntimeError:
                t = client.route_minutes(p["location"], destination, mode="driving", city=city)
            times.append(t)
            times_by_label[p["label"]] = round(t, 1)

        max_t = max(times)
        min_t = min(times)
        avg_t = sum(times) / len(times)
        std_t = math.sqrt(sum((t - avg_t) ** 2 for t in times) / len(times))

        if max_each_minutes > 0 and max_t > max_each_minutes:
            return None
    else:
        max_t = min_t = avg_t = std_t = 0.0

    transfer = {
        "mode": "origin",
        "label": "首站集合",
        "minutes": 0.0,
        "note": "此站按每个人从家出发的通勤公平性评估",
    }
    if previous_location:
        transfer = estimate_stage_transfer(client, previous_location, destination, city)

    cost = extract_cost(poi)
    rating = extract_rating(poi)
    sem = semantic_bonus(keyword_list, poi)
    b_bonus = budget_bonus(cost, budget_pref)
    v_bonus = vibe_bonus(vibe_keywords, poi)
    r_bonus = min(max(rating, 0.0), 5.0) / 5.0 * 0.9
    quality = sem + b_bonus + v_bonus + r_bonus

    return {
        **poi,
        "times": times_by_label,
        "gap": round(max_t - min_t, 1),
        "avg_time": round(avg_t, 1),
        "std": round(std_t, 2),
        "max_time": round(max_t, 1),
        "min_time": round(min_t, 1),
        "total": round(sum(times), 1),
        "transfer_minutes": round(transfer["minutes"], 1),
        "transfer_mode": transfer["mode"],
        "transfer_label": transfer["label"],
        "transfer_note": transfer["note"],
        "semantic_bonus": round(sem, 2),
        "budget_bonus": round(b_bonus, 2),
        "vibe_bonus": round(v_bonus, 2),
        "rating_bonus": round(r_bonus, 2),
        "quality": round(quality, 2),
        "cost": round(cost, 1) if cost else 0.0,
        "rating": round(rating, 1) if rating else 0.0,
        "reviews": extract_reviews(poi),
        "tags": extract_tags(poi),
        "photo": first_photo_url(poi),
    }


def gather_stage_options(
    client: AMapClient,
    participants: list[dict],
    city_code: str | None,
    stage_intent: str,
    keyword_list: list[str],
    anchors: list[str],
    mode: str,
    radius: int,
    per_anchor_limit: int,
    evaluate_limit: int,
    max_each_minutes: float,
    budget_pref: str,
    vibe_keywords: list[str],
    previous_location: str | None,
    consider_commute: bool,
) -> list[dict]:
    raw = []
    for loc in anchors:
        for kw in keyword_list:
            pois = client.search_around(loc, kw, radius=radius, offset=per_anchor_limit)
            raw.extend(pois)

    deduped = dedupe_pois(raw)
    deduped.sort(key=lambda x: int(x.get("distance") or 999999))
    limited = deduped[: max(1, evaluate_limit)]

    evaluated = []
    for poi in limited:
        item = evaluate_candidate(
            client=client,
            poi=poi,
            participants=participants,
            mode=mode,
            city=city_code,
            max_each_minutes=max_each_minutes,
            keyword_list=keyword_list,
            budget_pref=budget_pref,
            vibe_keywords=vibe_keywords,
            previous_location=previous_location,
            consider_commute=consider_commute,
        )
        if item:
            item["stage_intent"] = stage_intent
            evaluated.append(item)

    return evaluated


def build_reason(stage_index: int, selected: dict) -> str:
    reviews = selected.get("reviews") or []
    review_hint = ""
    if reviews:
        review_hint = f" 公开评论里有人提到“{reviews[0][:28]}”，可以作为现场感受的参考。"

    if stage_index == 0:
        times = list((selected.get("times") or {}).values())
        if times:
            spread = max(times) - min(times)
            if spread <= 8:
                opening = "这站适合当集合点，大家到达时间很接近，不会明显让某一个人多赶路。"
            elif max(times) <= 45:
                opening = "这站作为第一站比较稳，虽然通勤有一点差距，但整体都还在轻松可接受的范围内。"
            else:
                opening = "这站更偏体验取向，位置和活动匹配度不错，但需要其中一位多留一点路上时间。"
        else:
            opening = "这站适合当集合点，位置和活动类型都比较贴合这次安排。"
        details = []
        if selected.get("cost", 0) > 0:
            details.append(f"人均约 ¥{selected['cost']:.0f}")
        if selected.get("rating", 0) > 0:
            details.append(f"评分 {selected['rating']:.1f}/5")
        suffix = "，".join(details)
        return opening + (f" 参考下来{suffix}，适合先把电影这一步落稳。" if suffix else "") + review_hint

    t = float(selected.get("transfer_minutes", 0) or 0)
    label = selected.get("transfer_label", "转场")
    if t <= 5:
        opening = f"这站和上一站几乎是顺路衔接，{label}几分钟就到，适合不打断聊天和游玩的节奏。"
    elif t <= 15:
        opening = f"这站转场压力不大，{label}约 {t:g} 分钟，走过去或换乘都不会太折腾。"
    elif t <= 30:
        opening = f"这站需要留一点转场时间，但换来的是更贴合活动类型和偏好的选择。"
    else:
        opening = f"这站离上一站偏远，适合你们愿意为这一类体验多花一点路程时选择。"

    details = []
    if selected.get("cost", 0) > 0:
        details.append(f"人均约 ¥{selected['cost']:.0f}")
    if selected.get("rating", 0) > 0:
        details.append(f"评分 {selected['rating']:.1f}/5")
    suffix = "，".join(details)
    return opening + (f" {suffix}，整体比较适合接在这一段后面。" if suffix else "") + review_hint


def format_times(times: dict) -> str:
    if not times:
        return "暂无"
    parts = [f"{k} {v} 分钟" for k, v in times.items()]
    return " | ".join(parts)


def format_stage_summary(stage: dict) -> str:
    selected = stage["selected"]
    if stage["index"] == 1:
        return f"首站通勤 {format_times(selected.get('times', {}))}"
    return f"转场 {selected.get('transfer_label', '转场')} {selected.get('transfer_minutes', 0)} 分钟"


def candidate_keys(candidate: dict) -> set[str]:
    return {str(candidate.get("id") or ""), str(candidate.get("location") or "")}


def is_avoided(candidate: dict, avoid_keys: set[str] | None) -> bool:
    return bool(avoid_keys and candidate_keys(candidate).intersection(avoid_keys))


def choose_first_available(scored: list[dict], avoid_keys: set[str] | None = None) -> dict:
    if avoid_keys and len(scored) > 1:
        for candidate in scored:
            if not is_avoided(candidate, avoid_keys):
                return candidate
    return scored[0]


def compact_transfer_pool(scored: list[dict], avoid_keys: set[str] | None = None) -> list[dict]:
    available = [c for c in scored if not is_avoided(c, avoid_keys)] or scored
    min_transfer = min(float(c.get("transfer_minutes", 999.0) or 999.0) for c in available)
    walking = [c for c in available if c.get("transfer_mode") == "walking"]

    if walking:
        min_walking = min(float(c.get("transfer_minutes", 999.0) or 999.0) for c in walking)
        tight_walking = [
            c
            for c in walking
            if float(c.get("transfer_minutes", 999.0) or 999.0) <= min_walking + COMPACT_WALK_PREFERENCE_SLACK_MINUTES
        ]
        return tight_walking or walking

    tight = [
        c
        for c in available
        if float(c.get("transfer_minutes", 999.0) or 999.0) <= min_transfer + COMPACT_TRANSFER_SLACK_MINUTES
    ]
    if min_transfer <= COMPACT_SOFT_MAX_TRANSFER_MINUTES:
        tight = [
            c
            for c in tight
            if float(c.get("transfer_minutes", 999.0) or 999.0) <= COMPACT_SOFT_MAX_TRANSFER_MINUTES
        ]
    return tight or available


def select_option(options: list[dict], variant: str, stage_index: int, avoid_keys: set[str] | None = None) -> dict:
    scored = []
    for c in options:
        c2 = dict(c)
        c2["variant_score"] = round(score_candidate(c2, variant=variant, stage_index=stage_index), 3)
        scored.append(c2)
    scored.sort(key=lambda x: x["variant_score"])

    if variant == "compact" and stage_index > 0:
        return choose_first_available(compact_transfer_pool(scored, avoid_keys), avoid_keys)

    return choose_first_available(scored, avoid_keys)


def build_plan(
    client: AMapClient,
    participants: list[dict],
    centroid: str,
    city_code: str | None,
    stage_intents: list[str],
    mode: str,
    radius: int,
    per_anchor_limit: int,
    evaluate_limit: int,
    max_each_minutes: float,
    budget_pref: str,
    vibe_keywords: list[str],
    variant: str,
    option_topn: int,
    avoid_first_keys: set[str] | None = None,
) -> dict:
    stages_out = []
    previous = None
    previous_keys: set[str] = set()

    for i, intent in enumerate(stage_intents):
        kw = expand_intent_keywords(intent)
        anchors = ([previous, centroid] if previous else [centroid] + [p["location"] for p in participants])

        options = gather_stage_options(
            client=client,
            participants=participants,
            city_code=city_code,
            stage_intent=intent,
            keyword_list=kw,
            anchors=anchors,
            mode=mode,
            radius=radius,
            per_anchor_limit=per_anchor_limit,
            evaluate_limit=evaluate_limit,
            max_each_minutes=max_each_minutes,
            budget_pref=budget_pref,
            vibe_keywords=vibe_keywords,
            previous_location=previous,
            consider_commute=i == 0,
        )

        if not options and radius < 12000:
            options = gather_stage_options(
                client=client,
                participants=participants,
                city_code=city_code,
                stage_intent=intent,
                keyword_list=kw,
                anchors=anchors,
                mode=mode,
                radius=min(int(radius * 1.6), 14000),
                per_anchor_limit=per_anchor_limit,
                evaluate_limit=evaluate_limit,
                max_each_minutes=max_each_minutes,
                budget_pref=budget_pref,
                vibe_keywords=vibe_keywords,
                previous_location=previous,
                consider_commute=i == 0,
            )

        if not options:
            raise RuntimeError(f"No options for stage: {intent}")

        avoid_keys = avoid_first_keys if i == 0 else previous_keys
        selected = select_option(options, variant=variant, stage_index=i, avoid_keys=avoid_keys)
        selected["reason"] = build_reason(i, selected)
        stages_out.append(
            {
                "index": i + 1,
                "intent": intent,
                "selected": selected,
                "options": sorted(
                    [{**o, "variant_score": round(score_candidate(o, variant=variant, stage_index=i), 3)} for o in options],
                    key=lambda x: x["variant_score"],
                )[: max(1, option_topn)],
            }
        )
        previous = selected["location"]
        previous_keys = {str(selected.get("id") or ""), str(selected.get("location") or "")}

    avg_gap = round(sum(s["selected"]["gap"] for s in stages_out) / len(stages_out), 1)
    total_transfer = round(sum(s["selected"].get("transfer_minutes", 0.0) for s in stages_out[1:]), 1)

    return {
        "variant": variant,
        "avg_gap": avg_gap,
        "total_transfer": total_transfer,
        "stages": stages_out,
    }


def amap_nav_mode(mode: str | None) -> str:
    if mode == "walking":
        return "walk"
    if mode == "transit":
        return "bus"
    return "car"


def make_navigation_url(src: dict, dst: dict, mode: str | None = None) -> str:
    q = {
        "from": f"{src['location']},{src['name']}",
        "to": f"{dst['location']},{dst.get('name', '候选点')}",
        "mode": amap_nav_mode(mode),
    }
    return "https://uri.amap.com/navigation?" + urllib.parse.urlencode(q)


def escape_html(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def render_html(path: Path, result: dict, js_key: str | None, js_security_code: str | None):
    participants = result["participants"]
    centroid = result["centroid"]
    plans = result["plans"]

    plan_meta = {
        "fairness": {
            "kicker": "Plan A",
            "title": "公平优先 🍿",
            "summary": "首站集合时间尽量均衡，适合大家从不同方向出发。",
            "accent": "#FF6B6B",
            "tab": "#FF6B6B",
        },
        "compact": {
            "kicker": "Plan B",
            "title": "动线紧凑 🍜",
            "summary": "后续站点尽量少折返，适合多站连续体验。",
            "accent": "#FFE66D",
            "tab": "#FFE66D",
        },
        "custom": {
            "kicker": "Plan C",
            "title": "偏好匹配 🎲",
            "summary": "更看重预算、氛围、评分和关键词匹配度。",
            "accent": "#388E3C",
            "tab": "#388E3C",
        },
    }

    def plan_id(plan: dict) -> str:
        return str(plan.get("variant") or "plan")

    def plan_title(plan: dict) -> str:
        meta = plan_meta.get(plan_id(plan), {})
        return f"{meta.get('kicker', 'Plan')} · {meta.get('title', plan_id(plan))}"

    def render_rating(rating: float) -> str:
        if rating <= 0:
            return "<span class='muted'>暂无评分</span>"
        full = max(0, min(5, int(round(rating))))
        stars = "★" * full + "☆" * (5 - full)
        return f"<span class='stars'>{stars}</span><span>{rating:.1f}</span>"

    def render_budget(cost: float) -> str:
        if cost <= 0:
            return "预算暂无"
        return f"人均 ¥{cost:.0f}"

    def render_tags(item: dict) -> str:
        tags = item.get("tags") or []
        if not tags:
            return ""
        return "<div class='tags'>" + "".join(f"<span>{escape_html(t)}</span>" for t in tags[:4]) + "</div>"

    def render_reviews(item: dict) -> str:
        reviews = item.get("reviews") or []
        if not reviews:
            return "<div class='review-empty'>暂无可用公开评论</div>"
        return "".join(f"<blockquote>{escape_html(r)}</blockquote>" for r in reviews[:2])

    def svg_icon(name: str) -> str:
        common = "viewBox='0 0 24 24' aria-hidden='true' focusable='false'"
        if name == "people":
            return (
                f"<svg class='svg-icon' {common}><circle cx='8' cy='8' r='3'/><circle cx='16' cy='8' r='3'/>"
                f"<path d='M3.5 19c.6-3.2 2.4-5 4.5-5s3.9 1.8 4.5 5'/>"
                f"<path d='M11.5 19c.6-3.2 2.4-5 4.5-5s3.9 1.8 4.5 5'/></svg>"
            )
        if name == "gear":
            return (
                f"<svg class='svg-icon' {common}><circle cx='12' cy='12' r='3.2'/>"
                f"<path d='M12 2.8v3M12 18.2v3M4.2 12h3M16.8 12h3M6.4 6.4l2.1 2.1M15.5 15.5l2.1 2.1M17.6 6.4l-2.1 2.1M8.5 15.5l-2.1 2.1'/></svg>"
            )
        if name == "bulb":
            return (
                f"<svg class='svg-icon' {common}><path d='M8 14.5c-1.2-1.1-2-2.6-2-4.3a6 6 0 0 1 12 0c0 1.7-.8 3.2-2 4.3-.8.7-1.2 1.5-1.2 2.5H9.2c0-1-.4-1.8-1.2-2.5Z'/>"
                f"<path d='M9.4 20h5.2M10 17h4'/></svg>"
            )
        if name == "pin":
            return f"<svg class='mini-icon' {common}><path d='M12 21s7-5.2 7-11a7 7 0 0 0-14 0c0 5.8 7 11 7 11Z'/><circle cx='12' cy='10' r='2.2'/></svg>"
        if name == "walk":
            return f"<svg class='mini-icon' {common}><circle cx='13' cy='4' r='2'/><path d='M10 21l2-6-3-3 2-5 4 2 2 4M15 21l-3-6'/></svg>"
        if name == "bus":
            return f"<svg class='mini-icon' {common}><rect x='5' y='4' width='14' height='13' rx='2'/><path d='M8 17v2M16 17v2M7 9h10M8 13h2M14 13h2'/></svg>"
        if name == "car":
            return f"<svg class='mini-icon' {common}><path d='M6 16h12l-1.4-5.2A2.5 2.5 0 0 0 14.2 9H9.8a2.5 2.5 0 0 0-2.4 1.8L6 16Z'/><path d='M5 16h14M7 16v2M17 16v2M8 13h.1M16 13h.1'/></svg>"
        return f"<svg class='mini-icon' {common}><circle cx='12' cy='12' r='8'/></svg>"

    def mode_icon(mode: str | None) -> str:
        if mode == "walking":
            return svg_icon("walk")
        if mode == "driving":
            return svg_icon("car")
        return svg_icon("bus")

    def fair_score(plan: dict) -> float:
        return round(max(0.0, min(100.0, 100.0 - float(plan.get("avg_gap", 0)) * 6.0)), 1)

    def render_time_panel(stage: dict) -> str:
        s = stage["selected"]
        if stage["index"] != 1:
            return (
                f"<div class='time-panel single-time'>"
                f"<p>转场时间（分钟）</p>"
                f"<div class='time-box transfer-box'>{mode_icon(s.get('transfer_mode'))}<span>{escape_html(s.get('transfer_label', '转场'))}</span>"
                f"<strong>{s.get('transfer_minutes', 0)}</strong></div>"
                f"</div>"
            )

        times = s.get("times", {})
        boxes = []
        for label, minutes in times.items():
            boxes.append(
                f"<div class='time-box person-time {escape_html(label.lower())}'>"
                f"{mode_icon(result.get('mode'))}<span>{escape_html(label)}</span><strong>{minutes}</strong></div>"
            )
        return f"<div class='time-panel'><p>通勤时间（分钟）</p><div class='time-grid'>{''.join(boxes)}</div></div>"

    def render_option_table(stage: dict) -> str:
        rows = []
        for idx, opt in enumerate(stage["options"][:2], start=1):
            if stage["index"] == 1:
                tds = "".join(f"<td>{opt.get('times', {}).get(p['label'], '-')}</td>" for p in participants)
                last = f"<td>{opt.get('transfer_minutes', 0)}</td>"
            else:
                tds = f"<td colspan='{len(participants)}'>{opt.get('transfer_label', '转场')} {opt.get('transfer_minutes', 0)} 分钟</td>"
                last = f"<td>{render_budget(opt.get('cost', 0))}</td>"
            rows.append(
                f"<tr><th>{idx}</th><td>{escape_html(opt.get('name', '候选点'))}</td>{tds}{last}</tr>"
            )
        heads = "".join(f"<th>{escape_html(p['label'])}</th>" for p in participants)
        tail = "<th>转场</th>" if stage["index"] == 1 else "<th>预算</th>"
        pcols = "".join("<col class='person-col' />" for _ in participants)
        return (
            f"<div class='option-table'><h4>备选方案（2选2）</h4>"
            f"<table><colgroup><col class='idx-col' /><col class='place-col' />{pcols}<col class='tail-col' /></colgroup>"
            f"<thead><tr><th></th><th>地点</th>{heads}{tail}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
        )

    def transfer_separator(stage: dict) -> str:
        s = stage["selected"]
        return (
            f"<div class='transfer-sep'>{mode_icon(s.get('transfer_mode'))}"
            f"<strong>转场约 {s.get('transfer_minutes', 0)} 分钟</strong>"
            f"<span>| 与上一站衔接紧凑</span></div>"
        )

    def render_nav_actions(stage: dict, previous_stage: dict | None) -> str:
        s = stage["selected"]
        links = []
        if stage["index"] == 1:
            for p in participants:
                href = make_navigation_url(p, s, result.get("mode"))
                links.append(
                    f"<a class='nav-button' href='{escape_html(href)}' target='_blank' rel='noopener noreferrer'>"
                    f"{escape_html(p['label'])} 导航</a>"
                )
        elif previous_stage:
            prev = previous_stage["selected"]
            href = make_navigation_url(prev, s, s.get("transfer_mode") or result.get("mode"))
            links.append(
                f"<a class='nav-button' href='{escape_html(href)}' target='_blank' rel='noopener noreferrer'>转场导航</a>"
            )
        return f"<span class='nav-actions'>{''.join(links)}</span>" if links else ""

    def stage_html(plan: dict, stage: dict, previous_stage: dict | None = None) -> str:
        s = stage["selected"]
        photo_html = (
            f"<img class='hover-photo' src='{escape_html(s.get('photo') or '')}' alt='地点图片' />"
            if s.get("photo")
            else "<div class='hover-photo placeholder'>暂无地点图片</div>"
        )
        return (
            f"<article class='stage-card' tabindex='0'>"
            f"<div class='stage-main'>"
            f"<div class='stage-left'>"
            f"<div class='stage-head'><span>{stage['index']}</span><div><p>阶段 {stage['index']} · {escape_html(stage['intent'])}</p><h3>{escape_html(s.get('name', '候选点'))}</h3></div></div>"
            f"<div class='address'>{escape_html(s.get('address', ''))}</div>"
            f"<p class='reason'>推荐理由：{escape_html(s['reason'])}</p>"
            f"<div class='poi-meta'><span>{render_budget(s.get('cost', 0))}</span><span>{render_rating(s.get('rating', 0))}</span>{render_nav_actions(stage, previous_stage)}</div>"
            f"</div>"
            f"{render_time_panel(stage)}"
            f"</div>"
            f"<div class='stage-hover-panel'>{photo_html}{render_option_table(stage)}</div>"
            f"</article>"
        )

    plan_buttons = []
    plan_blocks = []
    for idx, plan in enumerate(plans):
        pid = plan_id(plan)
        meta = plan_meta.get(pid, {})
        active = " is-active" if idx == 0 else ""
        hidden = "" if idx == 0 else " hidden"
        accent = meta.get("accent", "#111111")
        tab = meta.get("tab", accent)
        plan_buttons.append(
            f"<button class='plan-tab{active}' data-plan='{escape_html(pid)}' style='--accent:{accent};--tab:{tab}'>"
            f"<strong>{escape_html(meta.get('kicker', 'Plan'))} · {escape_html(meta.get('title', pid))}</strong>"
            f"<div><span>总转场</span><b>{plan['total_transfer']} 分钟</b></div>"
            f"<div><span>公平度</span><b>{fair_score(plan)} 分</b></div>"
            f"<small>{escape_html(meta.get('summary', ''))}</small>"
            f"</button>"
        )
        stage_parts = []
        previous_stage = None
        for st in plan["stages"]:
            if st["index"] > 1:
                stage_parts.append(transfer_separator(st))
            stage_parts.append(stage_html(plan, st, previous_stage))
            previous_stage = st
        plan_blocks.append(
            f"<section class='plan-panel{active}' data-plan-panel='{escape_html(pid)}'{hidden}>"
            f"<div class='current-plan' style='--accent:{accent}'>"
            f"<div class='current-head'><h3>{escape_html(plan_title(plan))}<em>（当前选中）</em></h3>"
            f"<p>总转场 {plan['total_transfer']} 分钟&nbsp;&nbsp;|&nbsp;&nbsp;公平度 {fair_score(plan)} 分</p></div>"
            f"{''.join(stage_parts)}"
            f"</div>"
            f"</section>"
        )

    marker_data = []
    for i, p in enumerate(participants):
        marker_data.append(
            {
                "plan": "context",
                "role": f"origin p{i + 1}",
                "badge": p["label"],
                "name": p["name"],
                "location": p["location"],
                "popup_html": f"<div class='popup-title'>{escape_html(p['name'])}</div><div class='popup-meta'>{p['label']} 出发点</div>",
            }
        )

    marker_data.append(
        {
            "plan": "context",
            "role": "center",
            "badge": "C",
            "name": "集合中心参考点",
            "location": centroid,
            "popup_html": "<div class='popup-title'>集合中心参考点</div><div class='popup-meta'>这是按大家出发地估算的候选搜索中心，不是最终推荐站点。</div>",
        }
    )

    plan_paths = {}
    plan_tags = {"fairness": "A", "compact": "B", "custom": "C"}
    for plan in plans:
        pid = plan_id(plan)
        tag = plan_tags.get(pid, "P")
        plan_paths[pid] = []
        for stage in plan["stages"]:
            s = stage["selected"]
            plan_paths[pid].append(s["location"])
            image_html = (
                f"<img class='popup-photo' src='{escape_html(s.get('photo') or '')}' alt='门店图片' />"
                if s.get("photo")
                else "<div class='popup-noimg'>暂无门店图片</div>"
            )
            marker_data.append(
                {
                    "plan": pid,
                    "role": f"plan_{tag.lower()} stage_{stage['index']}",
                    "badge": str(stage["index"]),
                    "name": s.get("name", "候选点"),
                    "location": s["location"],
                    "popup_html": (
                        f"<div class='popup-title'>{escape_html(s.get('name', '候选点'))}</div>"
                        f"<div class='popup-meta'>阶段 {stage['index']}：{escape_html(stage['intent'])}</div>"
                        f"<div class='popup-meta'>{render_rating(s.get('rating', 0))} · {escape_html(render_budget(s.get('cost', 0)))}</div>"
                        f"<div class='popup-meta'>{escape_html(s.get('transfer_label', '首站集合'))} {s.get('transfer_minutes', 0)} 分钟</div>"
                        f"{image_html}"
                    ),
                }
            )

    security_line = ""
    if js_security_code:
        security_line = "window._AMapSecurityConfig = {securityJsCode: " + json.dumps(js_security_code) + "};"

    if js_key:
        map_bootstrap = """
<script>
__SECURITY_LINE__
</script>
<script src=\"https://webapi.amap.com/loader.js\"></script>
<script>
const markerData = __MARKER_DATA__;
const planPaths = __PLAN_PATHS__;
const center = __CENTER__;
const planLineColors = {
  fairness: ["#FF6B6B", "#FFE66D", "#388E3C"],
  compact: ["#FFE66D", "#388E3C", "#FF6B6B"],
  custom: ["#388E3C", "#FF6B6B", "#FFE66D"]
};

AMapLoader.load({
  key: __JS_KEY__,
  version: "2.0"
}).then((AMap) => {
  const map = new AMap.Map("map", { zoom: 12, center, mapStyle: "amap://styles/fresh" });
  const infoWindow = new AMap.InfoWindow({ offset: new AMap.Pixel(0, -20) });
  const markerRefs = markerData.map((m) => {
    const [lng, lat] = m.location.split(",").map(Number);
    const marker = new AMap.Marker({
      position: [lng, lat],
      title: m.name,
      content: `<div class='mk ${m.role}'><span>${m.badge}</span></div>`,
      offset: new AMap.Pixel(-18, -36)
    });
    marker.on("click", () => {
      infoWindow.setContent(`<div class='popup-card'>${m.popup_html || ''}</div>`);
      infoWindow.open(map, [lng, lat]);
    });
    return { marker, data: m };
  });
  let activeLines = [];
  window.activateMapPlan = (planId) => {
    const visible = [];
    markerRefs.forEach((entry) => {
      const show = entry.data.plan === "context" || entry.data.plan === planId;
      entry.marker.setMap(show ? map : null);
      if (show) visible.push(entry.marker);
    });
    activeLines.forEach((line) => line.setMap(null));
    activeLines = [];
    const path = (planPaths[planId] || []).map((loc) => loc.split(",").map(Number));
    if (path.length > 1) {
      const colors = planLineColors[planId] || ["#FF6B6B", "#FFE66D", "#388E3C"];
      for (let i = 1; i < path.length; i += 1) {
        const line = new AMap.Polyline({
          path: [path[i - 1], path[i]],
          strokeColor: colors[(i - 1) % colors.length],
          strokeOpacity: 0.92,
          strokeWeight: 7,
          strokeStyle: "solid",
          lineJoin: "round",
          lineCap: "round",
          zIndex: 20
        });
        line.setMap(map);
        activeLines.push(line);
      }
      const outline = new AMap.Polyline({
        path,
        strokeColor: "#1A1A1A",
        strokeOpacity: 0.95,
        strokeWeight: 11,
        zIndex: 19
      });
      outline.setMap(map);
      activeLines.unshift(outline);
    }
    if (visible.length) {
      map.setFitView(visible, false, [80, 80, 80, 80], 15);
    }
  };
  window.activateMapPlan(Object.keys(planPaths)[0]);
}).catch((e) => {
  document.getElementById("map").innerHTML = `<div class='error'>地图加载失败: ${String(e)}</div>`;
});
</script>
"""
        map_bootstrap = (
            map_bootstrap.replace("__SECURITY_LINE__", security_line)
            .replace("__MARKER_DATA__", json.dumps(marker_data, ensure_ascii=False))
            .replace("__PLAN_PATHS__", json.dumps(plan_paths, ensure_ascii=False))
            .replace("__CENTER__", json.dumps([float(centroid.split(",")[0]), float(centroid.split(",")[1])]))
            .replace("__JS_KEY__", json.dumps(js_key, ensure_ascii=False))
        )
    else:
        map_bootstrap = """
<script>
document.getElementById("map").innerHTML = "<div class='error'>未提供 AMAP_JS_KEY，已仅输出路线规划文本。</div>";
</script>
"""

    ui_bootstrap = """
<script>
document.querySelectorAll("[data-plan]").forEach((button) => {
  button.addEventListener("click", () => {
    const planId = button.dataset.plan;
    document.querySelectorAll("[data-plan]").forEach((b) => b.classList.toggle("is-active", b === button));
    document.querySelectorAll("[data-plan-panel]").forEach((panel) => {
      const active = panel.dataset.planPanel === planId;
      panel.hidden = !active;
      panel.classList.toggle("is-active", active);
    });
    if (window.activateMapPlan) window.activateMapPlan(planId);
  });
});
</script>
"""

    participant_meta = "".join(f"<span>{p['label']} · {escape_html(p['input'])}</span>" for p in participants)
    participant_cards = "".join(
        f"<article class='person-card person-{idx}'>"
        f"<span>{escape_html(p['label'])}</span>"
        f"<div><strong>{escape_html(p['input'])}</strong><small>{escape_html(p['name'])}</small></div>"
        f"{svg_icon('pin')}"
        f"</article>"
        for idx, p in enumerate(participants, start=1)
    )
    legend_origins = "".join(f"<i class='origin-{idx}'>{escape_html(p['label'])}</i>" for idx, p in enumerate(participants, start=1))
    legend_stages = "".join(f"<i class='stage-{idx}'>{idx}</i>" for idx, _ in enumerate(result["stage_intents"], start=1))
    legend_html = (
        f"<div class='legend-row'><span class='legend-badges origin-set'>{legend_origins}</span><span>= 出发地</span></div>"
        f"<div class='legend-row'><span class='legend-badges stage-set'>{legend_stages}</span><span>= 站点</span></div>"
        f"<div class='legend-row'><span class='legend-badges'><i class='center-mark'><span>C</span></i></span><span>= 集合中心参考点</span></div>"
        f"<div class='legend-row'><span class='legend-line'></span><span>= 当前路线连接</span></div>"
    )

    css = """
    :root {
      color-scheme: light;
      --bg: #FFF5F5;
      --red: #FF6B6B;
      --yellow: #FFE66D;
      --green: #388E3C;
      --ink: #1A1A1A;
      --paper: #FFFFFF;
      --muted: #5f5858;
      --line: #1A1A1A;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--ink);
      background:
        radial-gradient(circle at 38px 48px, rgba(255,107,107,.28) 0 18px, transparent 19px),
        radial-gradient(circle at calc(100% - 54px) 118px, rgba(56,142,60,.18) 0 24px, transparent 25px),
        linear-gradient(135deg, var(--bg), #fff 64%);
      font-family: "HarmonyOS Sans SC", "MiSans", "Alibaba PuHuiTi", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", system-ui, sans-serif;
      font-variant-numeric: tabular-nums;
    }
    .shell {
      display: grid;
      grid-template-columns: minmax(260px, 18vw) minmax(430px, 38vw) minmax(420px, 1fr);
      gap: 8px;
      min-height: 100vh;
      padding: 10px;
      border: 6px solid var(--ink);
    }
    .sidebar {
      position: relative;
      height: calc(100vh - 32px);
      overflow: auto;
      padding: 26px 16px 20px;
      background: rgba(255,245,245,0.96);
    }
    .sidebar::before {
      content: "";
      position: absolute;
      left: 18px;
      top: 18px;
      width: 42px;
      height: 42px;
      border: 2px solid var(--line);
      border-radius: 50%;
      background: var(--red);
    }
    .sidebar::after {
      content: "";
      position: absolute;
      right: 40px;
      top: 30px;
      width: 22px;
      height: 22px;
      border: 2px solid var(--line);
      background: var(--green);
      transform: rotate(12deg);
    }
    .panel {
      position: relative;
      height: calc(100vh - 32px);
      overflow: auto;
      padding: 18px 16px 22px;
      border: 2px solid var(--line);
      border-radius: 20px;
      background: rgba(255,255,255,0.74);
    }
    .panel::before {
      display: none;
    }
    .panel::after {
      content: "";
      position: absolute;
      right: 22px;
      bottom: 22px;
      width: 44px;
      height: 44px;
      border: 2px solid var(--line);
      border-radius: 50%;
      background: var(--yellow);
      pointer-events: none;
    }
    .brand {
      position: relative;
      display: block;
      margin: 44px 0 22px 18px;
      padding-right: 16px;
    }
    .brand::after {
      content: "";
      position: absolute;
      right: 2px;
      top: -28px;
      width: 24px;
      height: 24px;
      border: 2px solid var(--line);
      background: var(--red);
      transform: rotate(10deg);
    }
    .brand p {
      display: inline-flex;
      margin: 0 0 10px;
      padding: 6px 12px;
      border: 2px solid var(--line);
      border-radius: 999px;
      background: var(--yellow);
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
    }
    h1 { margin: 0; max-width: 100%; font-size: clamp(28px, 2vw, 36px); line-height: 1.08; font-weight: 900; letter-spacing: 0; }
    .brand small { display: block; margin-top: 10px; color: var(--muted); font-size: 14px; font-weight: 800; line-height: 1.45; }
    .panel-title {
      display: flex;
      justify-content: space-between;
      gap: 18px;
      align-items: flex-start;
      margin-bottom: 12px;
    }
    .panel-title h2 { margin: 0; font-size: 24px; line-height: 1.1; font-weight: 900; }
    .participants { display: flex; flex-wrap: wrap; gap: 7px; justify-content: flex-end; max-width: 240px; }
    .participants span, .tags span {
      border: 2px solid var(--line);
      border-radius: 999px;
      padding: 6px 9px;
      font-size: 11px;
      font-weight: 800;
      color: var(--ink);
      background: var(--paper);
      box-shadow: 2px 2px 0 var(--line);
    }
    .tags span { background: var(--green); color: #fff; box-shadow: none; }
    .brief {
      display: grid;
      gap: 7px;
      margin-bottom: 18px;
      padding: 14px;
      border: 2px solid var(--line);
      border-radius: 20px;
      background: var(--paper);
      box-shadow: 4px 4px 0 var(--line);
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
    }
    .side-card, .tip-card {
      margin-bottom: 16px;
      padding: 14px;
      border: 2px solid var(--line);
      border-radius: 20px;
      background: var(--paper);
      box-shadow: 4px 4px 0 var(--line);
      transition: transform .24s cubic-bezier(.2,.8,.2,1), box-shadow .24s cubic-bezier(.2,.8,.2,1);
    }
    .side-card:hover, .tip-card:hover { transform: translateY(-2px); box-shadow: 6px 7px 0 var(--line); }
    .side-card h2, .tip-card h2 { display: flex; align-items: center; gap: 8px; margin: 0 0 12px; font-size: 17px; font-weight: 900; }
    .svg-icon, .mini-icon { width: 24px; height: 24px; fill: none; stroke: var(--ink); stroke-width: 2.3; stroke-linecap: round; stroke-linejoin: round; flex: 0 0 auto; }
    .side-card h2 .svg-icon, .tip-card h2 .svg-icon {
      width: 30px;
      height: 30px;
      padding: 3px;
      border: 2px solid var(--line);
      border-radius: 50%;
      background: var(--yellow);
    }
    .person-list { display: grid; gap: 10px; }
    .person-card {
      display: grid;
      grid-template-columns: 54px 1fr 28px;
      gap: 12px;
      align-items: center;
      padding: 10px;
      border: 2px solid var(--line);
      border-radius: 14px;
      background: #fff;
      transition: transform .24s cubic-bezier(.2,.8,.2,1), background .24s ease;
    }
    .person-card:hover { transform: translateX(4px); background: #fffaf1; }
    .person-card > span {
      width: 46px;
      height: 46px;
      display: grid;
      place-items: center;
      border: 2px solid var(--line);
      border-radius: 14px;
      color: #fff;
      font-weight: 900;
      box-shadow: 2px 2px 0 var(--line);
    }
    .person-1 > span { background: var(--red); }
    .person-2 > span { background: #8B5CF6; }
    .person-3 > span { background: #F97316; }
    .person-4 > span { background: var(--green); }
    .person-card strong, .person-card small { display: block; line-height: 1.35; }
    .person-card strong { font-size: 14px; font-weight: 900; }
    .person-card small { color: var(--muted); font-size: 12px; font-weight: 700; }
    .person-card .mini-icon { width: 26px; height: 26px; stroke: var(--red); }
    .settings-card { display: grid; gap: 10px; }
    .settings-card div {
      padding: 10px 12px;
      border: 2px solid var(--line);
      border-radius: 12px;
      background: #fff;
    }
    .settings-card strong, .settings-card span { display: block; line-height: 1.4; }
    .settings-card strong { font-size: 13px; font-weight: 900; }
    .settings-card span { color: var(--muted); font-size: 13px; font-weight: 750; }
    .tip-card { background: #fff7be; }
    .tip-card p { margin: 0; color: var(--muted); font-size: 13px; line-height: 1.55; font-weight: 750; }
    .tabs {
      position: relative;
      top: auto;
      z-index: 7;
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 10px;
      padding: 0 0 14px;
    }
    .plan-tab {
      cursor: pointer;
      text-align: left;
      min-height: 132px;
      padding: 14px;
      border: 2px solid var(--line);
      border-radius: 20px;
      background: var(--paper);
      color: var(--ink);
      box-shadow: 4px 4px 0 var(--line);
      transition: transform .26s cubic-bezier(.2,.8,.2,1), box-shadow .26s cubic-bezier(.2,.8,.2,1), background .26s ease, border-color .26s ease;
    }
    .plan-tab:hover { transform: translate(-1px, -1px); box-shadow: 5px 5px 0 var(--line); }
    .plan-tab.is-active { background: #f1fff3; border-color: var(--green); transform: translate(2px, 2px); box-shadow: 2px 2px 0 var(--line); }
    .plan-tab strong { display: block; margin-bottom: 12px; font-size: 16px; font-weight: 900; line-height: 1.2; }
    .plan-tab div { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-bottom: 6px; }
    .plan-tab span { color: var(--muted); font-size: 12px; font-weight: 800; }
    .plan-tab b { font-size: 20px; line-height: 1; font-weight: 900; }
    .plan-tab small { display: block; margin-top: 10px; color: var(--muted); font-size: 12px; line-height: 1.35; font-weight: 800; }
    .plan-panel { animation: popIn .28s ease both; }
    .current-plan {
      position: relative;
      margin: 2px 0 0;
      padding: 16px;
      border: 2px solid var(--line);
      border-radius: 20px;
      background: linear-gradient(90deg, rgba(56,142,60,.08), #fff 42%);
      box-shadow: 4px 4px 0 var(--line);
      transition: box-shadow .28s cubic-bezier(.2,.8,.2,1), transform .28s cubic-bezier(.2,.8,.2,1);
    }
    .current-plan:hover { transform: translateY(-1px); box-shadow: 6px 7px 0 var(--line); }
    .current-plan::before {
      content: "";
      position: absolute;
      left: -2px;
      top: 18px;
      width: 18px;
      height: 42px;
      border: 2px solid var(--line);
      border-left: 0;
      border-radius: 0 20px 20px 0;
      background: var(--green);
    }
    .current-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin: 0 0 14px 16px;
      padding-bottom: 10px;
      border-bottom: 2px solid var(--line);
    }
    .current-head h3 { margin: 0; font-size: 22px; line-height: 1.15; font-weight: 900; }
    .current-head h3 em { font-size: 13px; font-style: normal; }
    .current-head p { margin: 0; white-space: nowrap; font-size: 13px; font-weight: 900; }
    .stage-card {
      position: relative;
      margin: 0;
      padding: 12px;
      border: 2px solid var(--line);
      border-radius: 20px;
      background: var(--paper);
      overflow: hidden;
      transition: transform .3s cubic-bezier(.2,.8,.2,1), box-shadow .3s cubic-bezier(.2,.8,.2,1), background .3s ease;
    }
    .stage-card:hover, .stage-card:focus, .stage-card:focus-within {
      transform: translateY(-3px);
      box-shadow: 5px 6px 0 var(--line);
      background: #fffdf9;
    }
    .stage-card::after {
      content: "";
      position: absolute;
      right: 16px;
      bottom: 16px;
      width: 22px;
      height: 22px;
      border: 2px solid var(--line);
      background: var(--yellow);
      transform: rotate(12deg);
    }
    .stage-head { display: grid; grid-template-columns: 44px 1fr; gap: 12px; align-items: start; margin-bottom: 12px; }
    .stage-head > span {
      width: 38px;
      height: 38px;
      border: 2px solid var(--line);
      border-radius: 50%;
      display: grid;
      place-items: center;
      font-size: 12px;
      font-weight: 900;
      color: var(--ink);
      background: var(--red);
      box-shadow: 3px 3px 0 var(--line);
    }
    .stage-head p { margin: 0 0 3px; color: var(--green); font-size: 12px; font-weight: 900; }
    .stage-head h3 { margin: 0; font-size: 22px; line-height: 1.16; font-weight: 900; letter-spacing: 0; }
    .stage-main {
      display: grid;
      grid-template-columns: minmax(0, 1.15fr) minmax(220px, .85fr);
      gap: 14px;
      align-items: start;
    }
    .stage-left { min-width: 0; }
    .poi-photo {
      width: 100%;
      aspect-ratio: 16 / 9;
      object-fit: cover;
      border: 2px solid var(--line);
      border-radius: 20px;
      margin: 2px 0 12px;
    }
    .poi-meta { display: flex; flex-wrap: nowrap; align-items: center; gap: 8px; margin-bottom: 8px; color: var(--ink); font-size: 13px; }
    .poi-meta > span {
      display: inline-flex;
      gap: 6px;
      align-items: center;
      padding: 6px 9px;
      border: 2px solid var(--line);
      border-radius: 999px;
      background: #fff;
      font-weight: 800;
      white-space: nowrap;
    }
    .nav-actions {
      display: inline-flex;
      flex-wrap: nowrap;
      gap: 8px;
      align-items: center;
      min-width: 0;
    }
    .poi-meta > .nav-actions {
      padding: 0;
      border: 0;
      border-radius: 0;
      background: transparent;
      box-shadow: none;
    }
    .nav-button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 34px;
      padding: 6px 11px;
      border: 2px solid var(--line);
      border-radius: 999px;
      background: var(--yellow);
      color: var(--ink);
      box-shadow: 2px 2px 0 var(--line);
      font-size: 12px;
      font-weight: 900;
      white-space: nowrap;
      text-decoration: none;
      transition: transform .22s cubic-bezier(.2,.8,.2,1), box-shadow .22s cubic-bezier(.2,.8,.2,1), background .22s ease;
    }
    .nav-button:hover, .nav-button:focus {
      transform: translate(-1px, -1px);
      box-shadow: 3px 3px 0 var(--line);
      background: #fff2a8;
    }
    .stars { color: var(--red); letter-spacing: 0; }
    .muted, .address { color: var(--muted); }
    .address { font-size: 13px; line-height: 1.4; margin-bottom: 8px; }
    .tags { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }
    .time-panel {
      padding: 10px;
      border: 2px solid var(--line);
      border-radius: 14px;
      background: #fff;
    }
    .time-panel p { margin: 0 0 8px; color: var(--muted); font-size: 12px; font-weight: 900; }
    .time-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(58px, 1fr)); gap: 8px; }
    .time-box {
      min-height: 64px;
      display: grid;
      place-items: center;
      gap: 2px;
      padding: 8px 6px;
      border-radius: 10px;
      background: #fff2f2;
      font-weight: 900;
    }
    .time-box .mini-icon { width: 18px; height: 18px; }
    .time-box span { font-size: 13px; }
    .time-box strong { font-size: 20px; line-height: 1; }
    .p2 { background: #f1eafe; }
    .p3 { background: #fff2e8; }
    .p4 { background: #e9f8ed; }
    .transfer-box { background: #f5f5f5; }
    .single-time .transfer-box { min-height: 86px; }
    .reason { margin: 10px 0 12px; font-size: 13px; line-height: 1.55; color: var(--ink); font-weight: 650; }
    .reviews h4 { margin: 0 0 6px; font-size: 12px; font-weight: 900; color: var(--green); }
    blockquote {
      margin: 6px 0;
      padding: 8px 10px;
      border: 2px solid var(--line);
      border-radius: 14px;
      background: #fff;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }
    .review-empty { color: var(--muted); font-size: 12px; }
    .stage-hover-panel {
      max-height: 0;
      opacity: 0;
      overflow: hidden;
      transform: translateY(-6px);
      transition: max-height .42s cubic-bezier(.2,.8,.2,1), opacity .28s ease, transform .36s cubic-bezier(.2,.8,.2,1);
    }
    .stage-card:hover .stage-hover-panel, .stage-card:focus .stage-hover-panel, .stage-card:focus-within .stage-hover-panel {
      max-height: 420px;
      opacity: 1;
      transform: translateY(0);
    }
    .hover-photo {
      width: calc(100% - 56px);
      margin: 12px 0 0 calc(44px + 12px);
      aspect-ratio: 18 / 7;
      object-fit: cover;
      border: 2px solid var(--line);
      border-radius: 14px;
      background: #fff5f5;
    }
    .hover-photo.placeholder { display: grid; place-items: center; color: var(--muted); font-size: 13px; font-weight: 900; }
    .option-table { margin-top: 10px; margin-left: calc(44px + 12px); border: 2px solid rgba(26,26,26,.12); border-radius: 12px; overflow: hidden; }
    .option-table h4 { margin: 0; padding: 8px 10px; background: #fff; font-size: 12px; font-weight: 900; }
    .option-table table { width: 100%; border-collapse: collapse; table-layout: fixed; font-size: 12px; background: #fff; }
    .option-table .idx-col { width: 26px; }
    .option-table .place-col { width: auto; }
    .option-table .person-col { width: 52px; }
    .option-table .tail-col { width: 56px; }
    .option-table th, .option-table td { padding: 6px 5px; border-top: 1px solid rgba(26,26,26,.12); text-align: left; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .option-table thead th { color: var(--muted); font-weight: 900; }
    .option-table tbody th { width: 24px; color: var(--ink); }
    .transfer-sep {
      width: max-content;
      max-width: 86%;
      display: flex;
      align-items: center;
      gap: 8px;
      margin: -2px auto;
      padding: 6px 16px;
      border: 2px solid var(--line);
      border-radius: 999px;
      background: #fff;
      font-size: 13px;
      font-weight: 900;
      z-index: 2;
      position: relative;
    }
    .transfer-sep .mini-icon { width: 20px; height: 20px; }
    .transfer-sep span { color: var(--muted); }
    .alternatives { margin-top: 12px; }
    .alternatives summary {
      cursor: pointer;
      display: inline-flex;
      padding: 8px 12px;
      border: 2px solid var(--line);
      border-radius: 999px;
      background: var(--yellow);
      color: var(--ink);
      font-size: 13px;
      font-weight: 900;
    }
    .alternatives ol { list-style: none; padding: 0; margin: 10px 0 0; display: grid; gap: 8px; }
    .alternatives li {
      display: grid;
      grid-template-columns: 28px 1fr;
      gap: 8px;
      align-items: start;
      padding: 9px;
      border: 2px solid var(--line);
      border-radius: 14px;
      background: #fff;
      font-size: 12px;
      color: var(--ink);
    }
    .alternatives li > span {
      width: 22px;
      height: 22px;
      border: 2px solid var(--line);
      border-radius: 50%;
      display: grid;
      place-items: center;
      background: var(--red);
      color: #fff;
      font-weight: 900;
    }
    .alternatives small { display: block; margin-top: 3px; color: var(--muted); line-height: 1.35; }
    .map-wrap {
      position: sticky;
      top: 10px;
      height: calc(100vh - 32px);
      border: 2px solid var(--line);
      border-radius: 20px;
      background: #fff;
      overflow: hidden;
    }
    .map-wrap::before {
      content: "";
      position: absolute;
      inset: 18px;
      z-index: 2;
      border: 2px solid var(--line);
      border-radius: 20px;
      pointer-events: none;
    }
    #map { position: absolute; inset: 0; background: #fff; }
    .map-legend {
      position: absolute;
      left: 34px;
      bottom: 34px;
      z-index: 5;
      display: grid;
      gap: 10px;
      max-width: min(240px, calc(100% - 68px));
      padding: 14px;
      border: 2px solid var(--line);
      border-radius: 20px;
      background: rgba(255,255,255,.94);
      box-shadow: 5px 5px 0 var(--line);
      font-size: 12px;
      font-weight: 800;
    }
    .map-legend strong { font-size: 16px; }
    .legend-row { display: flex; align-items: center; gap: 8px; }
    .legend-badges { display: inline-flex; gap: 6px; min-width: 68px; }
    .legend-badges i {
      min-width: 22px;
      height: 22px;
      padding: 0 5px;
      display: grid;
      place-items: center;
      border-radius: 50%;
      color: #fff;
      font-style: normal;
      font-size: 12px;
      font-weight: 900;
    }
    .origin-set i { border-radius: 7px; }
    .origin-1 { background: var(--red); }
    .origin-2 { background: #8B5CF6; }
    .origin-3 { background: #F97316; }
    .origin-4 { background: var(--green); }
    .stage-1 { background: #3B82F6; }
    .stage-2 { background: var(--green); }
    .stage-3 { background: #F97316; }
    .stage-4 { background: #8B5CF6; }
    .stage-5 { background: #64748B; }
    .center-mark { min-width: 22px !important; width: 22px; padding: 0 !important; border-radius: 0 !important; background: var(--green); transform: rotate(45deg); }
    .center-mark { color: #fff; }
    .center-mark span { display: block; transform: rotate(-45deg); }
    .legend-line {
      width: 68px;
      height: 9px;
      border: 2px solid var(--line);
      border-radius: 999px;
      background: linear-gradient(90deg, var(--red), var(--yellow), var(--green));
      display: inline-block;
    }
    .mk {
      min-width: 36px;
      min-height: 36px;
      display: grid;
      place-items: center;
      border: 2px solid var(--line);
      color: var(--ink);
      text-align: center;
      font-size: 12px;
      font-weight: 900;
      box-shadow: 3px 3px 0 var(--line);
    }
    .mk span { line-height: 1; }
    .mk.origin { border-radius: 9px; background: var(--red); color: #fff; }
    .mk.origin.p2 { background: #8B5CF6; }
    .mk.origin.p3 { background: #F97316; }
    .mk.origin.p4 { background: var(--green); }
    .mk.center { border-radius: 0; background: var(--green); color: #fff; transform: rotate(45deg); }
    .mk.center span { transform: rotate(-45deg); }
    .mk.plan_a, .mk.plan_b, .mk.plan_c { border-radius: 50%; background: #9ca3af; color: #fff; }
    .mk.stage_1 { background: #3B82F6; color: #fff; }
    .mk.stage_2 { background: var(--green); color: #fff; }
    .mk.stage_3 { background: #F97316; color: #fff; }
    .mk.stage_4 { background: #8B5CF6; color: #fff; }
    .mk.stage_5 { background: #64748B; color: #fff; }
    .popup-card {
      max-width: 300px;
      padding: 4px;
      font-family: inherit;
      color: var(--ink);
    }
    .popup-title { font-size: 15px; font-weight: 900; margin-bottom: 6px; }
    .popup-meta { font-size: 12px; color: var(--muted); margin-bottom: 5px; line-height: 1.35; font-weight: 700; }
    .popup-photo { width: 100%; max-height: 150px; object-fit: cover; border: 2px solid var(--line); border-radius: 14px; margin-top: 7px; }
    .popup-noimg, .error {
      margin-top: 7px;
      padding: 14px;
      border: 2px solid var(--line);
      border-radius: 14px;
      background: var(--bg);
      color: #7b332a;
      font-size: 13px;
      font-weight: 800;
    }
    @keyframes popIn { from { opacity: 0; transform: translateY(10px) scale(.99); } to { opacity: 1; transform: translateY(0) scale(1); } }
    @media (max-width: 1180px) {
      .shell { grid-template-columns: 1fr; }
      .sidebar {
        height: auto;
        overflow: visible;
      }
      .panel { height: auto; overflow: visible; }
      .panel::after { display: none; }
      .map-wrap { position: relative; height: 58vh; }
      .brand { display: block; }
      .panel-title { display: block; }
      .participants { justify-content: flex-start; margin-top: 14px; }
      .stage-main { grid-template-columns: 1fr; }
      .option-table { margin-left: 0; }
    }
    @media (max-width: 560px) {
      .panel { padding: 20px 16px 28px; }
      .panel::before { margin: -20px -16px 16px; }
      h1 { font-size: 28px; }
      .tabs { grid-template-columns: 1fr; }
      .time-grid { grid-template-columns: repeat(2, minmax(90px, 1fr)); }
      .current-head { display: block; }
      .current-head p { margin-top: 8px; white-space: normal; }
      .map-legend { left: 18px; right: 18px; bottom: 18px; max-width: none; }
    }
"""

    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Meetpoint Planner CN</title>
  <style>{css}</style>
</head>
<body>
  <div class="shell">
    <aside class="sidebar" aria-label="行程设置">
      <header class="brand">
        <div>
          <p>Meetpoint Planner CN</p>
          <h1>多人聚会路线规划</h1>
          <small>让见面更公平，让相聚更轻松 😊</small>
        </div>
      </header>
      <section class="side-card">
        <h2>{svg_icon('people')}参与者（{len(participants)}人）</h2>
        <div class="person-list">{participant_cards}</div>
      </section>
      <section class="side-card settings-card">
        <h2>{svg_icon('gear')}行程设置</h2>
        <div><strong>阶段顺序</strong><span>{escape_html(' -> '.join(result['stage_intents']))}</span></div>
        <div><strong>预算偏好</strong><span>{escape_html(result['budget_pref'])}</span></div>
        <div><strong>氛围偏好</strong><span>{escape_html(result['vibe_text'] or '无')}</span></div>
      </section>
      <section class="tip-card">
        <h2>{svg_icon('bulb')}小贴士</h2>
        <p>我们根据通勤时间的平衡性与转场效率，为你推荐更优见面路线。</p>
      </section>
    </aside>
    <section class="panel" aria-label="路线方案">
      <div class="panel-title">
        <h2>路线方案（共 3 个） ✨</h2>
      </div>
      <nav class="tabs" aria-label="方案切换">{''.join(plan_buttons)}</nav>
      {''.join(plan_blocks)}
    </section>
    <main class="map-wrap" aria-label="路线地图">
      <div id="map"></div>
      <div class="map-legend" aria-label="地图图例">
        <strong>地图图例</strong>
        {legend_html}
      </div>
    </main>
  </div>
  {map_bootstrap}
  {ui_bootstrap}
</body>
</html>
"""

    path.write_text(html, encoding="utf-8")


def choose_option(question: str, options: list[tuple[str, str]]) -> str:
    print(f"\n{question}")
    for i, (label, desc) in enumerate(options, start=1):
        print(f"  {i}. {label} - {desc}")
    while True:
        ans = input("输入选项编号: ").strip()
        if ans.isdigit() and 1 <= int(ans) <= len(options):
            return options[int(ans) - 1][0]
        print("无效输入，请重试。")


def run_interactive_wizard() -> dict:
    print("\n[Meetpoint Planner 问答配置]")
    print("先采集关键偏好，再生成双方案路线。")

    people = int(choose_option("参与人数", [("2", "两个人"), ("3", "三个人"), ("4", "四个人")]))
    origins = [input(f"请输入第 {i + 1} 位出发点: ").strip() for i in range(people)]

    city = input("城市（建议填写，例如 苏州）: ").strip()
    stages = input("路线阶段（示例: 猫咖->吃饭->甜品）: ").strip() or "咖啡"
    vibe = input("氛围偏好（可空，示例: 安静/拍照/性价比）: ").strip()

    budget_pref = choose_option(
        "预算偏好",
        [("any", "不限制"), ("economy", "性价比"), ("mid", "中等预算"), ("premium", "品质优先")],
    )
    mode = choose_option("出行方式", [("transit", "公交地铁"), ("driving", "驾车"), ("walking", "步行")])
    max_each = float(choose_option("每人可接受最大通勤", [("45", "45分钟"), ("60", "60分钟"), ("75", "75分钟"), ("90", "90分钟")]))

    return {
        "origins": origins,
        "city": city,
        "stages": stages,
        "vibe": vibe,
        "budget": budget_pref,
        "mode": mode,
        "max_each_minutes": max_each,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan fair multi-stop meetup routes in China (2-4 people).")
    parser.add_argument("--origin", action="append", default=[], help="Repeat 2-4 times")
    parser.add_argument("--origin-a", default="")
    parser.add_argument("--origin-b", default="")
    parser.add_argument("--preference", default="", help="Backward-compatible single intent")
    parser.add_argument("--stages", default="", help="Stage chain, e.g. 猫咖->吃饭->甜品")
    parser.add_argument("--city", default="")
    parser.add_argument("--mode", choices=["driving", "walking", "transit"], default="transit")
    parser.add_argument("--budget", choices=["any", "economy", "mid", "premium"], default="any")
    parser.add_argument("--vibe", default="", help="Style keywords")

    parser.add_argument("--radius", type=int, default=7000)
    parser.add_argument("--per-anchor-limit", type=int, default=12)
    parser.add_argument("--evaluate-limit", type=int, default=24)
    parser.add_argument("--max-each-minutes", type=float, default=60.0)
    parser.add_argument("--option-topn", type=int, default=2, help="Backup options per stage in HTML")
    parser.add_argument("--output", default="meetpoint_itinerary.html")
    parser.add_argument("--interactive", action="store_true")

    parser.add_argument("--web-key", default=os.getenv("AMAP_WEB_KEY") or os.getenv("AMAP_API_KEY") or "")
    parser.add_argument("--js-key", default=os.getenv("AMAP_JS_KEY") or "")
    parser.add_argument("--js-security-code", default=os.getenv("AMAP_JS_SECURITY_CODE") or "")
    return parser.parse_args()


def collect_origins(args: argparse.Namespace) -> list[str]:
    if args.interactive:
        w = run_interactive_wizard()
        args.origin = w["origins"]
        args.city = w["city"]
        args.stages = w["stages"]
        args.vibe = w["vibe"]
        args.budget = w["budget"]
        args.mode = w["mode"]
        args.max_each_minutes = w["max_each_minutes"]

    origins = list(args.origin)
    if not origins and args.origin_a and args.origin_b:
        origins = [args.origin_a, args.origin_b]

    origins = [x.strip() for x in origins if x and x.strip()]
    if len(origins) < 2 or len(origins) > 4:
        raise RuntimeError("Please provide 2 to 4 participants (use repeated --origin).")
    return origins


def main() -> int:
    args = parse_args()
    if not args.web_key:
        print("Missing web key. Set AMAP_WEB_KEY/AMAP_API_KEY or pass --web-key.", file=sys.stderr)
        return 2

    try:
        origin_inputs = collect_origins(args)
    except RuntimeError as err:
        print(str(err), file=sys.stderr)
        return 2

    stage_intents = parse_stage_sequence(args.stages, args.preference)
    vibe_keywords = split_tokens(args.vibe)

    client = AMapClient(args.web_key)

    participants = []
    city_hint = args.city or None
    for idx, origin_text in enumerate(origin_inputs, start=1):
        geo = client.geocode(origin_text, city=city_hint)
        participants.append(
            {
                "label": f"P{idx}",
                "input": origin_text,
                "name": geo["name"],
                "location": geo["location"],
                "lng": geo["lng"],
                "lat": geo["lat"],
                "citycode": geo.get("citycode"),
            }
        )

    city_code = args.city or participants[0].get("citycode")
    centroid = centroid_location(participants)

    plan_a = build_plan(
        client=client,
        participants=participants,
        centroid=centroid,
        city_code=city_code,
        stage_intents=stage_intents,
        mode=args.mode,
        radius=args.radius,
        per_anchor_limit=args.per_anchor_limit,
        evaluate_limit=args.evaluate_limit,
        max_each_minutes=args.max_each_minutes,
        budget_pref=args.budget,
        vibe_keywords=vibe_keywords,
        variant="fairness",
        option_topn=args.option_topn,
        avoid_first_keys=None,
    )

    first_a = plan_a["stages"][0]["selected"] if plan_a.get("stages") else {}
    avoid_id = first_a.get("id")
    avoid_first_keys = {str(x) for x in [first_a.get("id"), first_a.get("location")] if x}
    plan_b = build_plan(
        client=client,
        participants=participants,
        centroid=centroid,
        city_code=city_code,
        stage_intents=stage_intents,
        mode=args.mode,
        radius=args.radius,
        per_anchor_limit=args.per_anchor_limit,
        evaluate_limit=args.evaluate_limit,
        max_each_minutes=args.max_each_minutes,
        budget_pref=args.budget,
        vibe_keywords=vibe_keywords,
        variant="compact",
        option_topn=args.option_topn,
        avoid_first_keys=None,
    )

    first_b = plan_b["stages"][0]["selected"] if plan_b.get("stages") else {}
    avoid_ids = {str(x) for x in [avoid_id, first_a.get("location"), first_b.get("id"), first_b.get("location")] if x}
    plan_c = build_plan(
        client=client,
        participants=participants,
        centroid=centroid,
        city_code=city_code,
        stage_intents=stage_intents,
        mode=args.mode,
        radius=args.radius,
        per_anchor_limit=args.per_anchor_limit,
        evaluate_limit=args.evaluate_limit,
        max_each_minutes=args.max_each_minutes,
        budget_pref=args.budget,
        vibe_keywords=vibe_keywords,
        variant="custom",
        option_topn=args.option_topn,
        avoid_first_keys=avoid_ids if avoid_ids else None,
    )

    result = {
        "participants": participants,
        "centroid": centroid,
        "stage_intents": stage_intents,
        "budget_pref": args.budget,
        "vibe_text": args.vibe,
        "mode": args.mode,
        "plans": [plan_a, plan_b, plan_c],
    }

    out_path = Path(args.output).expanduser().resolve()
    render_html(out_path, result, js_key=args.js_key or None, js_security_code=args.js_security_code or None)

    print(f"Output HTML: {out_path}")
    print("方案A（公平优先）:")
    for st in plan_a["stages"]:
        print(
            f"  - {st['index']}. {st['intent']}: {st['selected'].get('name', '候选点')} | "
            f"{format_stage_summary(st)}"
        )
    print("方案B（紧凑体验优先）:")
    for st in plan_b["stages"]:
        print(
            f"  - {st['index']}. {st['intent']}: {st['selected'].get('name', '候选点')} | "
            f"{format_stage_summary(st)}"
        )
    print("方案C（偏好匹配优先）:")
    for st in plan_c["stages"]:
        print(
            f"  - {st['index']}. {st['intent']}: {st['selected'].get('name', '候选点')} | "
            f"{format_stage_summary(st)}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
