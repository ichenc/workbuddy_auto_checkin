import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
import re
DEFAULT_ENDPOINT = 'https://copilot.tencent.com'


def _clean_card_text(text):
    if not isinstance(text, str):
        text = str(text)
    text = re.sub('<[^>]+>', '', text)
    if len(text) > 800:
        text = text[:800] + '…'
    return text
REQUEST_TIMEOUT = 20
MAX_RETRY = 3
CODE_NO_NETWORK = -1
TOKEN_MIN_LEN = 16

def log(msg):
    print(msg, flush=True)

def masked(text):
    if isinstance(text, dict):
        out = {}
        for k, v in text.items():
            if isinstance(v, (dict, list)):
                out[k] = masked(v)
            elif isinstance(v, str) and len(v) > 24:
                out[k] = v[:6] + '***' + v[-4:]
            else:
                out[k] = v
        return out
    if isinstance(text, list):
        return [masked(x) for x in text]
    if isinstance(text, str) and len(text) > 200:
        return text[:200] + '...'
    return text

def dig(obj, *keys):
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

def request(url, headers, method='GET', payload=None, retry=True):
    body = None
    if payload is not None:
        body = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    ctx = ssl.create_default_context()
    attempt = 0
    while True:
        attempt += 1
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=ctx) as resp:
                raw = resp.read().decode('utf-8', 'replace')
                try:
                    return (resp.status, json.loads(raw))
                except ValueError:
                    return (resp.status, {'raw': raw[:300]})
        except urllib.error.HTTPError as e:
            raw = e.read().decode('utf-8', 'replace')
            try:
                body = json.loads(raw)
            except ValueError:
                body = {'raw': raw[:300]}
            if 300 <= e.code < 400 and attempt < MAX_RETRY:
                log('  · 收到 %d 跳转，%.1fs 后重试（%d/%d）' % (e.code, 2.0 * attempt, attempt, MAX_RETRY))
                time.sleep(2.0 * attempt)
                continue
            return (e.code, body)
        except Exception as e:
            if not retry or attempt >= MAX_RETRY:
                return (CODE_NO_NETWORK, {'error': '%s: %s' % (type(e).__name__, e)})
            log('  · 请求异常，%.1fs 后重试（%d/%d）' % (2.0, attempt, MAX_RETRY))
            time.sleep(2.0)

def get(url, headers):
    return request(url, headers, method='GET')

def post(url, headers, payload=None):
    return request(url, headers, method='POST', payload=payload)

def api_ok(code, body):
    return 200 <= code < 300 and (not isinstance(body, dict) or body.get('code', 0) == 0)

def unwrap(body):
    if isinstance(body, dict) and isinstance(body.get('data'), dict):
        return body['data']
    return body if isinstance(body, dict) else {}

def run_checkin(headers, endpoint):
    scode, sbody = post(endpoint + '/v2/billing/meter/checkin-activity-status', headers)
    log('  [签到-状态] HTTP %s %s' % (scode, json.dumps(masked(sbody), ensure_ascii=False)))
    if scode == CODE_NO_NETWORK:
        return (False, {'result': 'NETWORK', 'report': '网络不可达，签到未完成'})
    if scode in (401, 403):
        return (False, {'result': 'AUTH', 'report': '认证被拒（HTTP %s），请重新运行 refresh_token.py 注入这个账号的新 token' % scode})
    if not 200 <= scode < 300:
        return (False, {'result': 'ERROR', 'report': '状态接口异常（HTTP %s）' % scode})
    status = unwrap(sbody)
    if dig(status, 'active') is False:
        name = dig(status, 'activity_name')
        return (True, {'result': 'INACTIVE', 'report': '签到活动未开启' + ('（%s）' % name if name else '')})
    if dig(status, 'today_checked_in') in (True, 1):
        return (True, {'result': 'ALREADY', 'report': _fmt_already(status)})
    ccode, cbody = post(endpoint + '/v2/billing/meter/daily-checkin', headers)
    log('  [签到-领取] HTTP %s %s' % (ccode, json.dumps(masked(cbody), ensure_ascii=False)))
    if ccode == CODE_NO_NETWORK:
        return (False, {'result': 'NETWORK', 'report': '领取请求未送达，签到未完成'})
    if ccode in (401, 403):
        return (False, {'result': 'AUTH', 'report': '认证被拒（HTTP %s）' % ccode})
    if _already_checked_in(ccode, cbody):
        _, fresh = post(endpoint + '/v2/billing/meter/checkin-activity-status', headers)
        return (True, {'result': 'ALREADY', 'report': _fmt_already(unwrap(fresh), via='今日已签过（服务端判定已领取）')})
    credit = dig(unwrap(cbody), 'credit')
    if credit is not None:
        _, fresh = post(endpoint + '/v2/billing/meter/checkin-activity-status', headers)
        s = unwrap(fresh)
        bonus = '，且为连签奖励日' if dig(s, 'is_streak_day') else ''
        streak = dig(s, 'streak_days')
        cum = dig(s, 'total_credits')
        tail = ''
        if streak is not None:
            tail = '（连续 %s 天%s）' % (streak, '，累计 %s 积分' % cum if cum is not None else '')
        return (True, {'result': 'CLAIMED', 'credit': credit, 'report': '成功领取 %s 积分%s%s' % (credit, bonus, tail)})
    if isinstance(cbody, dict) and ('code' in cbody or 'msg' in cbody):
        return (False, {'result': 'ERROR', 'report': '领取失败：%s（HTTP %s）' % (cbody.get('msg') or cbody.get('code'), ccode)})
    return (False, {'result': 'UNKNOWN', 'report': '未识别的领取返回'})

def _already_checked_in(code, body):
    if isinstance(body, dict):
        msg = body.get('msg') or ''
        if body.get('code') == 10001 or '已签' in msg:
            return True
    return False

def _fmt_eta(arrive_at, server_now):
    if arrive_at is None or server_now is None:
        return ''
    try:
        left = int(arrive_at) - int(server_now)
    except (TypeError, ValueError):
        return ''
    if left <= 0:
        return '，已到家待领'
    if left < 3600:
        return '，约 %d 分钟后到家' % max(1, left // 60)
    return '，约 %.1f 小时后到家' % (left / 3600)

def _fmt_already(status, via='今日已签过'):
    parts = []
    if dig(status, 'today_credit') is not None:
        parts.append('今日 +%s' % dig(status, 'today_credit'))
    if dig(status, 'streak_days') is not None:
        parts.append('连续 %s 天' % dig(status, 'streak_days'))
    if dig(status, 'total_credits') is not None:
        parts.append('累计 %s 积分' % dig(status, 'total_credits'))
    return '%s（%s）' % (via, '，'.join(parts)) if parts else via

def run_buddy(headers, endpoint):
    base = endpoint + '/v2/activity/growth'
    notes = []
    try:
        scode, sbody = get(base + '/buddy/travel/status', headers)
        log('  [猫猫-状态] HTTP %s %s' % (scode, json.dumps(masked(sbody), ensure_ascii=False)))
        if scode == CODE_NO_NETWORK:
            return {'result': 'NETWORK', 'report': '网络不可达，猫猫未处理', 'notes': notes}
        if scode in (401, 403):
            return {'result': 'AUTH', 'report': '猫猫认证被拒（HTTP %s）' % scode, 'notes': notes}
        if not 200 <= scode < 300:
            return {'result': 'ERROR', 'report': '旅行状态接口异常（HTTP %s）' % scode, 'notes': notes}
        raw_state = dig(unwrap(sbody), 'state')
        travel = raw_state
        daily_limit = bool(dig(unwrap(sbody), 'daily_limit_reached'))
        travel_body = unwrap(sbody)
        if travel == 'arrived':
            ccode, cbody = post(base + '/buddy/travel/claim', headers, {'record_id': dig(travel_body, 'record_id')})
            log('  [猫猫-领礼物] HTTP %s %s' % (ccode, json.dumps(masked(cbody), ensure_ascii=False)))
            got = dig(unwrap(cbody), 'reward_credit')
            if api_ok(ccode, cbody) and got is not None:
                notes.append('领旅行礼物 +%s 积分' % got)
                travel = 'idle'
            else:
                return {'result': 'ERROR', 'report': '领旅行礼物失败：%s（HTTP %s）' % (dig(cbody, 'msg') or '未知', ccode), 'notes': notes}
        elif travel == 'traveling':
            loc = dig(travel_body, 'location', 'name') or '?'
            notes.append('旅行中（%s%s）' % (loc, _fmt_eta(dig(travel_body, 'arrive_at'), dig(travel_body, 'server_now'))))
        if travel == 'traveling':
            return {'result': 'IN_TRIP', 'notes': notes, 'report': '；'.join(notes) if notes else '猫猫在旅行中，无需派出'}
        if travel == 'idle':
            if daily_limit:
                return {'result': 'LIMIT_REACHED', 'report': '；'.join(notes + ['今日旅行名额已用完，不再派出'])}
            kcode, kbody = get(base + '/buddy/travel/config', headers)
            log('  [猫猫-目的地] HTTP %s %s' % (kcode, json.dumps(masked(kbody), ensure_ascii=False)))
            locs = dig(unwrap(kbody), 'locations') if 200 <= kcode < 300 else None
            if not locs or not isinstance(locs[0], dict):
                return {'result': 'ERROR', 'report': '未取到可派目的地', 'notes': notes}
            loc = locs[0]
            dcode, dbody = post(base + '/buddy/travel/depart', headers, {'location_id': loc.get('id')})
            log('  [猫猫-派出] HTTP %s %s' % (dcode, json.dumps(masked(dbody), ensure_ascii=False)))
            if 200 <= dcode < 300:
                depart = unwrap(dbody)
                name = dig(depart, 'location', 'name') or loc.get('name') or '?'
                dur = dig(depart, 'duration_hours') or dig(depart, 'location', 'duration_hours')
                notes.append('已派猫猫去%s%s' % (name, '（约 %s 小时后回）' % dur if dur else ''))
                return {'result': 'DEPARTED', 'notes': notes, 'report': '；'.join(notes)}
            msg = dig(unwrap(dbody), 'msg') or dig(dbody, 'msg') or ''
            if 'limit' in str(msg).lower() or dcode == 429:
                notes.append('今日旅行名额已用完，不再派出')
                return {'result': 'LIMIT_REACHED', 'notes': notes, 'report': '；'.join(notes)}
            return {'result': 'ERROR', 'report': '派出失败：%s（HTTP %s）' % (msg or '未知', dcode), 'notes': notes}
        return {'result': 'UNKNOWN_STATE', 'report': '；'.join(notes) if notes else '未识别的旅行状态：%s' % raw_state, 'notes': notes}
    except Exception as e:
        return {'result': 'ERROR', 'report': '猫猫模块异常（%s: %s）' % (type(e).__name__, e), 'notes': notes}

def _buddy_icon(result):
    return {'CLAIMED': '🐱', 'DEPARTED': '🐱', 'IN_TRIP': '🐱', 'ALREADY': '✅', 'INACTIVE': '💤', 'LIMIT_REACHED': '🛌', 'AUTH': '⚠️', 'NETWORK': '⚠️', 'ERROR': '⚠️', 'UNKNOWN_STATE': '❔', 'UNKNOWN': '❔'}.get(result, '🐾')

def build_card(rows, overall_ok):
    success_count = sum((1 for _, ok, _, _ in rows if ok))
    bj = time.strftime('%m-%d %H:%M', time.localtime(time.time() + 8 * 3600))
    if overall_ok:
        template, emoji, title = 'green', '✅', 'Buddy 加油站 · 全部成功'
    elif success_count == 0:
        template, emoji, title = 'red', '❌', 'Buddy 加油站 · 全部失败'
    else:
        template, emoji, title = 'orange', '⚠️', 'Buddy 加油站 · 部分成功'
    elements = []
    elements.append({'tag': 'div', 'text': {'tag': 'lark_md',
        'content': '%s **%d / %d** 个账号签到成功 · 北京时间 %s' % (emoji, success_count, len(rows), bj)}})
    elements.append({'tag': 'hr'})
    for idx, (name, ok, checkin, buddy) in enumerate(rows):
        if idx > 0:
            elements.append({'tag': 'hr'})
        status_icon = '✅' if ok else '❌'
        checkin_report = _clean_card_text(checkin.get('report') or '无结论')
        buddy_icon = _buddy_icon(buddy.get('result'))
        buddy_report = _clean_card_text(buddy.get('report') or '无结论')
        elements.append({'tag': 'div', 'text': {'tag': 'lark_md',
            'content': '**%s** %s' % (_clean_card_text(name), status_icon)}})
        elements.append({'tag': 'div', 'fields': [
            {'is_short': True, 'text': {'tag': 'lark_md',
             'content': '**📅 签到**' + chr(10) + '%s %s' % (status_icon, checkin_report)}},
            {'is_short': True, 'text': {'tag': 'lark_md',
             'content': '**🐱 猫猫旅行**' + chr(10) + '%s %s' % (buddy_icon, buddy_report)}},
        ]})
    elements.append({'tag': 'hr'})
    elements.append({'tag': 'note', 'elements': [
        {'tag': 'plain_text', 'content': '🔁 脚本幂等：已签不重领，已派不重派 · 结果以本卡片为准'}]})
    return {'msg_type': 'interactive', 'card': {
        'config': {'wide_screen_mode': True},
        'header': {
            'template': template,
            'title': {'tag': 'plain_text', 'content': '%s %s' % (emoji, title)},
            'subtitle': {'tag': 'plain_text', 'content': '%d 个账号 · 每日自动化' % len(rows)},
        },
        'elements': elements,
    }}

def push_feishu(webhook, rows, overall_ok):
    data = json.dumps(build_card(rows, overall_ok)).encode('utf-8')
    attempt = 0
    while True:
        attempt += 1
        try:
            req = urllib.request.Request(webhook, data=data, method='POST', headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=15, context=ssl.create_default_context()) as resp:
                body = json.loads(resp.read().decode('utf-8', 'replace'))
                log('  [飞书] HTTP %s %s' % (resp.status, json.dumps(body, ensure_ascii=False)))
                if body.get('code') == 0:
                    return (True, '')
                code = body.get('code', '未知')
                if code == 11246:
                    return (False, '飞书返回码 11246：卡片内容构建失败（create message content fail），与 Secret / 地址无关。检查 build_card 卡片结构：elements 是否含非法 tag、fields 是否包在 div 内、文本是否混入 HTML 标签或超长。')
                if code == 19001:
                    return (False, '飞书返回码 19001：webhook 地址无效（机器人被重建或地址复制错），检查 Secret FEISHU_WEBHOOK。')
                if code in (19021, 19022, 19024):
                    return (False, '飞书返回码 %s：签名校验 / IP 白名单 / 关键词不匹配，检查机器人安全设置。' % code)
                if code == 9499:
                    return (False, '飞书返回码 9499：请求体格式错误或超 20KB，缩短文案或拆分账号。')
                return (False, '飞书返回码 %s：推送被拒且重试无用。19001 查地址、11246 查卡片结构、签名相关见机器人安全设置。' % code)
        except urllib.error.HTTPError as e:
            if e.code >= 500 and attempt < MAX_RETRY:
                log('  [飞书] HTTP %s，退避后重试（%d/%d）' % (e.code, attempt, MAX_RETRY))
                time.sleep(2.0 * attempt)
                continue
            if e.code >= 500:
                return (False, '重试 %d 次后仍 HTTP %s' % (MAX_RETRY, e.code))
            return (False, 'HTTP %s' % e.code)
        except Exception as e:
            if attempt < MAX_RETRY:
                log('  [飞书] 请求异常，退避后重试（%d/%d）：%s: %s' % (attempt, MAX_RETRY, type(e).__name__, e))
                time.sleep(2.0 * attempt)
                continue
            return (False, '重试 %d 次后仍异常：%s: %s' % (MAX_RETRY, type(e).__name__, e))

def _token_problem(value):
    if value is None:
        return '未填 token'
    if not isinstance(value, str):
        return 'token 不是字符串'
    token = value.strip()
    if not token:
        return 'token 为空'
    if len(token) < TOKEN_MIN_LEN:
        return 'token 太短（%d 字符），多半是复制不完整' % len(token)
    if any((ch.isspace() for ch in token)):
        return 'token 中间夹了空格或换行，请清理后重新粘贴'
    return None

def load_accounts():
    raw = (os.environ.get('WB_ACCOUNTS') or '').strip()
    if not raw:
        token = (os.environ.get('WB_ACCESS_TOKEN') or '').strip()
        if not token:
            return []
        problem = _token_problem(token)
        if problem:
            raise ValueError('WB_ACCESS_TOKEN %s' % problem)
        return [{'name': '默认', 'token': token}]
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise ValueError('WB_ACCOUNTS 不是合法 JSON（引号必须是英文直引号，逗号冒号也不能用中文）：%s' % e)
    pairs = []
    if isinstance(data, dict):
        for name, value in data.items():
            pairs.append((str(name), value))
    elif isinstance(data, list):
        for i, item in enumerate(data):
            if not isinstance(item, dict):
                raise ValueError('WB_ACCOUNTS 数组的第 %d 项不是对象' % (i + 1))
            pairs.append((str(item.get('name') or '账号%d' % (i + 1)), item))
    else:
        raise ValueError('WB_ACCOUNTS 最外层必须是对象（{名字: token}）或数组，当前是 %s' % type(data).__name__)
    if not pairs:
        raise ValueError('WB_ACCOUNTS 里一个账号都没有')
    accounts = []
    for name, value in pairs:
        token = value.get('token') if isinstance(value, dict) else value
        label = name.strip() or '未命名'
        problem = _token_problem(token)
        if problem:
            log('  · 跳过账号「%s」：%s' % (label, problem))
            continue
        accounts.append({'name': label, 'token': token.strip()})
    if not accounts:
        raise ValueError('没有可用账号：WB_ACCOUNTS 里所有条目都缺少或无效 token，请只填已从 refresh_token.exe 导出 token 的账号')
    seen = {}
    for acc in accounts:
        if acc['token'] in seen:
            log('  · 警告：账号「%s」与「%s」的 token 完全相同，会被重复处理一遍' % (acc['name'], seen[acc['token']]))
        else:
            seen[acc['token']] = acc['name']
    return accounts

def headers_for(token):
    return {'Accept': 'application/json', 'Authorization': 'Bearer %s' % token, 'Content-Type': 'application/json', 'User-Agent': 'WorkBuddy'}

def _cell(text):
    return str(text).replace('|', '｜').replace('\n', ' ').strip()

def main():
    try:
        accounts = load_accounts()
    except ValueError as e:
        log('配置错误：%s' % e)
        return 2
    if not accounts:
        log('未配置任何账号：请设置 Secret WB_ACCOUNTS（JSON），或单账号 Secret WB_ACCESS_TOKEN')
        return 2
    webhook = os.environ.get('FEISHU_WEBHOOK')
    endpoint = (os.environ.get('WB_ENDPOINT') or DEFAULT_ENDPOINT).rstrip('/')
    if not webhook:
        log('缺少环境变量 FEISHU_WEBHOOK（本次不推送，只打印日志）')
    log('本轮共 %d 个账号：%s' % (len(accounts), '、'.join((a['name'] for a in accounts))))
    rows = []
    for index, acc in enumerate(accounts, 1):
        log('=== %d/%d 账号：%s ===' % (index, len(accounts), acc['name']))
        headers = headers_for(acc['token'])
        try:
            ok, checkin = run_checkin(headers, endpoint)
            if not isinstance(checkin, dict):
                ok, checkin = (False, {'result': 'ERROR', 'report': '签到返回结构异常'})
        except Exception as e:
            ok, checkin = (False, {'result': 'ERROR', 'report': '签到模块异常（%s: %s）' % (type(e).__name__, e)})
        log('  签到结论：%s' % (checkin.get('report') or '无结论'))
        try:
            buddy = run_buddy(headers, endpoint)
            if not isinstance(buddy, dict):
                buddy = {'result': 'ERROR', 'report': '猫猫返回结构异常'}
        except Exception as e:
            buddy = {'result': 'ERROR', 'report': '猫猫模块异常（%s: %s）' % (type(e).__name__, e)}
        log('  猫猫结论：%s' % (buddy.get('report') or '无结论'))
        rows.append((acc['name'], bool(ok), checkin, buddy))
    overall_ok = bool(rows) and all((row[1] for row in rows))
    push_ok, push_reason = (None, '未配置（缺少 FEISHU_WEBHOOK，本次不推送）')
    if webhook:
        push_ok, push_reason = push_feishu(webhook, rows, overall_ok)
    result = {'accounts': [{'name': name, 'ok': ok, 'checkin': {k: v for k, v in checkin.items() if k != 'notes'}, 'buddy': {k: v for k, v in buddy.items() if k != 'notes'}} for name, ok, checkin, buddy in rows], 'push': {'ok': push_ok, 'reason': push_reason}, 'overall_ok': overall_ok}
    print('RESULT_JSON ' + json.dumps(result, ensure_ascii=False))
    result_file = os.environ.get('WB_RESULT_FILE')
    if result_file:
        try:
            push_line = '✅ 已推送到飞书' if push_ok else '❌ 推送失败：%s' % push_reason
            with open(result_file, 'w', encoding='utf-8') as f:
                f.write('| 账号 | 签到 | 猫猫旅行 |\n| --- | --- | --- |\n')
                for name, ok, checkin, buddy in rows:
                    f.write('| %s | %s %s | %s |\n' % (_cell(name), '✅' if ok else '❌', _cell(checkin.get('report') or '无结论'), _cell(buddy.get('report') or '无结论')))
                f.write('\n- 飞书推送：%s\n' % push_line)
                f.write('- 整体：%s\n' % ('成功（全部账号签到已达成）' if overall_ok else '失败（有账号签到未达成）'))
        except OSError as e:
            log('结果文件写入失败：%s' % e)
    return 0 if overall_ok else 1
if __name__ == '__main__':
    sys.exit(main())
