"""體幹指標與 DP 台別區間。

單一斜角鏡頭下，所有角度都是「畫面上」量到的，再用垂直消失點（estimate_vertical）校正鉛直方向、
用踏板座標的 x 軸決定玩家的左右。前後方向（深度）的傾斜量不到，所以：
  傾斜、肩線：信心中（只量得到畫面平面內的分量）
  頭的位置：信心中～低（從背後拍，頭部點較不穩）
  手腕外展：信心中（畫面外或被遮住的手已排除）
  扭轉：信心低（肩寬／骨盆寬的「看起來」比例，會受站位與鏡頭角度影響，只適合看相對變化）
  台移動時上半身延遲：信心中（只用可觀測軸的分量，可觀測軸不接近左右時不計）
注意：「左右傾斜」其實是沿畫面水平方向量的。斜後方拍攝時這個方向在踏板上不是純左右（見 trunk_axis），
前傾／後仰也會混進來（例：軸為右後↔左前時，往前傾會被讀成往左）。輔助判斷：
  肩線傾斜（sh_tilt）幾乎不受前傾影響；體幹看起來變短（trunk_len_dev_pct < 0）通常代表前傾。

DP 區間（每幀）：'1'＝P1 台、'2'＝P2 台、'c'＝中央（跨兩台站 ≥ CENTER_MIN_S 秒）、'm'＝移動中。
"""
from __future__ import annotations

import numpy as np

CENTER_MIN_S = 0.8          # 跨兩台站超過這個時間才算「中央」站位，否則是移動途中
REGIONS = ('1', '2', 'm', 'c')
CONF = {'lean': 'medium', 'sh_tilt': 'medium', 'head_off': 'medium-low', 'wrist': 'medium',
        'twist': 'low', 'trunk_len': 'low', 'move_lag': 'medium'}


def _r(x, d=3):
    return None if x is None or not np.isfinite(x) else round(float(x), d)


# ───────────────────────── DP 台別區間 ─────────────────────────

def _step_side(st, lay):
    p = st.get('panel') or ''
    if p[:2] in ('1P', '2P'):
        return p[0]
    if p == '台間':
        return None
    pad = st.get('pad')
    if pad and pad[0] is not None:
        return '1' if pad[0] < 0 else '2'
    return None


def pad_regions(n, fps, steps, lay):
    """每幀所在區間與台移動列表。回傳 (region (n,) object, moves)。
    每隻腳的狀態＝最近一次落地所在的台；兩腳同台＝該台，分在兩台＝跨站。
    跨站持續 ≥ CENTER_MIN_S 秒為「中央」；較短的跨站若前後台不同為「移動中」，相同則視為原台（單腳伸過去又回來）。"""
    side = {s: np.full(n, None, object) for s in 'LR'}
    for s in 'LR':
        ss = sorted([st for st in steps if st['foot'] == s], key=lambda st: st['frame'])
        for a, b in zip(ss, ss[1:] + [None]):
            f0, f1 = a['frame'], (b['frame'] if b else n)
            side[s][f0:f1] = _step_side(a, lay)
    raw = np.full(n, None, object)
    for i in range(n):
        l, r = side['L'][i], side['R'][i]
        if l is None and r is None:
            continue
        raw[i] = l if l == r else 's'
    region = raw.copy()
    # 跨站片段
    i = 0
    while i < n:
        if raw[i] != 's':
            i += 1; continue
        j = i
        while j < n and raw[j] == 's':
            j += 1
        prev = next((raw[k] for k in range(i - 1, -1, -1) if raw[k] in ('1', '2')), None)
        nxt = next((raw[k] for k in range(j, n) if raw[k] in ('1', '2')), None)
        if (j - i) / fps >= CENTER_MIN_S:
            lab = 'c'
        elif prev and nxt and prev != nxt:
            lab = 'm'
        else:
            lab = prev or nxt
        region[i:j] = lab
        i = j
    # 台移動：P1／P2／中央 之間的切換
    moves = []
    stable = [(i, region[i]) for i in range(n) if region[i] in ('1', '2', 'c')]
    k = 0
    while k < len(stable) - 1:
        i, a = stable[k]
        j, b = stable[k + 1]
        if a != b:
            f0, f1 = i + 1, j - 1                 # 中間的「移動中」幀（直接切換時 f0 > f1）
            if f0 > f1:
                f0 = f1 = j
            moves.append(dict(frame0=int(f0), frame1=int(f1), frm=a, to=b))
        k += 1
    return region, moves


# ───────────────────────── 體幹逐幀指標 ─────────────────────────

def trunk_series(X, S, rel, vp, right_img, arm_est, vert_dir, vx=None):
    """逐幀：lean（體幹左右傾斜，°，＋＝往玩家右側）、sh_tilt（肩線相對骨盆線的傾斜，°，＋＝右肩較低）、
    twist（肩寬／骨盆寬，畫面上）、head_off（頭相對肩中點，肩寬為 1，＋＝右）、
    wrist_L／wrist_R（手腕離體幹中線的距離，肩寬為 1，＋＝往外）。
    right_img：(n,2) 每幀在骨盆附近「玩家右方」的畫面方向。
    vx：(n,3) 每幀踏板 x 軸（左右）的消失點（齊次，目前幀）。三維中與左右平行的線在畫面上都指向它，
    所以「水平的左右」在每個點各自是指向 vx 的方向；肩膀比鏡頭高、骨盆接近鏡頭高度時兩者的方向不同，
    只用骨盆處的方向量肩線，會把透視造成的傾斜誤當成「右肩低」（斜後方、鏡頭在胸口高度時約 5–10°）。"""
    n = len(X)
    ok = lambda j: rel[:, j] & (S[:, j] >= 0.5)
    hipL, hipR, shL, shR = X[:, 11], X[:, 12], X[:, 5], X[:, 6]
    hip = (hipL + hipR) / 2
    shm = (shL + shR) / 2
    base = ok(5) & ok(6) & ok(11) & ok(12)
    up = np.full((n, 2), np.nan)
    for i in np.where(base)[0]:
        up[i] = -vert_dir(hip[i], vp)
    r = right_img - (right_img * up).sum(1, keepdims=True) * up      # 與鉛直垂直的「右」
    r = r / (np.linalg.norm(r, axis=1, keepdims=True) + 1e-9)
    tv = shm - hip
    lean = np.degrees(np.arctan2((tv * r).sum(1), (tv * up).sum(1)))

    def hdir(p):
        """點 p 處「三維水平左右」的畫面方向（指向玩家右方）。沒有 vx 時用骨盆處的 r。"""
        if vx is None:
            return r
        w_ = vx[:, 2:3]
        far = np.abs(w_) < 1e-9
        d = np.where(far, vx[:, :2], vx[:, :2] / np.where(far, 1.0, w_) - p)
        d = d / (np.linalg.norm(d, axis=1, keepdims=True) + 1e-9)
        return np.where(((d * r).sum(1) < 0)[:, None], -d, d)

    def down_at(p):
        if vp is None:
            return np.tile([0.0, 1.0], (len(p), 1))
        d = (np.tile(vp[:2], (len(p), 1)) if abs(vp[2]) < 1e-6 else vp[:2] / vp[2] - p)
        d = d / (np.linalg.norm(d, axis=1, keepdims=True) + 1e-9)
        return np.where((d[:, 1] < 0)[:, None], -d, d)

    def tilt(a, b):
        """線 a→b 相對該處水平左右的傾斜（°，＋＝b 側較低）。"""
        m_ = (a + b) / 2
        d = hdir(m_)
        nn = np.c_[-d[:, 1], d[:, 0]]
        nn = np.where(((nn * down_at(m_)).sum(1) < 0)[:, None], -nn, nn)
        w = b - a
        return np.degrees(np.arctan2((w * nn).sum(1), (w * d).sum(1)))
    sh_tilt = tilt(shL, shR) - tilt(hipL, hipR)
    shw = np.linalg.norm(shR - shL, axis=1)
    hpw = np.linalg.norm(hipR - hipL, axis=1)
    twist = shw / (hpw + 1e-6)
    head = np.where((rel[:, 17] & (S[:, 17] >= 0.4))[:, None], X[:, 17], (X[:, 3] + X[:, 4]) / 2)
    head_ok = (rel[:, 17] & (S[:, 17] >= 0.4)) | (ok(3) & ok(4))
    head_off = ((head - shm) * hdir(shm)).sum(1) / (shw + 1e-6)        # 用肩膀高度的水平方向
    nrm = np.c_[-tv[:, 1], tv[:, 0]]
    nrm = nrm / (np.linalg.norm(nrm, axis=1, keepdims=True) + 1e-9)
    nrm = np.where(((nrm * r).sum(1) < 0)[:, None], -nrm, nrm)       # 指向玩家右側
    wr = {}
    for s_, j, sgn in (('L', 9, -1), ('R', 10, 1)):
        d = sgn * ((X[:, j] - hip) * nrm).sum(1) / (shw + 1e-6)
        good = base & ok(j) & ~np.asarray(arm_est[s_], bool)
        wr[s_] = np.where(good, d, np.nan)
    bad = ~base
    tlen = np.linalg.norm(tv, axis=1) / (shw + hpw + 1e-6)          # 體幹看起來的長度（以肩寬＋骨盆寬正規化）
    out = {'lean': lean, 'sh_tilt': sh_tilt, 'twist': twist, 'head_off': np.where(head_ok, head_off, np.nan),
           'wrist_L': wr['L'], 'wrist_R': wr['R'], 'trunk_len': tlen}
    for k in out:
        out[k] = np.where(bad, np.nan, out[k])
    return out


def summarize(series, region, play, dp):
    """區間別中位數。SP 只有 'all'；DP 為 '1'／'2'／'m'／'c'（樣本少於 15 幀的區間不列）。"""
    tw = series['twist'][play & np.isfinite(series['twist'])]
    tw_base = float(np.median(tw)) if len(tw) else np.nan
    tl = series['trunk_len'][play & np.isfinite(series['trunk_len'])]
    tl_base = float(np.median(tl)) if len(tl) else np.nan
    groups = {'all': play} if not dp else {g: play & (region == g) for g in REGIONS}
    out = {}
    for g, m in groups.items():
        if m.sum() < 15:
            continue
        row = {'frames': int(m.sum())}
        for k in ('lean', 'sh_tilt', 'head_off', 'wrist_L', 'wrist_R', 'twist', 'trunk_len'):
            v = series[k][m]
            v = v[np.isfinite(v)]
            row[k] = _r(np.median(v), 3 if k not in ('lean', 'sh_tilt') else 1) if len(v) >= 10 else None
        if row.get('twist') is not None and np.isfinite(tw_base):
            row['twist_dev_pct'] = _r((row['twist'] / tw_base - 1) * 100, 1)
        if row.get('trunk_len') is not None and np.isfinite(tl_base):
            row['trunk_len_dev_pct'] = _r((row['trunk_len'] / tl_base - 1) * 100, 1)
        out[g] = row
    return out, _r(tw_base)


# ───────────────────────── 台移動時上半身延遲 ─────────────────────────

def move_lag(moves, t, fps, pel, shd, axis_ok, panel_cm, lean=None):
    """每次台移動：骨盆與肩中點（都投到地面、踏板座標可觀測軸上）各自走到移動量 50% 的時間差
    （lag_ms，＋＝肩膀比骨盆晚到），以及骨盆走到 50% 那一刻肩膀「留在後面」的距離（trail_cm，＋＝落後）。"""
    out = []
    pad = int(0.4 * fps)
    for mv in moves:
        a, b = max(0, mv['frame0'] - pad), min(len(t) - 1, mv['frame1'] + pad)
        row = dict(mv, t0=_r(t[mv['frame0']]), t1=_r(t[mv['frame1']]))
        if mv['frm'] == 'c' or mv['to'] == 'c' or not axis_ok:
            out.append(row); continue
        p, q = pel[a:b + 1], shd[a:b + 1]
        okp, okq = np.isfinite(p), np.isfinite(q)
        if okp.sum() < 6 or okq.sum() < 6:
            out.append(row); continue
        p0, p1 = np.median(p[okp][:4]), np.median(p[okp][-4:])
        q0, q1 = np.median(q[okq][:4]), np.median(q[okq][-4:])
        dist = p1 - p0
        if abs(dist) < 0.8:                       # 骨盆沒有真的橫移（< 0.8 格）
            out.append(row); continue
        sg = np.sign(dist)
        tp, tq = _cross50(t[a:b + 1], p, p0, p1), _cross50(t[a:b + 1], q, q0, q1)
        row['dist_cm'] = _r(abs(dist) * panel_cm, 1)
        if tp is not None and tq is not None:
            row['lag_ms'] = _r((tq - tp) * 1000, 0)
            k = int(np.argmin(np.abs(t[a:b + 1] - tp)))
            if np.isfinite(p[k]) and np.isfinite(q[k]):
                row['trail_cm'] = _r((p[k] - q[k]) * sg * panel_cm, 1)
        if lean is not None:
            lv = lean[mv['frame0']:mv['frame1'] + 1]
            lv = lv[np.isfinite(lv)]
            if len(lv):
                row['lean'] = _r(np.median(lv), 1)
        out.append(row)
    return out


def _cross50(tt, v, v0, v1):
    m = np.isfinite(v)
    if m.sum() < 2:
        return None
    tt, v = tt[m], (v[m] - v0) / (v1 - v0)
    idx = np.where((v[:-1] < 0.5) & (v[1:] >= 0.5))[0]
    if not len(idx):
        return None
    i = idx[0]
    return float(tt[i] + (0.5 - v[i]) / (v[i + 1] - v[i] + 1e-9) * (tt[i + 1] - tt[i]))
