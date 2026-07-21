#!/usr/bin/env python3
# Talentio API client (cloud-portable / stdlib only).
# Secrets from env: TALENTIO_TOKEN (required), EVALUATOR_MAP (JSON, required for stagesync).
# Usage:
#   python3 talentio_client.py new --since today
#   python3 talentio_client.py detail --id 5225009
#   python3 talentio_client.py stagesync --hours 18 [--dry-run]
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
        req_name = c.get("requisition", {}).get("name") if isinstance(c.get("requisition"), dict) else ""
        who = "id=%s %s%s [%s]" % (c.get("id"), c.get("lastName") or "", c.get("firstName") or "", req_name)
        if len(c.get("stages") or []) > 0:
            skipped.append(who + " (stage already set)"); continue
        if c.get("status") != "ongoing":
            skipped.append(who + " (status=%s)" % c.get("status")); continue
        num = _pos_num(req_name)
        evs = _evaluators_for(num, emap)
        if not evs:
            skipped.append(who + " (no evaluator map for req %s)" % num); continue
        reg = _parse_dt(c.get("registeredAt"))
        reg_jst = reg.astimezone(JST) if reg else datetime.datetime.now(JST)
        day = reg_jst.date() if reg_jst.hour < 17 else (reg_jst.date() + datetime.timedelta(days=1))
        if day < today:
            day = today
        sched = "%sT10:00:00+09:00" % day.isoformat()
        ev_names = "+".join(e.get("name", "?") for e in evs)
        if dry_run:
            staged.append("[DRYRUN] %s -> %s date=%s" % (who, ev_names, day.isoformat())); continue
        body = {"evaluations": [{"employee": e["email"], "formTemplateId": RESUME_FORM_ID} for e in evs],
                "type": "resume", "scheduledAt": sched}
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
            staged.append("[STAGED] %s -> %s date=%s url=%s" % (who, ev_names, day.isoformat(), c.get("url")))
        else:
            warn.append("[FORM-WARN] %s -> %s date=%s (expected form %s got %s) url=%s" % (who, ev_names, day.isoformat(), RESUME_FORM_ID, applied, c.get("url")))
    print(json.dumps({"mode": "dry-run" if dry_run else "live",
                      "staged": staged, "skipped_count": len(skipped), "skipped": skipped, "warn": warn},
                     ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="action", required=True)
    n = sub.add_parser("new"); n.add_argument("--since", required=True)
    d = sub.add_parser("detail"); d.add_argument("--id", required=True)
    s = sub.add_parser("stagesync"); s.add_argument("--hours", type=float, default=18); s.add_argument("--dry-run", action="store_true")
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


if __name__ == "__main__":
    main()
