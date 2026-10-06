"""重心大幅移動事件：以玩家自己的慣用姿勢（整段中位數，含扶桿後靠）為基準，
挑出偏離最明顯的 10–20 段，附上當時的踩點、原因推測，並統計哪種配置最容易讓重心跑掉。

原因代碼（介面依語言翻譯）：
  crossover   交叉步（左腳踩 → 或右腳踩 ←）
  jump        跳（兩腳幾乎同時落地）
  same_foot   同一隻腳連續踩不同板
  wide        大跨距（← 與 → 或 ↑ 與 ↓ 連續）
  bar_release 手離開扶桿
  fast        比平常密集

DP：箭頭為「1P←」…「2P→」。交叉步改以左右腳落點的 x 判斷（左腳踩到右腳右邊的板）；
大跨距＝同一台的 ←→／↑↓，或跨台連續踩；跨台移動另外由 trunk.pad_regions 列為「台移動」，不算重心異常。
"""
from __future__ import annotations

import numpy as np
from scipy.ndimage import uniform_filter1d

ARROWS4 = ('←', '↓', '↑', '→')
OPPOSITE = {('←', '→'), ('→', '←'), ('↑', '↓'), ('↓', '↑')}


def _r(x, d=3):
    return None if x is None or not np.isfinite(x) else round(float(x), d)


def _dp_x(p):
    """DP 箭頭符號 → 踏板 x（以格為單位的相對值；台間距不影響左右順序）。"""
    if not p or p[:2] not in ('1P', '2P'):
        return None
    return (-10 if p[0] == '1' else 10) + {'←': -1, '↓': 0, '↑': 0, '→': 1}.get(p[2:], 0)


def com_events(t, fps, com_obs, com_rel, steps, hold_mask, axis, panel_cm, target=15, lo=10, hi=20, lay=None):
    n = len(t)
    dp = bool(lay and lay['mode'] == 'dp')
    arrows = ARROWS4 if not dp else tuple(lay['sym'].values())
    v = np.asarray(com_obs, float)
    ok = np.isfinite(v)
    if ok.sum() < fps * 5 or axis is None:
        return [], {}
    base = float(np.nanmedian(v))
    d = np.where(ok, v - base, 0.0)
    d = uniform_filter1d(d, max(1, int(0.1 * fps)))
    d[~ok] = 0.0
    if steps:                                   # 只看實際在踩的範圍：前後 1 秒內至少 3 步
        st_t = np.array([s_['t'] for s_ in steps])
        cnt = np.searchsorted(st_t, t + 1.0) - np.searchsorted(st_t, t - 1.0)
        d[cnt < 3] = 0.0
    d[(t < t[0] + 1.5) | (t > t[-1] - 1.5)] = 0.0   # 頭尾平滑邊界不可靠
    mad = float(np.nanmedian(np.abs(d[ok]))) * 1.4826 + 1e-6

    # 候選峰值：由大到小挑，彼此間隔 ≥ 0.6 秒
    order = np.argsort(-np.abs(d))
    gap = int(0.6 * fps)
    picks = []
    taken = np.zeros(n, bool)
    for i in order:
        if taken[i] or not ok[i]:
            continue
        a = abs(d[i])
        if len(picks) >= hi or a < max(0.10, 1.5 * mad):
            break
        if len(picks) >= target and a < max(0.12, 2.5 * mad):
            break
        picks.append(i)
        taken[max(0, i - gap):i + gap + 1] = True
    if len(picks) < lo:
        picks = picks[:len(picks)]

    rel_base = float(np.nanmedian(com_rel)) if np.isfinite(com_rel).any() else None
    step_t = np.array([s['t'] for s in steps]) if steps else np.zeros(0)
    rate_all = len(steps) / max(1e-6, t[-1] - t[0])
    hold_any = None
    if hold_mask:
        hold_any = np.any(np.stack(list(hold_mask.values())), 0)
    hold_base = float(hold_any.mean()) if hold_any is not None else 0

    events = []
    for i in sorted(picks):
        pk = d[i]
        a = i
        while a > 0 and np.sign(d[a - 1]) == np.sign(pk) and abs(d[a - 1]) > 0.5 * abs(pk):
            a -= 1
        b = i
        while b < n - 1 and np.sign(d[b + 1]) == np.sign(pk) and abs(d[b + 1]) > 0.5 * abs(pk):
            b += 1
        t0, t1 = float(t[a]), float(t[b])
        # 類型：重心離開雙腳（相對雙腳也變很多）或整個人移過去（腳跟著走）
        typ = 'travel'
        if rel_base is not None:
            w = com_rel[max(0, i - 4):i + 5]
            w = w[np.isfinite(w)]
            if len(w) and abs(np.median(w) - rel_base) > 0.5 * abs(pk):
                typ = 'balance'
        win = [s for s in steps if t0 - 0.5 <= s['t'] <= t1 + 0.1]
        if not win:
            continue
        causes = []
        if dp:
            cross = _dp_crossover(steps, win)
        else:
            cross = any((s['foot'] == 'L' and s.get('panel') == '→') or (s['foot'] == 'R' and s.get('panel') == '←') for s in win)
        if cross:
            causes.append('crossover')
        if any(x['foot'] != y['foot'] and abs(x['t'] - y['t']) <= 0.06 for x, y in zip(win[:-1], win[1:])):
            causes.append('jump')
        if any(x['foot'] == y['foot'] and x.get('panel') != y.get('panel')
               and x.get('panel') in arrows and y.get('panel') in arrows for x, y in zip(win[:-1], win[1:])):
            causes.append('same_foot')
        if any(_wide(x.get('panel'), y.get('panel'), dp) for x, y in zip(win[:-1], win[1:])):
            causes.append('wide')
        if hold_any is not None and hold_base > 0.6:
            h = hold_any[max(0, a - int(0.3 * fps)):b + 1]
            if len(h) and h.mean() < 0.5:
                causes.append('bar_release')
        dur = max(0.3, t1 - t0 + 0.6)
        if len(win) / dur > 1.3 * rate_all:
            causes.append('fast')
        events.append(dict(
            t=_r(t[i]), t0=_r(t0), t1=_r(t1), frame=int(i), f0=int(a), f1=int(b),
            dev=_r(pk), dev_cm=_r(abs(pk) * panel_cm, 1),
            dir='pos' if pk > 0 else 'neg', type=typ, causes=causes,
            seq=[dict(t=s['t'], foot=s['foot'], panel=s.get('panel'), part=s.get('part'),
                      heel_up=s.get('heel_up')) for s in win]))

    # 配置統計：每個三步配置之後 0.35 秒內的最大偏離，平均起來排序
    arrows = [s for s in steps if s.get('panel') in arrows]
    resp = {}
    span = int(0.35 * fps)
    for k in range(2, len(arrows)):
        key = arrows[k - 2]['panel'] + arrows[k - 1]['panel'] + arrows[k]['panel']
        f = arrows[k]['frame']
        r = float(np.max(np.abs(d[f:f + span + 1]))) if f < n else 0.0
        resp.setdefault(key, []).append(r)
    all_r = [x for v in resp.values() for x in v]
    mean_all = float(np.mean(all_r)) if all_r else 0.0
    pats = [dict(pattern=k, n=len(v), mean_cm=_r(np.mean(v) * panel_cm, 1),
                 ratio=_r(np.mean(v) / mean_all, 2) if mean_all else None)
            for k, v in resp.items() if len(v) >= 5]
    pats.sort(key=lambda p: -(p['ratio'] or 0))
    cause_cnt = {}
    for e in events:
        for c in e['causes']:
            cause_cnt[c] = cause_cnt.get(c, 0) + 1
    stats = dict(baseline=_r(base), baseline_cm=_r(base * panel_cm, 1), mad_cm=_r(mad * panel_cm, 1),
                 mean_resp_cm=_r(mean_all * panel_cm, 1), patterns=pats[:8], causes=cause_cnt,
                 types={tp: sum(1 for e in events if e['type'] == tp) for tp in ('travel', 'balance')})
    return events, stats


def _wide(a, b, dp):
    if not dp:
        return (a, b) in OPPOSITE
    if not a or not b or a[:2] not in ('1P', '2P') or b[:2] not in ('1P', '2P'):
        return False
    return (a[0] == b[0] and (a[2:], b[2:]) in OPPOSITE) or a[0] != b[0]


def _dp_crossover(steps, win):
    """DP 交叉步：某隻腳落地時，落點在另一隻腳（最近一次落地）的另一側。"""
    last = {}
    for s in steps:
        if s['t'] > win[-1]['t']:
            break
        x = _dp_x(s.get('panel'))
        if x is None:
            continue
        o = last.get('R' if s['foot'] == 'L' else 'L')
        if s in win and o is not None and ((s['foot'] == 'L' and x > o) or (s['foot'] == 'R' and x < o)):
            return True
        last[s['foot']] = x
    return False
