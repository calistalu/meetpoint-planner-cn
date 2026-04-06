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
    seen = set()
    out = []
    for poi in items:
        key = poi.get("id") or f"{poi.get('name', '')}:{poi.get('location', '')}"
        if key in seen:
            continue
        seen.add(key)
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
        return c["transfer_minutes"] * 1.35 + c["gap"] * 1.9 + c["avg_time"] * 0.15 - c["quality"]

    # compact variant
    if stage_index == 0:
        return c["gap"] * 2.0 + c["std"] * 0.8 + c["avg_time"] * 0.22 - c["quality"] * 1.08
    return c["transfer_minutes"] * 2.2 + c["gap"] * 1.4 + c["avg_time"] * 0.12 - c["quality"] * 1.1


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
) -> dict | None:
    destination = poi.get("location")
    if not destination:
        return None

    times = []
    times_by_label = {}
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

    transfer_minutes = 0.0
    if previous_location:
        try:
            transfer_minutes = client.route_minutes(previous_location, destination, mode="walking", city=city)
        except RuntimeError:
            try:
                transfer_minutes = client.route_minutes(previous_location, destination, mode="driving", city=city)
            except RuntimeError:
                transfer_minutes = 999.0

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
        "transfer_minutes": round(transfer_minutes, 1),
        "semantic_bonus": round(sem, 2),
        "budget_bonus": round(b_bonus, 2),
        "vibe_bonus": round(v_bonus, 2),
        "rating_bonus": round(r_bonus, 2),
        "quality": round(quality, 2),
        "cost": round(cost, 1) if cost else 0.0,
        "rating": round(rating, 1) if rating else 0.0,
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
        )
        if item:
            item["stage_intent"] = stage_intent
            evaluated.append(item)

    return evaluated


def build_reason(stage_index: int, selected: dict) -> str:
    reasons = []
    reasons.append(f"各位通勤时间：{format_times(selected.get('times', {}))}。")

    if stage_index > 0:
        t = selected.get("transfer_minutes", 0)
        if t <= 15:
            reasons.append(f"与上一站衔接紧凑，预计 {t} 分钟可到。")
        elif t <= 30:
            reasons.append(f"与上一站距离中等，预计 {t} 分钟可到。")
        else:
            reasons.append(f"与上一站较远（约 {t} 分钟），但该类型优质备选较集中。")

    if selected.get("cost", 0) > 0:
        reasons.append(f"人均参考 ¥{selected['cost']:.0f}。")
    if selected.get("rating", 0) > 0:
        reasons.append(f"评分参考 {selected['rating']:.1f}/5。")

    return " ".join(reasons)


def format_times(times: dict) -> str:
    if not times:
        return "暂无"
    parts = [f"{k} {v} 分钟" for k, v in times.items()]
    return " | ".join(parts)


def select_option(options: list[dict], variant: str, stage_index: int, avoid_id: str | None = None) -> dict:
    scored = []
    for c in options:
        c2 = dict(c)
        c2["variant_score"] = round(score_candidate(c2, variant=variant, stage_index=stage_index), 3)
        scored.append(c2)
    scored.sort(key=lambda x: x["variant_score"])

    if avoid_id and scored and scored[0].get("id") == avoid_id and len(scored) > 1:
        return scored[1]
    return scored[0]


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
    avoid_first_id: str | None = None,
) -> dict:
    stages_out = []
    previous = None

    for i, intent in enumerate(stage_intents):
        kw = expand_intent_keywords(intent)
        anchors = [centroid] + ([previous] if previous else [p["location"] for p in participants])

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
            )

        if not options:
            raise RuntimeError(f"No options for stage: {intent}")

        selected = select_option(options, variant=variant, stage_index=i, avoid_id=avoid_first_id if i == 0 else None)
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

    avg_gap = round(sum(s["selected"]["gap"] for s in stages_out) / len(stages_out), 1)
    total_transfer = round(sum(s["selected"].get("transfer_minutes", 0.0) for s in stages_out[1:]), 1)

    return {
        "variant": variant,
        "avg_gap": avg_gap,
        "total_transfer": total_transfer,
        "stages": stages_out,
    }


def make_navigation_url(src: dict, dst: dict) -> str:
    q = {
        "from": f"{src['location']},{src['name']}",
        "to": f"{dst['location']},{dst.get('name', '候选点')}",
        "mode": "car",
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

    marker_data = []
    p_badges = ["🧡", "💙", "💚", "💜"]
    for i, p in enumerate(participants):
        marker_data.append(
            {
                "role": "participant",
                "badge": f"{p_badges[i]}{p['label']}",
                "name": p["name"],
                "location": p["location"],
                "popup_html": f"<div class='popup-title'>{escape_html(p['name'])}</div><div class='popup-meta'>{p['label']} 出发点</div>",
            }
        )

    marker_data.append(
        {
            "role": "center",
            "badge": "🎯C",
            "name": "几何中心(仅参考)",
            "location": centroid,
            "popup_html": "<div class='popup-title'>几何中心（仅参考）</div><div class='popup-meta'>用于扩展候选池</div>",
        }
    )

    plan_tags = {"fairness": "🅰", "compact": "🅱"}
    for plan in plans:
        tag = plan_tags.get(plan["variant"], "🅿")
        for stage in plan["stages"]:
            s = stage["selected"]
            image_html = f"<img class='popup-photo' src='{escape_html(s.get('photo') or '')}' alt='门店图片' />" if s.get("photo") else "<div class='popup-noimg'>暂无门店图片</div>"
            marker_data.append(
                {
                    "role": "plan_a" if plan["variant"] == "fairness" else "plan_b",
                    "badge": f"{tag}{stage['index']}",
                    "name": s.get("name", "候选点"),
                    "location": s["location"],
                    "popup_html": (
                        f"<div class='popup-title'>{escape_html(s.get('name', '候选点'))}</div>"
                        f"<div class='popup-meta'>阶段 {stage['index']}：{escape_html(stage['intent'])}</div>"
                        f"<div class='popup-meta'>{escape_html(stage['selected']['reason'])}</div>"
                        f"{image_html}"
                    ),
                }
            )

    def stage_html(plan: dict, stage: dict) -> str:
        s = stage["selected"]
        option_rows = []
        for idx, opt in enumerate(stage["options"], start=1):
            option_rows.append(
                f"<li><strong>{idx}. {escape_html(opt.get('name', '候选点'))}</strong>"
                f" <span class='meta2'>{escape_html(format_times(opt.get('times', {})))} | 转场 {opt.get('transfer_minutes', 0)} 分钟 | 评分 {opt['variant_score']}</span></li>"
            )
        return (
            f"<div class='stage-card'>"
            f"<div class='stage-title'>阶段 {stage['index']} · {escape_html(stage['intent'])}</div>"
            f"<div><strong>{escape_html(s.get('name', '候选点'))}</strong></div>"
            f"<div class='meta2'>{escape_html(s.get('address', ''))}</div>"
            f"<div class='meta2'>推荐理由：{escape_html(s['reason'])}</div>"
            f"<div class='meta2'>通勤时间：{escape_html(format_times(s.get('times', {})))} | 转场 {s.get('transfer_minutes', 0)} 分钟</div>"
            f"<div class='opt-title'>本阶段备选（可精细替换）</div>"
            f"<ul class='opt-list'>{''.join(option_rows)}</ul>"
            f"</div>"
        )

    plan_blocks = []
    for plan in plans:
        title = "方案A（公平优先）" if plan["variant"] == "fairness" else "方案B（紧凑体验优先）"
        plan_blocks.append(
            f"<section class='plan-block'>"
            f"<h2>{title}</h2>"
            f"<div class='meta'>总转场：{plan['total_transfer']} 分钟（每阶段展示每位参与者通勤时间）</div>"
            f"{''.join(stage_html(plan, st) for st in plan['stages'])}"
            f"</section>"
        )

    security_line = ""
    if js_security_code:
        security_line = f"window._AMapSecurityConfig = {{securityJsCode: '{js_security_code}'}};"

    if js_key:
        map_bootstrap = f"""
<script>
{security_line}
</script>
<script src=\"https://webapi.amap.com/loader.js\"></script>
<script>
const markerData = {json.dumps(marker_data, ensure_ascii=False)};
const center = [{centroid.split(',')[0]}, {centroid.split(',')[1]}];

AMapLoader.load({{
  key: {json.dumps(js_key, ensure_ascii=False)},
  version: "2.0"
}}).then((AMap) => {{
  const map = new AMap.Map("map", {{ zoom: 11, center }});
  const infoWindow = new AMap.InfoWindow({{ offset: new AMap.Pixel(0, -20) }});
  markerData.forEach((m) => {{
    const [lng, lat] = m.location.split(",").map(Number);
    const marker = new AMap.Marker({{
      position: [lng, lat],
      title: m.name,
      label: {{ content: `<div class='mk ${{m.role}}'>${{m.badge}}</div>`, direction: "top" }}
    }});
    marker.on("click", () => {{
      infoWindow.setContent(`<div class='popup-card'>${{m.popup_html || ''}}</div>`);
      infoWindow.open(map, [lng, lat]);
    }});
    map.add(marker);
  }});
}}).catch((e) => {{
  document.getElementById("map").innerHTML = `<div class='error'>地图加载失败: ${{String(e)}}</div>`;
}});
</script>
"""
    else:
        map_bootstrap = """
<script>
document.getElementById("map").innerHTML = "<div class='error'>未提供 AMAP_JS_KEY，已仅输出路线规划文本。</div>";
</script>
"""

    participant_meta = "".join(f"<div class='meta'>{p['label']}: {escape_html(p['name'])}</div>" for p in participants)

    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Meetpoint Planner CN - Itinerary</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; color: #1b1f23; }}
    .layout {{ display: grid; grid-template-columns: 480px 1fr; min-height: 100vh; }}
    .panel {{ padding: 16px; border-right: 1px solid #e7eaf0; overflow: auto; background: #f8fafc; }}
    #map {{ min-height: 100vh; background: #f1f5f9; }}
    h1 {{ margin: 0 0 8px 0; font-size: 20px; }}
    h2 {{ margin: 0 0 8px 0; font-size: 16px; }}
    .meta {{ margin-bottom: 6px; font-size: 13px; color: #475569; }}
    .meta2 {{ margin-top: 4px; font-size: 12px; color: #4b5563; line-height: 1.4; }}
    .plan-block {{ margin-top: 14px; padding: 12px; border: 1px solid #e2e8f0; border-radius: 12px; background: #fff; }}
    .stage-card {{ margin-top: 10px; padding: 10px; border: 1px solid #e5e7eb; border-radius: 10px; background: #fcfdff; }}
    .stage-title {{ font-weight: 700; font-size: 13px; margin-bottom: 4px; color: #0f172a; }}
    .opt-title {{ margin-top: 8px; font-size: 12px; color: #334155; }}
    .opt-list {{ margin: 6px 0 0 0; padding-left: 18px; }}
    .opt-list li {{ margin: 4px 0; font-size: 12px; color: #334155; }}
    .mk {{ display: inline-block; min-width: 30px; text-align: center; color: #fff; border-radius: 12px; padding: 3px 8px; font-size: 12px; box-shadow: 0 2px 8px rgba(15,23,42,0.18); }}
    .mk.participant {{ background: #fb7185; }}
    .mk.center {{ background: #f59e0b; }}
    .mk.plan_a {{ background: #0ea5e9; }}
    .mk.plan_b {{ background: #22c55e; }}
    .popup-card {{ max-width: 290px; }}
    .popup-title {{ font-size: 14px; font-weight: 700; color: #1f2937; margin-bottom: 4px; }}
    .popup-meta {{ font-size: 12px; color: #4b5563; margin-bottom: 4px; line-height: 1.35; }}
    .popup-photo {{ width: 100%; max-height: 140px; object-fit: cover; border-radius: 8px; margin-top: 6px; border: 1px solid #e2e8f0; }}
    .popup-noimg {{ width: 100%; margin-top: 6px; padding: 10px; border-radius: 8px; background: #f8fafc; border: 1px dashed #cbd5e1; font-size: 12px; color: #64748b; text-align: center; }}
    .error {{ padding: 18px; color: #991b1b; }}
    @media (max-width: 1100px) {{
      .layout {{ grid-template-columns: 1fr; }}
      #map {{ min-height: 55vh; }}
      .panel {{ border-right: none; border-bottom: 1px solid #e7eaf0; }}
    }}
  </style>
</head>
<body>
  <div class="layout">
    <aside class="panel">
      <h1>多站见面路线规划（2-4人）</h1>
      {participant_meta}
      <div class="meta">阶段需求: {escape_html(' -> '.join(result['stage_intents']))}</div>
      <div class="meta">预算偏好: {escape_html(result['budget_pref'])} | 氛围偏好: {escape_html(result['vibe_text'] or '无')}</div>
      {''.join(plan_blocks)}
    </aside>
    <main id="map"></main>
  </div>
  {map_bootstrap}
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
    parser.add_argument("--option-topn", type=int, default=5, help="Options per stage in HTML")
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
        avoid_first_id=None,
    )

    avoid_id = plan_a["stages"][0]["selected"].get("id") if plan_a.get("stages") else None
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
        avoid_first_id=avoid_id,
    )

    result = {
        "participants": participants,
        "centroid": centroid,
        "stage_intents": stage_intents,
        "budget_pref": args.budget,
        "vibe_text": args.vibe,
        "plans": [plan_a, plan_b],
    }

    out_path = Path(args.output).expanduser().resolve()
    render_html(out_path, result, js_key=args.js_key or None, js_security_code=args.js_security_code or None)

    print(f"Output HTML: {out_path}")
    print("方案A（公平优先）:")
    for st in plan_a["stages"]:
        print(
            f"  - {st['index']}. {st['intent']}: {st['selected'].get('name', '候选点')} | "
            f"{format_times(st['selected'].get('times', {}))} | 转场 {st['selected'].get('transfer_minutes', 0)} 分钟"
        )
    print("方案B（紧凑体验优先）:")
    for st in plan_b["stages"]:
        print(
            f"  - {st['index']}. {st['intent']}: {st['selected'].get('name', '候选点')} | "
            f"{format_times(st['selected'].get('times', {}))} | 转场 {st['selected'].get('transfer_minutes', 0)} 分钟"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
