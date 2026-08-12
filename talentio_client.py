#!/usr/bin/env python3
# Talentio API client (cloud-portable / stdlib only).
# Secrets from env: TALENTIO_TOKEN (required), EVALUATOR_MAP (JSON, required for stagesync).
# Usage:
#   python3 talentio_client.py new --since today
#   python3 talentio_client.py detail --id 5225009
#   python3 talentio_client.py stagesync --hours 18 [--dry-run]
#   python3 talentio_client.py tagsync --hours 168 [--dry-run]
#   python3 talentio_client.py rejected --days 14
import os, sys, json, argparse, datetime
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

BASE = "https://talentio.com/api/v1"
JST = datetime.timezone(datetime.timedelta(hours=9))
RESUME_FORM_ID = 717  # 書類選考「合格 or 不合格」評価フォーム


def _token():
    t = os.environ.get("TALENTIO_TOKEN", "").strip()
    if not t:
        sys.exit("[ERROR] env var TALENTIO_TOKEN is not set")
    return t


def _get(path):
    req = Request(BASE + path, headers={"Authorization": "Bearer " + _token()})
    try:
        with urlopen(req, timeout=60) as r:
            headers = {k.lower(): v for k, v in r.getheaders()}
            return headers, json.loads(r.read().decode("utf-8"))
    except HTTPError as e:
        sys.exit("[API ERROR] GET %s -> HTTP %s: %s" % (path, e.code, e.read().decode("utf-8", "ignore")[:200]))
    except URLError as e:
        sys.exit("[API ERROR] GET %s -> %s" % (path, e))


def _post(path, body):
    data = json.dumps(body).encode("utf-8")
    req = Request(BASE + path, data=data, method="POST",
                  headers={"Authorization": "Bearer " + _token(), "Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except HTTPError as e:
        return {"_error": "HTTP %s: %s" % (e.code, e.read().decode("utf-8", "ignore")[:200])}
    except URLError as e:
        return {"_error": str(e)}


def _patch(path, body):
    # Tag writes use PATCH /candidates/{id} with "tagNames" (verified 2026-08-10).
    # NOTE: tagNames is a FULL OVERWRITE - always send the complete desired set.
    data = json.dumps(body).encode("utf-8")
    req = Request(BASE + path, data=data, method="PATCH",
                  headers={"Authorization": "Bearer " + _token(), "Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=60) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except HTTPError as e:
        return {"_error": "HTTP %s: %s" % (e.code, e.read().decode("utf-8", "ignore")[:200])}
    except URLError as e:
        return {"_error": str(e)}


def _parse_dt(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s)
    except ValueError:
        return None


def _since_dt(since):
    if since == "today":
        return datetime.datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)
    d = datetime.datetime.fromisoformat(since)
    return d.replace(tzinfo=JST) if d.tzinfo is None else d


def get_new_since(since_dt):
    headers, _rows = _get("/candidates?page=1")
    total = int(headers.get("x-total") or 0)
    last_page = (total + 99) // 100
    matched = []
    p = last_page
    while p >= 1:
        _, rows = _get("/candidates?page=%d" % p)
        if not rows:
            p -= 1
            continue
        hits = [c for c in rows if (_parse_dt(c.get("registeredAt")) or datetime.datetime.min.replace(tzinfo=JST)) >= since_dt]
        if hits:
            matched = hits + matched
        else:
            break
        p -= 1
    return matched


def _pos_num(name):
    import re
    m = re.search(r"[【](\d+)[】]", name or "")
    return m.group(1) if m else None


def _evaluators_for(num, emap):
    entry = emap.get(num)
    if not entry:
        return None
    if isinstance(entry, dict) and entry.get("evaluators"):
        return entry["evaluators"]
    if isinstance(entry, dict) and entry.get("email"):
        return [entry]
    return None


def stagesync(hours, dry_run):
    emap_raw = os.environ.get("EVALUATOR_MAP", "").strip()
    if not emap_raw:
        sys.exit("[ERROR] env var EVALUATOR_MAP (JSON) is not set")
    emap = json.loads(emap_raw)
    since_dt = datetime.datetime.now(JST) - datetime.timedelta(hours=hours)
    today = datetime.datetime.now(JST).date()
    cands = get_new_since(since_dt)
    staged, skipped, warn = [], [], []
    for c in cands:
        # 対象範囲（2026-07-21変更）：エージェント経由のみ（channelType=agent）。
        # ビズリーチ等スカウト媒体は選定〜返信〜面談調整に人の判断が介在するため除外。
        is_agent = c.get("channelType") == "agent"
        if not is_agent:
            continue
        ag = c.get("agentCompany", {}).get("name") if isinstance(c.get("agentCompany"), dict) else ""
        route = "agent:%s" % ag if ag else "agent"
        req_name = c.get("requisition", {}).get("name") if isinstance(c.get("requisition"), dict) else ""
        who = "id=%s %s%s [%s] route=%s" % (c.get("id"), c.get("lastName") or "", c.get("firstName") or "", req_name, route)
        if len(c.get("stages") or []) > 0:
            skipped.append(who + " (stage already set)"); continue
        if c.get("status") != "ongoing":
            skipped.append(who + " (status=%s)" % c.get("status")); continue
        num = _pos_num(req_name)
        evList = _evaluators_for(num, emap)
        if not evList:
            skipped.append(who + " (no evaluator map for req %s)" % num); continue
        evNames = "+".join(e.get("name", "?") for e in evList)
        reg = _parse_dt(c.get("registeredAt"))
        reg_jst = reg.astimezone(JST) if reg else datetime.datetime.now(JST)
        day = reg_jst.date() if reg_jst.hour < 17 else (reg_jst.date() + datetime.timedelta(days=1))
        if day < today:
            day = today
        sched = "%sT10:00:00+09:00" % day.isoformat()
        if dry_run:
            staged.append("[DRYRUN] %s -> %s date=%s" % (who, evNames, day.isoformat())); continue
        evaluations = [{"employee": e["email"], "formTemplateId": RESUME_FORM_ID} for e in evList]
        body = {"evaluations": evaluations, "type": "resume", "scheduledAt": sched}
        resp = _post("/candidates/%s/stages" % c.get("id"), body)
        if resp.get("_error"):
            warn.append("%s POST failed: %s" % (who, resp["_error"])); continue
        _, chk = _get("/candidates/%s" % c.get("id"))
        st = (chk.get("stages") or [])
        applied = None
        if st:
            evals = st[-1].get("evaluations") or []
            if evals:
                applied = evals[0].get("formTemplateId")
        if str(applied) == str(RESUME_FORM_ID):
            staged.append("[STAGED] %s -> %s date=%s url=%s" % (who, evNames, day.isoformat(), c.get("url")))
        else:
            warn.append("[FORM-WARN] %s -> %s date=%s (expected form %s got %s) url=%s" % (who, evNames, day.isoformat(), RESUME_FORM_ID, applied, c.get("url")))
    print(json.dumps({"mode": "dry-run" if dry_run else "live",
                      "staged": staged, "skipped_count": len(skipped), "skipped": skipped, "warn": warn},
                     ensure_ascii=False, indent=2))


# ---- Tag reconciliation (API-based; replaces browser automation) ----
# 設計方針（2026-08-11 改訂）:
#   AIが管理するのは「求人番号・チャネルから一意に決まる属性タグ」だけ。
#   選考フェーズのタグ（通過－書類選考／カジュアル面談済み／不合格－1次面接 等）は
#   積み上げ/置換の運用が人により異なり、実運用の語彙も網羅できないため **一切触らない**。
MANAGED_TAGS = ["中途", "28卒", "28卒-ビジネス", "28卒-エンジニア", "スカウト経由"]
SCOUT_CHANNELS = ["ビズリーチ", "LAPRAS", "Findy", "YOUTRUST", "転職ドラフト", "bizreach", "lapras", "findy"]


def _desired_tags(c):
    """求人番号とチャネルから一意に決まる属性タグのみを算出する。"""
    req_name = (c.get("requisition") or {}).get("name") or ""
    ch_name = c.get("channelName") or ""
    ch_type = c.get("channelType") or ""
    want = set()
    if "28卒" in req_name:
        want.add("28卒")
        if any(k in req_name for k in ["ビジネス", "BPO", "セールス"]):
            want.add("28卒-ビジネス")
        elif any(k in req_name for k in ["エンジニア", "3days"]):
            want.add("28卒-エンジニア")
    elif _pos_num(req_name):
        want.add("中途")
    if ch_type != "agent" and any(s in ch_name for s in SCOUT_CHANNELS):
        want.add("スカウト経由")
    return want


def tagsync(hours, dry_run):
    since_dt = datetime.datetime.now(JST) - datetime.timedelta(hours=hours)
    cands = get_new_since(since_dt)
    changed, skipped, errors = [], 0, []
    for c in cands:
        ch_name = c.get("channelName") or ""
        is_biz = ("ビズリーチ" in ch_name) or ("bizreach" in ch_name.lower())
        if not (c.get("channelType") == "agent" or is_biz):
            continue
        cur = [t.get("name") for t in (c.get("tags") or []) if t.get("name")]
        want = _desired_tags(c)
        keep = [t for t in cur if t not in MANAGED_TAGS]
        final = sorted(set(keep) | want)
        if sorted(set(cur)) == final:
            skipped += 1
            continue
        add = sorted(want - set(cur))
        remove = sorted([t for t in cur if t in MANAGED_TAGS and t not in want])
        who = "id=%s %s%s [%s]" % (c.get("id"), c.get("lastName") or "", c.get("firstName") or "",
                                   (c.get("requisition") or {}).get("name") or "")
        label = "%s  +%s  -%s" % (who, add or "-", remove or "-")
        if dry_run:
            changed.append("[DRYRUN] " + label)
            continue
        resp = _patch("/candidates/%s" % c.get("id"), {"tagNames": final})
        if resp.get("_error"):
            errors.append("%s : %s" % (who, resp["_error"]))
            continue
        _, chk = _get("/candidates/%s" % c.get("id"))
        now = sorted({t.get("name") for t in (chk.get("tags") or []) if t.get("name")})
        if now == final:
            changed.append("[OK] " + label)
        else:
            errors.append("[VERIFY-FAIL] %s expected=%s got=%s" % (who, final, now))
    print(json.dumps({"mode": "dry-run" if dry_run else "live",
                      "changed": changed, "unchanged": skipped, "errors": errors},
                     ensure_ascii=False, indent=2))


# ---- Rejected candidates (for farewell-mail drafting) ----
def rejected(days):
    """エージェント経由で書類選考お見送り(resume fail)が確定した候補者を列挙する。
    stageStatuses の有効値は fail / pass / on_evaluating / evaluated_all（2026-08-11検証）。
    ※「未処理かどうか」は呼び出し側がNotion判定ログDBの記録で判断する（時間で近似しない）。
    """
    since = datetime.datetime.now(JST) - datetime.timedelta(days=days)
    out = []
    headers, _first = _get("/candidates?stageStatuses=fail&page=1")
    total = int(headers.get("x-total") or 0)
    last_page = max(1, (total + 99) // 100)
    scanned_pages = 0
    p = last_page
    while p >= 1 and scanned_pages < 30:
        _, rows = _get("/candidates?stageStatuses=fail&page=%d" % p)
        rows = [] if not rows else ([rows] if isinstance(rows, dict) else rows)
        scanned_pages += 1
        if not rows:
            p -= 1
            continue
        newest_reg = max([(_parse_dt(c.get("registeredAt")) or datetime.datetime.min.replace(tzinfo=JST)) for c in rows])
        for c in rows:
            if c.get("channelType") != "agent":
                continue
            r = None
            for s in (c.get("stages") or []):
                if s.get("type") == "resume" and s.get("status") == "fail":
                    r = s
            if not r or not r.get("fixedAt"):
                continue
            fx = _parse_dt(r.get("fixedAt"))
            if not fx or fx < since:
                continue
            # 評価コメント(理由)は一覧APIに含まれないため、詳細APIから取得する
            ev_reason = ""
            _, detail = _get("/candidates/%s" % c.get("id"))
            dr = None
            for s in ((detail or {}).get("stages") or []):
                if s.get("type") == "resume" and s.get("status") == "fail":
                    dr = s
            for e in ((dr or {}).get("evaluations") or []):
                for it in (e.get("items") or []):
                    if "理由" in (it.get("name") or ""):
                        cm = (it.get("comment") or "").strip()
                        if cm:
                            ev_reason = (ev_reason + "\n" + cm).strip()
            out.append({
                "id": c.get("id"),
                "name": ((c.get("lastName") or "") + (c.get("firstName") or "")),
                "position": (c.get("requisition") or {}).get("name"),
                "agentCompany": (c.get("agentCompany") or {}).get("name"),
                "agentPerson": (c.get("agent") or {}).get("name"),
                "fixedAt": r.get("fixedAt"),
                "reason": ev_reason,
                "url": c.get("url"),
            })
        if newest_reg < (since - datetime.timedelta(days=60)):
            break
        p -= 1
    out.sort(key=lambda x: x["fixedAt"], reverse=True)
    print(json.dumps({"count": len(out), "candidates": out}, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="action", required=True)
    n = sub.add_parser("new"); n.add_argument("--since", required=True)
    d = sub.add_parser("detail"); d.add_argument("--id", required=True)
    s = sub.add_parser("stagesync"); s.add_argument("--hours", type=float, default=18); s.add_argument("--dry-run", action="store_true")
    g = sub.add_parser("tagsync"); g.add_argument("--hours", type=float, default=168); g.add_argument("--dry-run", action="store_true")
    rj = sub.add_parser("rejected"); rj.add_argument("--days", type=float, default=30)
    args = ap.parse_args()

    if args.action == "new":
        cands = get_new_since(_since_dt(args.since))
        out = [{"id": c.get("id"), "name": (c.get("lastName") or "") + (c.get("firstName") or ""),
                "registeredAt": c.get("registeredAt"), "requisition": (c.get("requisition") or {}).get("name"),
                "channelType": c.get("channelType"), "channelName": c.get("channelName"),
                "agentCompany": (c.get("agentCompany") or {}).get("name"), "stages": len(c.get("stages") or []),
                "status": c.get("status"), "url": c.get("url")} for c in cands]
        print(json.dumps({"count": len(out), "candidates": out}, ensure_ascii=False, indent=2))
    elif args.action == "detail":
        _, c = _get("/candidates/%s" % args.id)
        print(json.dumps(c, ensure_ascii=False, indent=2))
    elif args.action == "stagesync":
        stagesync(args.hours, args.dry_run)
    elif args.action == "tagsync":
        tagsync(args.hours, args.dry_run)
    elif args.action == "rejected":
        rejected(args.days)


if __name__ == "__main__":
    main()
