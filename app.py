# -*- coding: utf-8 -*-
"""
국토부 아파트 매매 실거래가 조회 웹앱 (백엔드)
- 단지명 + 시/도(또는 시/군/구) + 기간을 받아 해당 지역들을 훑어 단지명으로 필터링
- 국토부 공공데이터포털 API를 서버에서 호출 → CORS 문제 없음, API키는 서버 환경변수에만 보관
"""
import os
import json
import re
import logging
from concurrent.futures import ThreadPoolExecutor
from xml.etree import ElementTree as ET

import requests
from flask import Flask, request, jsonify, Response

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("molit")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_URL = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev"
# 국토부 서비스키: 배포 시 환경변수로 설정 (일반 인증키 Decoding 값 권장)
SERVICE_KEY = os.environ.get("MOLIT_API_KEY", "").strip()
MAX_REQUESTS = 900          # 한 검색당 최대 (지역수 × 개월수) 호출 상한
REQUEST_TIMEOUT = 15
MAX_WORKERS = 10

with open(os.path.join(BASE_DIR, "region_codes.json"), encoding="utf-8") as f:
    REGIONS = json.load(f)

app = Flask(__name__, static_folder=None)


def _txt(item, *names):
    """XML item에서 여러 후보 태그명 중 첫 값 반환 (영문/국문 태그 모두 대응)."""
    for n in names:
        el = item.find(n)
        if el is not None and el.text is not None:
            return el.text.strip()
    return ""


def parse_items(xml_text):
    rows = []
    root = ET.fromstring(xml_text)
    header = root.find(".//header")
    if header is not None:
        code = _txt(header, "resultCode")
        msg = _txt(header, "resultMsg")
        if code and code not in ("00", "000"):
            raise RuntimeError(f"API 오류 {code}: {msg}")
    for item in root.iter("item"):
        amount = _txt(item, "dealAmount", "거래금액").replace(",", "").strip()
        y = _txt(item, "dealYear", "년")
        m = _txt(item, "dealMonth", "월")
        d = _txt(item, "dealDay", "일")
        rows.append({
            "aptNm": _txt(item, "aptNm", "아파트"),
            "umdNm": _txt(item, "umdNm", "법정동"),
            "jibun": _txt(item, "jibun", "지번"),
            "dealDate": f"{y}-{int(m):02d}-{int(d):02d}" if (y and m and d) else "",
            "amount": int(amount) if amount.isdigit() else None,  # 만원
            "area": _txt(item, "excluUseAr", "전용면적"),
            "floor": _txt(item, "floor", "층"),
            "buildYear": _txt(item, "buildYear", "건축년도"),
            "dealType": _txt(item, "dealingGbn", "거래유형"),
            "cancel": _txt(item, "cdealType", "해제여부"),
            "cancelDay": _txt(item, "cdealDay", "해제사유발생일"),
        })
    return rows


def fetch_one(code, ym):
    """단일 (지역코드, 년월) 조회 → 파싱된 행 리스트."""
    params = {
        "serviceKey": SERVICE_KEY,
        "LAWD_CD": code,
        "DEAL_YMD": ym,
        "numOfRows": "1000",
        "pageNo": "1",
    }
    r = requests.get(API_URL, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return parse_items(r.text)


def month_range(start_ym, end_ym):
    sy, sm = int(start_ym[:4]), int(start_ym[4:6])
    ey, em = int(end_ym[:4]), int(end_ym[4:6])
    out = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        out.append(f"{y}{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def norm(s):
    return re.sub(r"\s+", "", s or "").lower()


def gather_rows(codes, months):
    """여러 (지역코드, 년월) 조합을 병렬 조회해 전체 행과 오류 목록 반환."""
    tasks = [(c, ym) for c in codes for ym in months]
    all_rows, errors = [], []

    def work(t):
        code, ym = t
        try:
            return fetch_one(code, ym)
        except Exception as e:  # noqa
            msg = f"{code}/{ym}: {e}"
            log.warning("조회 오류 %s", msg)
            errors.append(msg)
            return []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for rows in ex.map(work, tasks):
            all_rows.extend(rows)
    return all_rows, errors, len(tasks)


def resolve_codes(sido, sigungu):
    if sigungu:
        return list(REGIONS[sido][sigungu])
    return [c for cs in REGIONS[sido].values() for c in cs]


@app.route("/api/regions")
def api_regions():
    out = {sido: list(gus.keys()) for sido, gus in REGIONS.items()}
    return jsonify(out)


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True, "keySet": bool(SERVICE_KEY)})


@app.route("/api/search", methods=["POST"])
def api_search():
    if not SERVICE_KEY:
        return jsonify({"error": "서버에 국토부 API 키(MOLIT_API_KEY)가 설정되지 않았습니다."}), 500
    data = request.get_json(force=True, silent=True) or {}
    complex_q = (data.get("complex") or "").strip()
    sido = (data.get("sido") or "").strip()
    sigungu = (data.get("sigungu") or "").strip()
    start_ym = re.sub(r"\D", "", data.get("start_ym") or "")
    end_ym = re.sub(r"\D", "", data.get("end_ym") or "")

    if not complex_q:
        return jsonify({"error": "단지명을 입력하세요."}), 400
    if sido not in REGIONS:
        return jsonify({"error": "시/도를 선택하세요."}), 400
    if len(start_ym) != 6 or len(end_ym) != 6:
        return jsonify({"error": "기간(년월)을 올바르게 지정하세요."}), 400
    if sigungu and sigungu not in REGIONS[sido]:
        return jsonify({"error": "시/군/구 값이 올바르지 않습니다."}), 400

    codes = resolve_codes(sido, sigungu)
    months = month_range(start_ym, end_ym)
    if not months:
        return jsonify({"error": "기간 순서를 확인하세요 (시작이 종료보다 이후)."}), 400
    if len(codes) * len(months) > MAX_REQUESTS:
        return jsonify({
            "error": f"조회 범위가 너무 큽니다({len(codes) * len(months)}건). "
                     f"시/군/구를 지정하거나 기간을 줄여주세요. (상한 {MAX_REQUESTS}건)"
        }), 400

    all_rows, errors, nreq = gather_rows(codes, months)
    q = norm(complex_q)
    results = [r for r in all_rows if q in norm(r["aptNm"])]
    results.sort(key=lambda x: x["dealDate"], reverse=True)

    return jsonify({
        "count": len(results),
        "rows": results,
        "queried": {"sido": sido, "sigungu": sigungu or "(전체)",
                     "codes": len(codes), "months": len(months),
                     "requests": nreq, "fetched": len(all_rows)},
        "errors": errors[:5],
    })


@app.route("/api/complexes", methods=["POST"])
def api_complexes():
    """자동완성용: 해당 지역·기간의 고유 단지명 목록."""
    if not SERVICE_KEY:
        return jsonify({"error": "서버에 국토부 API 키(MOLIT_API_KEY)가 설정되지 않았습니다."}), 500
    data = request.get_json(force=True, silent=True) or {}
    sido = (data.get("sido") or "").strip()
    sigungu = (data.get("sigungu") or "").strip()
    start_ym = re.sub(r"\D", "", data.get("start_ym") or "")
    end_ym = re.sub(r"\D", "", data.get("end_ym") or "")

    if sido not in REGIONS:
        return jsonify({"error": "시/도를 선택하세요."}), 400
    if not sigungu:
        return jsonify({"error": "자동완성은 시/군/구를 선택해야 빠르게 불러올 수 있어요."}), 400
    if sigungu not in REGIONS[sido]:
        return jsonify({"error": "시/군/구 값이 올바르지 않습니다."}), 400
    if len(start_ym) != 6 or len(end_ym) != 6:
        return jsonify({"error": "기간(년월)을 지정하세요."}), 400

    months = month_range(start_ym, end_ym)
    if not months:
        return jsonify({"error": "기간 순서를 확인하세요."}), 400
    codes = resolve_codes(sido, sigungu)
    if len(codes) * len(months) > MAX_REQUESTS:
        return jsonify({"error": "기간이 너무 깁니다. 줄여주세요."}), 400

    all_rows, errors, _ = gather_rows(codes, months)
    names = sorted({r["aptNm"] for r in all_rows if r["aptNm"]})
    return jsonify({"names": names, "count": len(names), "errors": errors[:5]})


@app.route("/")
def index():
    with open(os.path.join(BASE_DIR, "index.html"), encoding="utf-8") as f:
        return Response(f.read(), mimetype="text/html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False)
