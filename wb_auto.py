#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wb_auto.py —— 云端 runner 执行的每日自动化（多账号版）：签到 + 派猫猫旅行，结果推飞书。

与原版（FXmonkey/workbuddy_auto_checkin）的差别只在「账号来源」和「汇报方式」：
  - 账号列表来自 GitHub Secret WB_ACCOUNTS（JSON），一次运行依次处理全部账号；
  - 单个账号出错（网络 / 认证 / 业务异常）只影响它自己那一行，不中断整轮；
  - 飞书一张汇总卡：顶部成功概览，每个账号「签到 / 猫猫」两列，底部幂等备注；
  - 没配 WB_ACCOUNTS 时自动退回单账号 WB_ACCESS_TOKEN，行为与原版一致。

沿用原版的设计要点：
  1. 每个账号一次运行把签到与派猫都做掉；
  2. 先领已到家的旅行积分，再判断能否派新一趟；今日名额已用则不派；
  3. 签到与猫猫状态分开汇报；
  4. 猫猫失败不污染签到结论 —— 签到成功即整体成功。

凭证只从环境变量读取，不落盘、不打日志；所有打印都经过 masked() 脱敏。

退出码：0 全部账号签到成功 / 1 有账号签到未达成 / 2 没配置账号或配置格式错误。
"""

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

DEFAULT_ENDPOINT = "https://copilot.tencent.com"
REQUEST_TIMEOUT = 20
MAX_RETRY = 3
CODE_NO_NETWORK = -1
# token 最短长度：只用来拦「复制不完整」，不是格式校验
TOKEN_MIN_LEN = 16


def log(msg):
    print(msg, flush=True)


def masked(text):
    """把返回体里的 token 类字段脱敏后再落日志。"""
    if isinstance(text, dict):
        out = {}
        for k, v in text.items():
            if isinstance(v, (dict, list)):
                out[k] = masked(v)
            elif isinstance(v, str) and len(v) > 24:
                out[k] = v[:6] + "***" + v[-4:]
            else:
                out[k] = v
        return out
    if isinstance(text, list):
        return [masked(x) for x in text]
    if isinstance(text, str) and len(text) > 200:
        return text[:200] + "..."
    return text


def dig(obj, *keys):
    """逐层取值，任一层缺失返回 None。"""
    for k in keys:
        if isinstance(obj, dict) and k in obj:
            obj = obj[k]
        else:
            return None
    return obj


def as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def request(url, headers, method="GET", payload=None, retry=True):
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    ctx = ssl.create_default_context()
    attempt = 0
    while True:
        attempt += 1
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=ctx) as resp:
                raw = resp.read().decode("utf-8", "replace")
                try:
                    return resp.status, json.loads(raw)
                except ValueError:
                    return resp.status, {"raw": raw[:300]}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                body = json.loads(raw)
            except ValueError:
                body = {"raw": raw[:300]}
            # 偶发 3xx（快速轮询时网关会短暂跳转）不是业务结论，退避后重试
            if 300 <= e.code < 400 and attempt < MAX_RETRY:
                log("  · 收到 %d 跳转，%.1fs 后重试（%d/%d）"
                    % (e.code, 2.0 * attempt, attempt, MAX_RETRY))
                time.sleep(2.0 * attempt)
                continue
            return e.code, body
        except Exception as e:
            # 只对网络抖动与 5xx 重试；写操作靠接口幂等，重试有界
            if not retry or attempt >= MAX_RETRY:
                return CODE_NO_NETWORK, {"error": "%s: %s" % (type(e).__name__, e)}
            log("  · 请求异常，%.1fs 后重试（%d/%d）" % (2.0, attempt, MAX_RETRY))
            time.sleep(2.0)


def get(url, headers):
    return request(url, headers, method="GET")


def post(url, headers, payload=None):
    return request(url, headers, method="POST", payload=payload)


def api_ok(code, body):
    return 200 <= code < 300 and (not isinstance(body, dict) or body.get("code", 0) == 0)


def unwrap(body):
    """真实返回统一是 {code,msg,data} 信封，业务字段都在 data 里。"""
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        return body["data"]
    return body if isinstance(body, dict) else {}


# ---------------------------------------------------------------- 签到
def run_checkin(headers, endpoint):
    """查状态 → 未签才领。返回 (ok, 汇报 dict)。"""
    scode, sbody = post(endpoint + "/v2/billing/meter/checkin-activity-status", headers)
    log("  [签到-状态] HTTP %s %s" % (scode, json.dumps(masked(sbody), ensure_ascii=False)))
    if scode == CODE_NO_NETWORK:
        return False, {"result": "NETWORK", "report": "网络不可达，签到未完成"}
    if scode in (401, 403):
        return False, {"result": "AUTH",
                       "report": "认证被拒（HTTP %s），请重新运行 refresh_token.py 注入这个账号的新 token" % scode}
    if not (200 <= scode < 300):
        return False, {"result": "ERROR", "report": "状态接口异常（HTTP %s）" % scode}
    status = unwrap(sbody)
    if dig(status, "active") is False:
        name = dig(status, "activity_name")
        return True, {"result": "INACTIVE", "report": "签到活动未开启" + ("（%s）" % name if name else "")}
    if dig(status, "today_checked_in") in (True, 1):
        return True, {"result": "ALREADY", "report": _fmt_already(status)}
    ccode, cbody = post(endpoint + "/v2/billing/meter/daily-checkin", headers)
    log("  [签到-领取] HTTP %s %s" % (ccode, json.dumps(masked(cbody), ensure_ascii=False)))
    if ccode == CODE_NO_NETWORK:
        return False, {"result": "NETWORK", "report": "领取请求未送达，签到未完成"}
    if ccode in (401, 403):
        return False, {"result": "AUTH", "report": "认证被拒（HTTP %s）" % ccode}
    if _already_checked_in(ccode, cbody):
        _, fresh = post(endpoint + "/v2/billing/meter/checkin-activity-status", headers)
        return True, {"result": "ALREADY",
                      "report": _fmt_already(unwrap(fresh), via="今日已签过（服务端判定已领取）")}
    credit = dig(unwrap(cbody), "credit")
    if credit is not None:
        _, fresh = post(endpoint + "/v2/billing/meter/checkin-activity-status", headers)
        s = unwrap(fresh)
        bonus = "，且为连签奖励日" if dig(s, "is_streak_day") else ""
        streak = dig(s, "streak_days")
        cum = dig(s, "total_credits")
        tail = ""
        if streak is not None:
            tail = "（连续 %s 天%s）" % (streak, "，累计 %s 积分" % cum if cum is not None else "")
        return True, {"result": "CLAIMED", "credit": credit,
                      "report": "成功领取 %s 积分%s%s" % (credit, bonus, tail)}
    if isinstance(cbody, dict) and ("code" in cbody or "msg" in cbody):
        return False, {"result": "ERROR",
                       "report": "领取失败：%s（HTTP %s）" % (cbody.get("msg") or cbody.get("code"), ccode)}
    return False, {"result": "UNKNOWN", "report": "未识别的领取返回"}


def _already_checked_in(code, body):
    if isinstance(body, dict):
        msg = body.get("msg") or ""
        if body.get("code") == 10001 or "已签" in msg:
            return True
    return False


def _fmt_eta(arrive_at, server_now):
    """把秒级时间戳差格式化成人类可读文案；数据缺失时不编造。"""
    if arrive_at is None or server_now is None:
        return ""
    try:
        left = int(arrive_at) - int(server_now)
    except (TypeError, ValueError):
        return ""
    if left <= 0:
        return "，已到家待领"
    if left < 3600:
        return "，约 %d 分钟后到家" % max(1, left // 60)
    return "，约 %.1f 小时后到家" % (left / 3600)


def _fmt_already(status, via="今日已签过"):
    parts = []
    if dig(status, "today_credit") is not None:
        parts.append("今日 +%s" % dig(status, "today_credit"))
    if dig(status, "streak_days") is not None:
        parts.append("连续 %s 天" % dig(status, "streak_days"))
    if dig(status, "total_credits") is not None:
        parts.append("累计 %s 积分" % dig(status, "total_credits"))
    return "%s（%s）" % (via, "，".join(parts)) if parts else via


# ---------------------------------------------------------------- 猫猫
def run_buddy(headers, endpoint):
    """先领已到家的旅行积分，再判断能否派新一趟。猫猫失败不影响签到结论。"""
    base = endpoint + "/v2/activity/growth"
    notes = []
    try:
        scode, sbody = get(base + "/buddy/travel/status", headers)
        log("  [猫猫-状态] HTTP %s %s" % (scode, json.dumps(masked(sbody), ensure_ascii=False)))
        if scode == CODE_NO_NETWORK:
            return {"result": "NETWORK", "report": "网络不可达，猫猫未处理", "notes": notes}
        if scode in (401, 403):
            return {"result": "AUTH", "report": "猫猫认证被拒（HTTP %s）" % scode, "notes": notes}
        if not (200 <= scode < 300):
            return {"result": "ERROR", "report": "旅行状态接口异常（HTTP %s）" % scode, "notes": notes}
        raw_state = dig(unwrap(sbody), "state")
        travel = raw_state
        daily_limit = bool(dig(unwrap(sbody), "daily_limit_reached"))
        travel_body = unwrap(sbody)
        # 1) 已到家 → 先领积分
        if travel == "arrived":
            ccode, cbody = post(base + "/buddy/travel/claim", headers,
                                {"record_id": dig(travel_body, "record_id")})
            log("  [猫猫-领礼物] HTTP %s %s" % (ccode, json.dumps(masked(cbody), ensure_ascii=False)))
            got = dig(unwrap(cbody), "reward_credit")
            if api_ok(ccode, cbody) and got is not None:
                notes.append("领旅行礼物 +%s 积分" % got)
                travel = "idle"
            else:
                return {"result": "ERROR",
                        "report": "领旅行礼物失败：%s（HTTP %s）" % (dig(cbody, "msg") or "未知", ccode),
                        "notes": notes}
        elif travel == "traveling":
            loc = dig(travel_body, "location", "name") or "?"
            notes.append("旅行中（%s%s）" % (loc, _fmt_eta(dig(travel_body, "arrive_at"),
                                                          dig(travel_body, "server_now"))))
        # 2) 判断能否派新一趟
        if travel == "traveling":
            return {"result": "IN_TRIP", "notes": notes,
                    "report": "；".join(notes) if notes else "猫猫在旅行中，无需派出"}
        if travel == "idle":
            if daily_limit:
                return {"result": "LIMIT_REACHED",
                        "report": "；".join(notes + ["今日旅行名额已用完，不再派出"])}
            kcode, kbody = get(base + "/buddy/travel/config", headers)
            log("  [猫猫-目的地] HTTP %s %s" % (kcode, json.dumps(masked(kbody), ensure_ascii=False)))
            locs = dig(unwrap(kbody), "locations") if 200 <= kcode < 300 else None
            if not locs or not isinstance(locs[0], dict):
                return {"result": "ERROR", "report": "未取到可派目的地", "notes": notes}
            loc = locs[0]
            dcode, dbody = post(base + "/buddy/travel/depart", headers,
                                {"location_id": loc.get("id")})
            log("  [猫猫-派出] HTTP %s %s" % (dcode, json.dumps(masked(dbody), ensure_ascii=False)))
            if 200 <= dcode < 300:
                depart = unwrap(dbody)
                name = dig(depart, "location", "name") or loc.get("name") or "?"
                dur = dig(depart, "duration_hours") or dig(depart, "location", "duration_hours")
                notes.append("已派猫猫去%s%s" % (name, "（约 %s 小时后回）" % dur if dur else ""))
                return {"result": "DEPARTED", "notes": notes, "report": "；".join(notes)}
            msg = dig(unwrap(dbody), "msg") or dig(dbody, "msg") or ""
            # 名额已用完属于正常幂等结果，不算失败
            if "limit" in str(msg).lower() or dcode == 429:
                notes.append("今日旅行名额已用完，不再派出")
                return {"result": "LIMIT_REACHED", "notes": notes, "report": "；".join(notes)}
            return {"result": "ERROR",
                    "report": "派出失败：%s（HTTP %s）" % (msg or "未知", dcode), "notes": notes}
        # 兜底：state 不是已知取值。原样汇报、不做写操作，避免误派或误领。
        return {"result": "UNKNOWN_STATE",
                "report": "；".join(notes) if notes else "未识别的旅行状态：%s" % raw_state,
                "notes": notes}
    except Exception as e:
        return {"result": "ERROR", "report": "猫猫模块异常（%s: %s）" % (type(e).__name__, e), "notes": notes}


# ---------------------------------------------------------------- 飞书
def _buddy_icon(result):
    """猫猫模块状态对应的图标。"""
    return {
        "CLAIMED": "🐱", "DEPARTED": "🐱", "IN_TRIP": "🐱",
        "ALREADY": "✅", "INACTIVE": "💤", "LIMIT_REACHED": "🛌",
        "AUTH": "⚠️", "NETWORK": "⚠️", "ERROR": "⚠️",
        "UNKNOWN_STATE": "❔", "UNKNOWN": "❔",
    }.get(result, "🐾")


def build_card(rows, overall_ok):
    """构造飞书卡片 payload；单独拆出来便于预览与测试。

    rows: [(账号名, 签到是否成功, 签到汇报 dict, 猫猫汇报 dict), ...]
    卡片结构：顶部汇总行 → 每个账号一行标题 + 两列（签到 / 猫猫）→ 底部备注。
    """
    color = "green" if overall_ok else "red"
    success_count = sum(1 for _, ok, _, _ in rows if ok)
    bj = time.strftime("%m-%d %H:%M", time.localtime(time.time() + 8 * 3600))
    title = "Buddy 加油站 · 全部成功" if overall_ok else "Buddy 加油站 · 部分失败"

    elements = []
    # 顶部汇总：成功账号数 + 北京时间
    elements.append({"tag": "div", "text": {"tag": "lark_md",
        "content": "**%d/%d** 个账号签到成功 · 北京时间 %s" % (success_count, len(rows), bj)}})

    for idx, (name, ok, checkin, buddy) in enumerate(rows):
        if idx > 0:
            elements.append({"tag": "hr"})
        checkin_icon = "✅" if ok else "❌"
        checkin_report = checkin.get("report") or "无结论"
        buddy_icon = _buddy_icon(buddy.get("result"))
        buddy_report = buddy.get("report") or "无结论"
        # 账号标题行：名字 + 签到总状态
        elements.append({"tag": "div", "text": {"tag": "lark_md",
            "content": "**%s**　%s" % (name, checkin_icon)}})
        # 两列：签到 / 猫猫旅行
        elements.append({"tag": "fields", "fields": [
            {"is_short": True, "text": {"tag": "lark_md",
             "content": "**签到**\n%s %s" % (checkin_icon, checkin_report)}},
            {"is_short": True, "text": {"tag": "lark_md",
             "content": "**猫猫旅行**\n%s %s" % (buddy_icon, buddy_report)}},
        ]})

    elements.append({"tag": "hr"})
    elements.append({"tag": "note", "elements": [
        {"tag": "plain_text", "content": "脚本幂等：已签不重领，已派不重派；结果以本卡片为准"}]})

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": color,
                "title": {"tag": "plain_text", "content": title},
                "subtitle": {"tag": "plain_text",
                             "content": "%d 个账号 · 每日自动化" % len(rows)},
            },
            "elements": elements,
        },
    }


def push_feishu(webhook, rows, overall_ok):
    """推送到飞书自定义机器人。返回 (ok, reason)。

    飞书单次请求体上限 20 KB，账号数量到几十个时如果超限，把文案压短或改成分账号发卡。

    重试策略与业务请求一致：
      - 网络异常 / HTTP 5xx → 自动重试（最多 MAX_RETRY 次，退避），瞬时故障自愈
      - 飞书业务拒绝（19001 地址无效、机器人开启签名校验等）→ 不重试（重试无用）
    兜底：脚本幂等，当天 cron 有多个时段时，下一次运行会自动再推一遍当天结果。
    """
    data = json.dumps(build_card(rows, overall_ok)).encode("utf-8")
    attempt = 0
    while True:
        attempt += 1
        try:
            req = urllib.request.Request(webhook, data=data, method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15, context=ssl.create_default_context()) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
                log("  [飞书] HTTP %s %s" % (resp.status, json.dumps(body, ensure_ascii=False)))
                if body.get("code") == 0:
                    return True, ""
                return False, ("飞书返回码 %s（19001 多为地址不对；机器人若开了签名校验也会失败。"
                               "这是配置问题，重试无用：检查 Secret FEISHU_WEBHOOK，或建机器人时"
                               "不要勾选签名校验）" % body.get("code", "未知"))
        except urllib.error.HTTPError as e:
            if e.code >= 500 and attempt < MAX_RETRY:
                log("  [飞书] HTTP %s，退避后重试（%d/%d）" % (e.code, attempt, MAX_RETRY))
                time.sleep(2.0 * attempt)
                continue
            if e.code >= 500:
                return False, "重试 %d 次后仍 HTTP %s" % (MAX_RETRY, e.code)
            return False, "HTTP %s" % e.code
        except Exception as e:
            if attempt < MAX_RETRY:
                log("  [飞书] 请求异常，退避后重试（%d/%d）：%s: %s"
                    % (attempt, MAX_RETRY, type(e).__name__, e))
                time.sleep(2.0 * attempt)
                continue
            return False, "重试 %d 次后仍异常：%s: %s" % (MAX_RETRY, type(e).__name__, e)


# ---------------------------------------------------------------- 多账号
def _token_problem(value):
    """返回无效原因；token 可用时返回 None。只做基础检查，不是格式校验。"""
    if value is None:
        return "未填 token"
    if not isinstance(value, str):
        return "token 不是字符串"
    token = value.strip()
    if not token:
        return "token 为空"
    if len(token) < TOKEN_MIN_LEN:
        return "token 太短（%d 字符），多半是复制不完整" % len(token)
    if any(ch.isspace() for ch in token):
        return "token 中间夹了空格或换行，请清理后重新粘贴"
    return None


def load_accounts():
    """读取账号列表。优先 WB_ACCOUNTS（JSON），否则退回单账号 WB_ACCESS_TOKEN。

    支持三种写法（value 可以是 token 字符串，也可以是 {"token": ...} 对象）：
      {"主号": "token", "小号": "token"}
      {"主号": {"token": "token"}, "小号": {"token": "token"}}
      [{"name": "主号", "token": "token"}, {"name": "小号", "token": "token"}]

    条目 token 为空 / 太短 / 含空白时跳过该账号并告警，不打断整轮；
    只有「一个可用账号都没有」时才判定配置错误。
    账号顺序 = 你写 JSON 的顺序（Python 的 dict 保留插入顺序），卡片也按这个顺序排。
    """
    raw = (os.environ.get("WB_ACCOUNTS") or "").strip()
    if not raw:
        token = (os.environ.get("WB_ACCESS_TOKEN") or "").strip()
        if not token:
            return []
        problem = _token_problem(token)
        if problem:
            raise ValueError("WB_ACCESS_TOKEN %s" % problem)
        return [{"name": "默认", "token": token}]

    try:
        data = json.loads(raw)
    except ValueError as e:
        raise ValueError("WB_ACCOUNTS 不是合法 JSON（引号必须是英文直引号，逗号冒号也不能用中文）：%s" % e)

    pairs = []
    if isinstance(data, dict):
        for name, value in data.items():
            pairs.append((str(name), value))
    elif isinstance(data, list):
        for i, item in enumerate(data):
            if not isinstance(item, dict):
                raise ValueError("WB_ACCOUNTS 数组的第 %d 项不是对象" % (i + 1))
            pairs.append((str(item.get("name") or "账号%d" % (i + 1)), item))
    else:
        raise ValueError("WB_ACCOUNTS 最外层必须是对象（{名字: token}）或数组，当前是 %s"
                         % type(data).__name__)

    if not pairs:
        raise ValueError("WB_ACCOUNTS 里一个账号都没有")

    accounts = []
    for name, value in pairs:
        token = value.get("token") if isinstance(value, dict) else value
        label = name.strip() or "未命名"
        problem = _token_problem(token)
        if problem:
            log("  · 跳过账号「%s」：%s" % (label, problem))
            continue
        accounts.append({"name": label, "token": token.strip()})

    if not accounts:
        raise ValueError("没有可用账号：WB_ACCOUNTS 里所有条目都缺少或无效 token，"
                         "请只填已从 refresh_token.exe 导出 token 的账号")

    seen = {}
    for acc in accounts:
        if acc["token"] in seen:
            log("  · 警告：账号「%s」与「%s」的 token 完全相同，会被重复处理一遍"
                % (acc["name"], seen[acc["token"]]))
        else:
            seen[acc["token"]] = acc["name"]
    return accounts


def headers_for(token):
    return {
        "Accept": "application/json",
        "Authorization": "Bearer %s" % token,
        "Content-Type": "application/json",
        "User-Agent": "WorkBuddy",
    }


def _cell(text):
    """表格单元格：去掉会破坏 Markdown 表格的竖线与换行。"""
    return str(text).replace("|", "｜").replace("\n", " ").strip()


# ---------------------------------------------------------------- 主流程
def main():
    try:
        accounts = load_accounts()
    except ValueError as e:
        log("配置错误：%s" % e)
        return 2
    if not accounts:
        log("未配置任何账号：请设置 Secret WB_ACCOUNTS（JSON），或单账号 Secret WB_ACCESS_TOKEN")
        return 2

    webhook = os.environ.get("FEISHU_WEBHOOK")
    endpoint = (os.environ.get("WB_ENDPOINT") or DEFAULT_ENDPOINT).rstrip("/")
    if not webhook:
        log("缺少环境变量 FEISHU_WEBHOOK（本次不推送，只打印日志）")
    log("本轮共 %d 个账号：%s" % (len(accounts), "、".join(a["name"] for a in accounts)))

    rows = []
    for index, acc in enumerate(accounts, 1):
        log("=== %d/%d 账号：%s ===" % (index, len(accounts), acc["name"]))
        headers = headers_for(acc["token"])

        # 每个账号的签到单独 try：异常只影响它自己这一行
        try:
            ok, checkin = run_checkin(headers, endpoint)
            if not isinstance(checkin, dict):
                ok, checkin = False, {"result": "ERROR", "report": "签到返回结构异常"}
        except Exception as e:
            ok, checkin = False, {"result": "ERROR",
                                  "report": "签到模块异常（%s: %s）" % (type(e).__name__, e)}
        log("  签到结论：%s" % (checkin.get("report") or "无结论"))

        # 猫猫独立 try：任何异常都不影响这个账号的签到结论
        try:
            buddy = run_buddy(headers, endpoint)
            if not isinstance(buddy, dict):
                buddy = {"result": "ERROR", "report": "猫猫返回结构异常"}
        except Exception as e:
            buddy = {"result": "ERROR",
                     "report": "猫猫模块异常（%s: %s）" % (type(e).__name__, e)}
        log("  猫猫结论：%s" % (buddy.get("report") or "无结论"))

        rows.append((acc["name"], bool(ok), checkin, buddy))

    # 整体成败只看签到，且要求每个账号都达成
    overall_ok = bool(rows) and all(row[1] for row in rows)

    # 推送状态与业务结论分开呈现：失败原因可诊断，但不污染签到结论
    push_ok, push_reason = None, "未配置（缺少 FEISHU_WEBHOOK，本次不推送）"
    if webhook:
        push_ok, push_reason = push_feishu(webhook, rows, overall_ok)

    result = {
        "accounts": [
            {
                "name": name,
                "ok": ok,
                "checkin": {k: v for k, v in checkin.items() if k != "notes"},
                "buddy": {k: v for k, v in buddy.items() if k != "notes"},
            }
            for name, ok, checkin, buddy in rows
        ],
        "push": {"ok": push_ok, "reason": push_reason},
        "overall_ok": overall_ok,
    }
    print("RESULT_JSON " + json.dumps(result, ensure_ascii=False))

    # 结果文件只含结论文案，不含 token，供 Actions 写入 step summary
    result_file = os.environ.get("WB_RESULT_FILE")
    if result_file:
        try:
            push_line = "✅ 已推送到飞书" if push_ok else "❌ 推送失败：%s" % push_reason
            with open(result_file, "w", encoding="utf-8") as f:
                f.write("| 账号 | 签到 | 猫猫旅行 |\n| --- | --- | --- |\n")
                for name, ok, checkin, buddy in rows:
                    f.write("| %s | %s %s | %s |\n"
                            % (_cell(name), "✅" if ok else "❌",
                               _cell(checkin.get("report") or "无结论"),
                               _cell(buddy.get("report") or "无结论")))
                f.write("\n- 飞书推送：%s\n" % push_line)
                f.write("- 整体：%s\n"
                        % ("成功（全部账号签到已达成）" if overall_ok else "失败（有账号签到未达成）"))
        except OSError as e:
            log("结果文件写入失败：%s" % e)

    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
