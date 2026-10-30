"""Stream the unchanged OMS command, then notify. Never submit/cancel/retry orders."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

LABELS = {'pre':'盘前检查','buy':'买入提交','sell':'卖出提交','cancel':'撤单请求',
          'eod':'日终核对','upload-spool':'补传','intraday':'盘中状态','status':'状态查询'}
BAD = {'REJECTED','UNKNOWN','FAILED','HOLD_LOCAL','WRONG_DATE','NOT_EXECUTABLE',
       'SERVER_UNAVAILABLE','SKIPPED_NO_ROOM','GUARD_REJECTED','HOLD_STRATEGY','UNRESOLVED_PRIOR_DAY'}
TERMINAL = {'FILLED', 'CANCELLED', 'CANCELED', 'REJECTED', 'EXPIRED'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf8')).hexdigest()


def intraday_decision(previous, code, result, view, view_failed):
    """Pure notification policy. No trading, network calls, clock, or file writes."""
    previous = previous if previous.get('version') == 2 else {}
    state = dict(previous, version=2)
    result = result or {}
    faults = set()
    if code:
        faults.add('TASK_FAILED')
    elif not result:
        faults.add('RESULT_MISSING')
    manual = result.get('manual') or {}
    snapshot = result.get('snapshot') or {}
    if view_failed or snapshot.get('uploaded') is False or manual.get('status') == 'SERVER_UNAVAILABLE':
        faults.add('SERVER_CONNECTION')
    if result.get('status') in BAD:
        faults.add('RESULT_' + result['status'])
    if manual.get('status') in BAD - {'SERVER_UNAVAILABLE'}:
        faults.add('MANUAL_' + manual['status'])
    if isinstance(snapshot.get('reconciliation'), dict) and snapshot['reconciliation'].get('passed') is not True:
        faults.add('RECONCILIATION_FAILED')
    if any(s.get('deferrals') for s in snapshot.get('planned_sessions') or []):
        faults.add('PLAN_DEFERRED')
    cycle = (view or {}).get('cycle') or {}
    if cycle.get('status') == 'HELD':
        faults.add('CYCLE_HELD')
    orders = None
    changed = False
    if view is not None and not view_failed:
        # Exclude timestamps, object order and other transient server metadata.
        fields = ('client_order_id', 'symbol', 'side', 'quantity', 'state', 'filled_qty', 'avg_price')
        orders = sorted([{k: o.get(k) for k in fields} for o in view.get('orders') or []],
                        key=lambda o: (str(o.get('client_order_id')), str(o.get('symbol')), str(o.get('side'))))
        signature = digest({'orders': orders, 'cycle_status': cycle.get('status')})
        if 'business_signature' in previous:
            changed = signature != previous['business_signature']
        else:
            # Existing completed orders establish a silent baseline, not a new fill.
            changed = any(o.get('state') not in TERMINAL | {'PLANNED'} for o in orders)
        state['business_signature'] = signature
        for order in orders:
            if order.get('state') in BAD:
                faults.add('ORDER_' + order['state'])
        if any(s.get('deferrals') for s in view.get('sessions') or []
               if s.get('trade_date', '') > result.get('_notification_date', '') and s.get('status') == 'PLANNED'):
            faults.add('PLAN_DEFERRED')
    manual_results = manual.get('results') or []
    if manual_results:
        fields = ('instruction_id', 'client_order_id', 'status', 'quantity', 'broker_order_id')
        normalized = sorted([{k: x.get(k) for k in fields} for x in manual_results], key=lambda x: digest(x))
        signature = digest(normalized)
        changed |= signature != previous.get('manual_signature')
        state['manual_signature'] = signature
        for item in manual_results:
            if item.get('status') in BAD:
                faults.add('MANUAL_' + item['status'])
    previous_faults = set(previous.get('faults') or [])
    reasons = []
    if faults:
        if faults - previous_faults:
            reasons.append('异常首次出现或新增异常')
        state['faults'] = sorted(previous_faults | faults)
        state['healthy_runs'] = 0
    elif previous_faults:
        # A transient healthy minute must not create fault/recovery message flapping.
        healthy = previous.get('healthy_runs', 0) + 1
        state['healthy_runs'] = healthy
        if healthy >= 2:
            reasons.append('恢复正常，连续两次检查通过')
            state['faults'] = []
            state['healthy_runs'] = 0
    else:
        state['faults'] = []
        state['healthy_runs'] = 0
    if changed:
        reasons.append('委托状态、成交或人工指令发生变化')
    return state, reasons


def status_view():
    from live_client.config import LiveClientConfig
    from live_client.http_client import LiveServerClient
    cfg = LiveClientConfig.from_env()
    return LiveServerClient(cfg.server_base_url, cfg.api_key,
                           execution_domain=cfg.execution_domain).get_oms_status(cfg.account_alias)


def summarize(command, date, code, result, view=None):
    """Only allowlisted business fields leave the machine; never raw logs/errors/env."""
    lines = [f'[Hydra OMS] {date} {LABELS[command]}']
    alert = code != 0 or result is None
    if code:
        lines.append(f'任务执行失败，退出码{code}；请检查旧电脑日志。')
    elif result is None:
        lines.append('未识别到任务结果，请检查旧电脑日志。')
    else:
        state = str(result.get('status', '完成'))
        lines.append('任务结果：' + state)
        alert |= state in BAD
        orders = list(result.get('orders') or [])
        # Shared OMS returns one child result per strategy; keep the legacy flat path too.
        for plan in result.get('plans') or []:
            if not isinstance(plan, dict):
                continue
            plan_state = str(plan.get('status', ''))
            alert |= plan_state in BAD
            lines.append(f"策略{plan.get('instance_id', '')}：{plan_state}")
            orders.extend(plan.get('orders') or [])
        for item in orders:
            if isinstance(item, dict):
                state = str(item.get('status', ''))
                alert |= state in BAD
                lines.append(f"{item.get('client_order_id','')}：{state}，数量{item.get('quantity',0)}")
        if command in ('buy','sell'):
            lines.append('提交结果不代表成交；成交由后续查询确认。')
        if command == 'cancel':
            lines.append(f"撤单请求数量：{len(result.get('requested') or [])}；以券商后续状态为准。")
        if command == 'upload-spool':
            lines.append(f"补传快照数量：{len(result.get('uploaded') or [])}")
            if result.get('failed'):
                alert = True
                lines.append('仍有快照补传失败。')
        snap = result.get('snapshot') if command in ('pre','intraday') else result
        if isinstance(snap, dict):
            if snap.get('uploaded') is False:
                alert = True
                lines.append('快照上传失败。')
            recon = snap.get('reconciliation')
            if isinstance(recon, dict):
                passed = recon.get('passed') is True
                alert |= not passed
                lines.append('对账：' + ('通过' if passed else '未通过'))
            for session in snap.get('planned_sessions') or []:
                lines.append(f"后续计划：{session.get('trade_date')} {session.get('phase')}，订单{len(session.get('orders') or [])}笔")
                if session.get('deferrals'):
                    alert = True
                    lines.append(f"暂缓项目：{len(session['deferrals'])}项")
        manual = result.get('manual') or {}
        alert |= manual.get('status') in BAD
    if view:
        cycle = view.get('cycle') or {}
        if cycle.get('status') == 'HELD':
            alert = True
            lines.append('周期已暂停：HELD')
        for order in view.get('orders') or []:
            state = str(order.get('state',''))
            alert |= state in BAD
            lines.append(f"{order.get('symbol')} {order.get('side')}：{state}，成交{order.get('filled_qty',0)}/{order.get('quantity',0)}，均价{float(order.get('avg_price') or 0):.3f}")
        for session in view.get('sessions') or []:
            if session.get('trade_date','') > date and session.get('status') == 'PLANNED':
                alert |= bool(session.get('deferrals'))
                lines.append(f"后续：{session.get('trade_date')} {session.get('phase')}，暂缓{len(session.get('deferrals') or [])}项")
    if alert:
        lines.insert(1, '【需关注】')
    return '\n'.join(lines), alert


def send(message, hook=None):
    hook = hook if hook is not None else os.environ.get('HYDRA_LIVE_WECHAT_WEBHOOK','')
    if not hook:
        raise RuntimeError('WEBHOOK_NOT_CONFIGURED')
    content = message.encode('utf8')[:3500].decode('utf8', errors='ignore')
    payload = json.dumps({'msgtype':'text','text':{'content':content}}, ensure_ascii=False).encode('utf8')
    request = urllib.request.Request(hook, data=payload, headers={'Content-Type':'application/json; charset=utf-8'})
    with urllib.request.urlopen(request, timeout=5) as response:
        receipt = json.load(response)
    if receipt.get('errcode') != 0:
        raise RuntimeError('WECOM_REJECTED')


def notify(command, date, code, result, root):
    if command == 'status':
        return
    if command == 'intraday' and code == 0 and (result or {}).get('status') == 'SKIPPED_ACCOUNT_BUSY':
        # Expected lock contention is neither failure nor recovery. Do not fetch status.
        return
    view = None
    view_failed = False
    if command in ('intraday','eod'):
        try:
            view = status_view()
            if not isinstance(view, dict):
                raise ValueError('INVALID_STATUS_RESPONSE')
        except Exception:
            view_failed = True
    message, alert = summarize(command, date, code, result, view)
    if view_failed:
        message += '\n通知汇总未能取得服务器状态，请检查日志。'
        alert = True
    directory = root / 'logs' / 'oms-notify'
    directory.mkdir(parents=True, exist_ok=True)
    state_file = directory / f'{command}-{date}.json'
    previous = {}
    if state_file.exists():
        try:
            previous = json.loads(state_file.read_text(encoding='utf8'))
        except Exception:
            pass
    if command == 'intraday':
        state, reasons = intraday_decision(previous, code,
            dict(result, _notification_date=date) if result is not None else None, view, view_failed)
        if not reasons:
            # Persist silent observations separately from the last successful send.
            temporary = state_file.with_suffix('.tmp')
            temporary.write_text(json.dumps(state, ensure_ascii=False), encoding='utf8')
            temporary.replace(state_file)
            return
        message += '\n通知原因：' + '；'.join(reasons)
        signature = digest(state)
    else:
        signature = hashlib.sha256(message.encode('utf8')).hexdigest()
    send(message)
    if command != 'intraday':
        state = {}
    state.update(signature=signature, sent_at=time.time())
    temporary = state_file.with_suffix('.tmp')
    temporary.write_text(json.dumps(state,ensure_ascii=False),encoding='utf8')
    temporary.replace(state_file)
    print('OMS_WECOM_SENT command=' + command, flush=True)


def run_child(arguments):
    result = None
    child = subprocess.Popen([sys.executable, *arguments], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, encoding='utf8', errors='replace')
    for line in child.stdout:
        print(line, end='', flush=True)
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                result = value
        except (ValueError, TypeError):
            pass
    child.stdout.close()
    return child.wait(), result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--command', choices=LABELS, required=True)
    parser.add_argument('--date', required=True)
    parser.add_argument('--root', default=r'C:\hydra-live')
    parser.add_argument('--test-notification', action='store_true')
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.test_notification:
        try:
            send('[Hydra OMS] 企业微信通知功能测试：新版定时任务通知通道已接通。本次仅测试通知，没有执行交易、发单或撤单。')
            print('OMS_WECOM_TEST_ACCEPTED')
            return 0
        except Exception as exc:
            print('OMS_WECOM_TEST_FAILED type=' + type(exc).__name__)
            return 1
    arguments = args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments
    if not arguments:
        parser.error('missing child command')
    try:
        code, result = run_child(arguments)
    except Exception as exc:
        print('OMS_CHILD_LAUNCH_FAILED type=' + type(exc).__name__, flush=True)
        code, result = 1, None
    try:
        notify(args.command, args.date, code, result, Path(args.root))
    except Exception as exc:
        # Never leak webhook URLs in exception strings or change the child's exit code.
        print('OMS_WECOM_FAILED command=' + args.command + ' type=' + type(exc).__name__, flush=True)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
