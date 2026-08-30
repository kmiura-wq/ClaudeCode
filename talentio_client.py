#!/usr/bin/env python3
# Talentio API client (cloud-portable / stdlib only).
# Secrets from env: TALENTIO_TOKEN (required), EVALUATOR_MAP (JSON, required for stagesync).
# Usage:
#   python3 talentio_client.py new --since today
#   python3 talentio_client.py detail --id 5225009
#   python3 talentio_client.py stagesync --hours 18 [--dry-run]
#   python3 talentio_client.py tagsync --hours 168 [--dry-run]
#   python3 talentio_client.py rejected --days 14
#   python3 talentio_client.py pending --hours 2 --max-days 14
import os, sys, json, argparse, datetime, time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

BASE = "https://talentio.com/api/v1"
JST = datetime.timezone(datetime.timedelta(hours=9))
RESUME_FORM_ID = 717  # 書類選考「合格 or 不合格」評価フォーム

# クラウド実行時の接続リセット対策（2026-08-12）。
# データセンターIPから毎秒4件級のバーストで叩くとTalentio側に接続を切られる事象が発生した。
# GET_DELAY で流量を抑え、_get は一過性の失敗を指数バックオフで自力リトライする
# （以前は1回のリセットでコマンド全体が sys.exit していた）。
GET_DELAY = 0.15   # 各GETの前に入れる待機秒数
GET_RETRIES = 4    # 接続エラー/429/5xx の再試行回数


def _token():
    t = os.environ.get("TALENTIO_TOKEN", "").strip()
    if not t:
        sys.exit("[ERROR] env var TALENTIO_TOKEN is not set")
    return t


def _get(path):
    last = ""
    for attempt in range(GET_RETRIES):
        if attempt:
            time.sleep(2 ** attempt)  # 2s, 4s, 8s
        else:
            time.sleep(GET_DELAY)
        req = Request(BASE + path, headers={"Authorization": "Bearer " + _token()})
        try:
            with urlopen(req, timeout=60) as r:
                headers = {k.lower(): v for k, v in r.getheaders()}
                return headers, json.loads(r.read().decode("utf-8"))
        except HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:200]
            last = "HTTP %s: %s" % (e.code, body)
            if e.code != 429 and e.code < 500:
                sys.exit("[API ERROR] GET %s -> %s" % (path, last))  # 4xxは再試行しても無駄
        except (URLError, OSError) as e:
            last = str(e)  # Connection reset by peer 等
    sys.exit("[API ERROR] GET %s -> %s (after %d attempts)" % (path, last, GET_RETRIES))


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
    """求人番号・チャネル・確定した選考結果から、一意に決まるタグを算出する。"""
    req_name = (c.get("requisition") or {}).get("name") or ""
    ch_name = c.get("channelName") or ""
    ch_type = c.get("channelType") or ""
    want = set()
    # 選考フェーズタグは原則AIが触らないが、「書類選考が fail で確定」は
    # 事実として一意に決まるため付与のみ行う（2026-08-12 伏見さん要望）。
    # MANAGED_TAGS には入れない＝**追加のみ・除去はしない**。人が付けた他のフェーズタグ
    # （通過－書類選考／○月面談済み 等）には一切手を触れない。
    for s in (c.get("stages") or []):
        if s.get("type") == "resume" and s.get("status") == "fail" and s.get("fixedAt"):
            want.add("不合格－書類選考")
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
def _resume_evaluation(cid):
    """詳細APIから resume 評価の【理由】原文・合否・評価日時を取得する。

    合否は items[name=="合否"] の **input**（bool: False=お見送り / True=通過）に入る。
    comment は常に空なので、そちらを見ても判定は取れない（2026-08-12に画面と突合して判明）。
    """
    _, detail = _get("/candidates/%s" % cid)
    reason, verdict, ev_at = "", None, None
    for s in ((detail or {}).get("stages") or []):
        if s.get("type") != "resume":
            continue
        for e in (s.get("evaluations") or []):
            t = _parse_dt(e.get("evaluatedAt"))
            if t and (ev_at is None or t > ev_at):
                ev_at = t
            for it in (e.get("items") or []):
                nm = it.get("name") or ""
                if nm == "合否" and it.get("type") == "bool":
                    verdict = it.get("input")
                elif "理由" in nm:
                    cm = (it.get("comment") or "").strip()
                    if cm:
                        reason = (reason + "\n" + cm).strip()
    return reason, verdict, ev_at


def _scan(bucket, days, pick):
    """stageStatuses={bucket} を新しいページから遡り、pick(候補者, resumeステージ) が
    dict を返したものを集める。ページは registeredAt 昇順なので最終ページ＝最新。"""
    since = datetime.datetime.now(JST) - datetime.timedelta(days=days)
    out, scanned = [], 0
    headers, _first = _get("/candidates?stageStatuses=%s&page=1" % bucket)
    total = int(headers.get("x-total") or 0)
    p = max(1, (total + 99) // 100)
    while p >= 1 and scanned < 30:
        _, rows = _get("/candidates?stageStatuses=%s&page=%d" % (bucket, p))
        rows = [] if not rows else ([rows] if isinstance(rows, dict) else rows)
        scanned += 1
        if not rows:
            p -= 1
            continue
        newest = max([(_parse_dt(c.get("registeredAt")) or datetime.datetime.min.replace(tzinfo=JST)) for c in rows])
        for c in rows:
            if c.get("channelType") != "agent":
                continue
            r = None
            for s in (c.get("stages") or []):
                if s.get("type") == "resume":
                    r = s
            if not r:
                continue
            got = pick(c, r, since)
            if got:
                out.append(got)
        if newest < (since - datetime.timedelta(days=60)):
            break
        p -= 1
    return out


def rejected(days):
    """エージェント経由・書類選考お見送りの候補者を列挙する。

    2種類を返す（finalized で区別）：
      finalized=True  … 選考ステージが fail で確定済み（stageStatuses=fail）
      finalized=False … 評価者は「お見送り」判定を出したが、Talentio画面上部の
                        「お見送り／通過」ボタンによるステージ確定がまだ（evaluated_all）。
                        2026-08-12にこの状態の4名が検知されず「通知が来ない」と報告された。
                        判定は推測ではなく評価データ（合否のinput=False）から読んでいるので
                        下書きを作って差し支えないが、**送信前にTalentioでの確定が必要**。
    ※「未処理かどうか」は呼び出し側がNotion判定ログDBの記録で判断する（時間で近似しない）。
    """
    def base(c, r, reason, when, finalized):
        return {
            "id": c.get("id"),
            "name": ((c.get("lastName") or "") + (c.get("firstName") or "")),
            "position": (c.get("requisition") or {}).get("name"),
            "agentCompany": (c.get("agentCompany") or {}).get("name"),
            "agentPerson": (c.get("agent") or {}).get("name"),
            "fixedAt": when,
            "finalized": finalized,
            "reason": reason,
            "url": c.get("url"),
        }

    def pick_fail(c, r, since):
        if r.get("status") != "fail" or not r.get("fixedAt"):
            return None
        fx = _parse_dt(r.get("fixedAt"))
        if not fx or fx < since:
            return None
        reason, _v, _t = _resume_evaluation(c.get("id"))
        return base(c, r, reason, r.get("fixedAt"), True)

    def pick_unfinalized(c, r, since):
        if r.get("fixedAt") or r.get("status") in ("fail", "pass"):
            return None  # 確定済みは fail バケツ側で拾う
        reason, verdict, ev_at = _resume_evaluation(c.get("id"))
        if verdict is not False:   # True=通過 / None=未入力 は対象外
            return None
        if not ev_at or ev_at < since:
            return None
        return base(c, r, reason, ev_at.isoformat(), False)

    out = _scan("fail", days, pick_fail) + _scan("evaluated_all", days, pick_unfinalized)
    out.sort(key=lambda x: x["fixedAt"], reverse=True)
    print(json.dumps({"count": len(out),
                      "unfinalized": sum(1 for x in out if not x["finalized"]),
                      "candidates": out}, ensure_ascii=False, indent=2))


# ---- Pending finalization (合否未確定の検出) ----
def pending(hours, max_days):
    """評価は入力済みだが「合否」が未確定の候補者を検出する。

    2026-08-12の事象：評価者が【理由】だけ記入し「合否」プルダウンを選ばないと、
    stageはongoingのままでfixedAtも付かない。rejected（fail絞り込み）には現れず、
    お見送りメールの下書きが永久に作られない。伏見さんからは「通知が来ない」と見える。
    ※【理由】欄は合格時にも記入されるため、**文面から合否を推測してはいけない**。
      このアクションは「確定を促すアラート」用であり、下書き生成には使わない。
    """
    cutoff = datetime.datetime.now(JST) - datetime.timedelta(hours=hours)
    # max_days より古い滞留（2024年〜の放置分など）は日々のアラート対象外にする。
    # 毎回通知に出ると新しい案件が埋もれるため。棚卸しは max_days を伸ばして手動実行する。
    floor = datetime.datetime.now(JST) - datetime.timedelta(days=max_days)
    horizon = datetime.datetime.now(JST) - datetime.timedelta(days=30)
    out, stale = [], 0
    headers, _first = _get("/candidates?stageStatuses=evaluated_all&page=1")
    total = int(headers.get("x-total") or 0)
    last_page = max(1, (total + 99) // 100)
    scanned = 0
    p = last_page
    while p >= 1 and scanned < 10:
        _, rows = _get("/candidates?stageStatuses=evaluated_all&page=%d" % p)
        rows = [] if not rows else ([rows] if isinstance(rows, dict) else rows)
        scanned += 1
        if not rows:
            p -= 1
            continue
        newest_reg = max([(_parse_dt(c.get("registeredAt")) or datetime.datetime.min.replace(tzinfo=JST)) for c in rows])
        for c in rows:
            if c.get("channelType") != "agent":
                continue
            r = None
            for s in (c.get("stages") or []):
                if s.get("type") == "resume":
                    r = s
            if not r:
                continue
            if r.get("fixedAt") or r.get("status") in ("fail", "pass"):
                continue  # 既に確定済み＝rejected側の担当
            # 評価入力時刻は一覧APIに無いため詳細APIから取る
            _, detail = _get("/candidates/%s" % c.get("id"))
            ev_at, has_reason = None, False
            for s in ((detail or {}).get("stages") or []):
                if s.get("type") != "resume":
                    continue
                for e in (s.get("evaluations") or []):
                    t = _parse_dt(e.get("evaluatedAt"))
                    if t and (ev_at is None or t > ev_at):
                        ev_at = t
                    for it in (e.get("items") or []):
                        if "理由" in (it.get("name") or "") and (it.get("comment") or "").strip():
                            has_reason = True
            if not ev_at or ev_at > cutoff:
                continue  # 未入力、または入力から間もない（確定作業中の可能性）
            if ev_at < floor:
                stale += 1
                continue
            out.append({
                "id": c.get("id"),
                "name": ((c.get("lastName") or "") + (c.get("firstName") or "")),
                "position": (c.get("requisition") or {}).get("name"),
                "agentCompany": (c.get("agentCompany") or {}).get("name"),
                "evaluatedAt": ev_at.isoformat(),
                "hasReason": has_reason,
                "url": c.get("url"),
            })
        if newest_reg < horizon:
            break
        p -= 1
    out.sort(key=lambda x: x["evaluatedAt"], reverse=True)
    print(json.dumps({"count": len(out), "stale_excluded": stale, "candidates": out},
                     ensure_ascii=False, indent=2))


# ---- Awaiting decision (判定待ちの棚卸し) ----
def awaiting(days):
    """Talentioの「判定待ち」（評価は完了しているが合否が未確定）を、
    評価者の判定内容で仕分けて返す。

    2026-08-20 伏見さん要望：判定待ち一覧に「通過者」と「お見送り者」が混在し、
    Talentioを 1件ずつ開いて閉じて確認する手間が発生している。
    「通過者はこの人」をSlackで知らせるためのアクション。

    分類：
      pass_agent / pass_direct   … 評価者が「通過」判定→**人の対応が必要**
      fail_agent                 … エージェント経由のお見送り→AIが下書きを出すので**待てばよい**
      fail_direct                … 媒体直接応募のお見送り→AI対象外なので**人の対応が必要**
      no_verdict                 … 合否が未入力（評価者待ち）
    """
    floor = datetime.datetime.now(JST) - datetime.timedelta(days=days)
    now = datetime.datetime.now(JST)
    groups = {"pass_agent": [], "pass_direct": [], "fail_agent": [], "fail_direct": [], "no_verdict": []}
    stale = 0
    headers, _first = _get("/candidates?stageStatuses=evaluated_all&page=1")
    total = int(headers.get("x-total") or 0)
    p = max(1, (total + 99) // 100)
    scanned = 0
    while p >= 1 and scanned < 10:
        _, rows = _get("/candidates?stageStatuses=evaluated_all&page=%d" % p)
        rows = [] if not rows else ([rows] if isinstance(rows, dict) else rows)
        scanned += 1
        if not rows:
            p -= 1
            continue
        for c in rows:
            r = None
            for s in (c.get("stages") or []):
                if s.get("type") == "resume":
                    r = s
            if not r:
                continue
            if r.get("fixedAt") or r.get("status") in ("fail", "pass"):
                continue  # 既に確定済み＝判定待ちでない
            reason, verdict, ev_at = _resume_evaluation(c.get("id"))
            if not ev_at:
                continue
            if ev_at < floor:
                stale += 1
                continue
            is_agent = c.get("channelType") == "agent"
            key = ("no_verdict" if verdict is None
                   else ("pass_" if verdict else "fail_") + ("agent" if is_agent else "direct"))
            groups[key].append({
                "id": c.get("id"),
                "name": ((c.get("lastName") or "") + (c.get("firstName") or "")),
                "position": (c.get("requisition") or {}).get("name"),
                "channel": (c.get("agentCompany") or {}).get("name") or c.get("channelName") or c.get("channelType"),
                "evaluatedAt": ev_at.isoformat(),
                "waitingDays": round((now - ev_at).total_seconds() / 86400, 1),
                "reason": reason,
                "url": c.get("url"),
            })
        p -= 1
    for k in groups:
        groups[k].sort(key=lambda x: x["evaluatedAt"])
    need = len(groups["pass_agent"]) + len(groups["pass_direct"]) + len(groups["fail_direct"])
    print(json.dumps({"action_needed_count": need, "stale_excluded": stale,
                      "counts": {k: len(v) for k, v in groups.items()}, "groups": groups},
                     ensure_ascii=False, indent=2))


def passed(days, req_ids):
    """書類選考を通過した候補者と、その後の面接設定の進み具合を返す（面接官起点フロー①用）。

    通過 = resumeステージ status==pass かつ fixedAt が days 日以内。チャネルは問わない。
    req_ids（requisition id の集合）を指定するとそのポジションのみ。空なら全ポジション。

    面接官起点フロー（2026-08-30〜 fondeskパイロット）での使い方：
      interviewStageCreated=False … 面接官がまだ日程調整を発行していない → 面接官へ依頼/リマインド
      Created=True & Scheduled=False … 発行済みだが候補者が未選択 → リンク送付漏れ or 候補者放置の疑い
      Scheduled=True … 確定済み。対応不要（採用カレンダーへはGASが転記）

    stalled と同じく status=ongoing で絞るのでAPIは数リクエストで済む。
    """
    now = datetime.datetime.now(JST)
    since = now - datetime.timedelta(days=days)
    headers, first = _get("/candidates?status=ongoing&page=1")
    total = int(headers.get("x-total") or 0)
    if total > 3000:
        sys.exit("[ERROR] status=ongoing filter appears to be ignored (X-Total=%d)." % total)
    rows = [] if not first else ([first] if isinstance(first, dict) else list(first))
    for p in range(2, max(1, (total + 99) // 100) + 1):
        _, r = _get("/candidates?status=ongoing&page=%d" % p)
        rows += [] if not r else ([r] if isinstance(r, dict) else list(r))

    out, excluded_test = [], 0
    for c in rows:
        if c.get("status") != "ongoing":
            continue
        rq = c.get("requisition") or {}
        if req_ids and rq.get("id") not in req_ids:
            continue
        if _is_test(c):
            excluded_test += 1
            continue
        resume, nxt = None, []
        for s in (c.get("stages") or []):
            if s.get("type") == "resume":
                resume = s
            elif s.get("type") in ("interview", "contact"):
                nxt.append(s)
        if not resume or resume.get("status") != "pass":
            continue
        fx = _parse_dt(resume.get("fixedAt"))
        if not fx or fx < since:
            continue
        out.append({
            "id": c.get("id"),
            "name": ((c.get("lastName") or "") + " " + (c.get("firstName") or "")).strip(),
            "position": rq.get("name"), "requisitionId": rq.get("id"),
            "channel": (c.get("agentCompany") or {}).get("name") or c.get("channelName") or c.get("channelType"),
            "channelType": c.get("channelType"),
            "passedAt": resume.get("fixedAt"),
            "waitingHours": round((now - fx).total_seconds() / 3600, 1),
            "interviewStageCreated": len(nxt) > 0,
            "interviewScheduled": any(s.get("scheduledAt") for s in nxt),
            "url": c.get("url") or ("https://talentio.com/r/ats/candidates/%s" % c.get("id")),
        })
    out.sort(key=lambda x: x["passedAt"] or "", reverse=True)
    print(json.dumps({"scanned_ongoing": len(rows), "excluded_test": excluded_test,
                      "days": days, "count": len(out), "items": out},
                     ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="action", required=True)
    n = sub.add_parser("new"); n.add_argument("--since", required=True)
    d = sub.add_parser("detail"); d.add_argument("--id", required=True)
    s = sub.add_parser("stagesync"); s.add_argument("--hours", type=float, default=18); s.add_argument("--dry-run", action="store_true")
    g = sub.add_parser("tagsync"); g.add_argument("--hours", type=float, default=168); g.add_argument("--dry-run", action="store_true")
    rj = sub.add_parser("rejected"); rj.add_argument("--days", type=float, default=30)
    pd = sub.add_parser("pending"); pd.add_argument("--hours", type=float, default=2); pd.add_argument("--max-days", type=float, default=14)
    aw = sub.add_parser("awaiting"); aw.add_argument("--days", type=float, default=30)
    ps = sub.add_parser("passed"); ps.add_argument("--days", type=float, default=5); ps.add_argument("--requisitions", default="", help="requisition idをカンマ区切り。空なら全ポジション")
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
    elif args.action == "pending":
        pending(args.hours, args.max_days)
    elif args.action == "awaiting":
        awaiting(args.days)
    elif args.action == "passed":
        ids = set(int(x) for x in args.requisitions.split(",") if x.strip())
        passed(args.days, ids)


if __name__ == "__main__":
    main()
