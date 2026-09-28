"""踏板亮燈校正：DDR 箭頭板被踩時會亮紅／藍燈。把整支影片的亮燈像素累積到參考幀，
四團亮區就是四個箭頭板，其重心即箭頭板中心（比腳印分群準，腳印會偏向中央）。

輸出 data/<vid>/glow.npz：F（預覽解析度、參考幀座標的亮燈頻率圖）、scale（預覽/原始）
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from itertools import combinations

CELL = 8          # 逐幀亮燈圖的縮小倍率（預覽解析度 / 8）


def glow_map(vid_dir: str | Path, M, orig_w: int):
    """回傳 (F, scale, Fr)：F 亮燈頻率圖；Fr 每幀亮燈圖（每格 8×8 像素中亮的像素數，0–64）。"""
    vid_dir = Path(vid_dir)
    f = vid_dir / 'glow.npz'
    if f.exists():
        z = np.load(f)
        if 'Fr' in z and len(z['Fr']) == len(M):
            return z['F'], float(z['scale']), z['Fr']
    cap = cv2.VideoCapture(str(vid_dir / 'preview.mp4'))
    pw, ph = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    s = pw / orig_w
    S = np.diag([s, s, 1.0]); Si = np.diag([1 / s, 1 / s, 1.0])
    C = np.zeros((ph, pw), np.float32)
    Fr = np.zeros((len(M), ph // CELL, pw // CELL), np.uint8)
    i = 0
    while True:
        ok, fr = cap.read()
        if not ok or i >= len(M):
            break
        hsv = cv2.cvtColor(fr, cv2.COLOR_BGR2HSV)
        H, Sa, V = hsv[..., 0], hsv[..., 1], hsv[..., 2]
        m = ((Sa > 140) & (V > 140) & ((H < 8) | (H > 172) | ((H > 100) & (H < 130)))).astype(np.uint8)
        w = cv2.warpPerspective(m, S @ M[i] @ Si, (pw, ph), flags=cv2.INTER_NEAREST)
        C += w
        Fr[i] = (w[:Fr.shape[1] * CELL, :Fr.shape[2] * CELL]
                 .reshape(Fr.shape[1], CELL, Fr.shape[2], CELL).sum((1, 3))).astype(np.uint8)
        i += 1
    cap.release()
    F = C / max(i, 1)
    np.savez_compressed(f, F=F, scale=s, Fr=Fr)
    return F, s, Fr


def panel_lights(Fr, s, Hinv):
    """每幀四塊箭頭板區域內的亮燈比例 → {箭頭: (N,) 0–1（以該板 99 百分位正規化）}。
    Hinv：踏板座標 → 參考幀影像（原始解析度）。"""
    from .analyze import ARROWS, _apply_h
    h, w = Fr.shape[1:]
    out = {}
    for k, (ax, ay) in ARROWS.items():
        e = 0.55
        quad = _apply_h(Hinv, [[ax - e, ay - e], [ax + e, ay - e], [ax + e, ay + e], [ax - e, ay + e]]) * s / CELL
        mask = np.zeros((h, w), np.uint8)
        cv2.fillPoly(mask, [np.round(quad).astype(np.int32)], 1)
        if mask.sum() == 0:
            out[k] = np.zeros(len(Fr)); continue
        act = Fr[:, mask > 0].sum(1) / (64.0 * mask.sum())
        peak = np.percentile(act, 99)
        out[k] = np.clip(act / peak, 0, 1.5) if peak > 1e-3 else np.zeros(len(Fr))
    return out


def light_onsets(lights, on=0.4, off=0.2):
    """每塊板「由暗轉亮」的幀：[(幀, 箭頭鍵)]。"""
    out = []
    for k, act in lights.items():
        lit = False
        for i, v in enumerate(act):
            if not lit and v > on and act[max(0, i - 4):i + 1].min() < off:
                out.append((i, k)); lit = True
            elif lit and v < off:
                lit = False
    return sorted(out)


def lit_at_step(lights, a, foot_pad_pts=None, win=(4, 5)):
    """落地幀 a 附近「剛亮起來」的箭頭板；多塊同時亮起時取離這隻腳最近的。"""
    from .analyze import ARROWS, ARROW_SYM
    best = []
    for k, act in lights.items():
        lo, hi = max(0, a - win[0]), min(len(act), a + win[1] + 1)
        if hi - lo < 3:
            continue
        before = act[lo:a].min() if a > lo else act[lo]
        after = act[a:hi].max()
        if after > 0.35 and after - before > 0.25:
            best.append((k, after - before))
    if not best:
        return None
    if foot_pad_pts is not None:
        # 亮起的板必須在這隻腳附近（腳跟→腳尖各延伸 30% 後，離該板不超過 0.6 格），
        # 避免把另一隻腳同時踩下的板算到這隻腳
        hl, tp = np.asarray(foot_pad_pts[0], float), np.asarray(foot_pad_pts[1], float)
        v = tp - hl
        fs = (hl - 0.3 * v) + np.linspace(0, 1, 9)[:, None] * (1.6 * v)
        def dist(k):
            c = np.array(ARROWS[k])
            return float(np.min(np.maximum(np.abs(fs[:, 0] - c[0]), np.abs(fs[:, 1] - c[1]))) - 0.5)
        best = [kv for kv in best if dist(kv[0]) <= 0.6]
        if not best:
            return None
        best.sort(key=lambda kv: dist(kv[0]))
    else:
        best.sort(key=lambda kv: -kv[1])
    return ARROW_SYM[best[0][0]]


def _wkmeans(X, w, init, it=30):
    c = init.astype(float).copy()
    for _ in range(it):
        lab = np.argmin(((X[:, None] - c[None]) ** 2).sum(-1), 1)
        c2 = np.array([np.average(X[lab == k], axis=0, weights=w[lab == k]) if (lab == k).any() else c[k]
                       for k in range(len(c))])
        if np.allclose(c2, c, atol=0.3):
            break
        c = c2
    lab = np.argmin(((X[:, None] - c[None]) ** 2).sum(-1), 1)
    return c, lab


def glow_points(F, s, contacts_ref, fwd, init_pts=None):
    """回傳 {'L','D','U','R': 原始解析度參考幀座標}；找不到可信的四團亮區則回傳 None。"""
    if F is None or len(contacts_ref) < 12:
        return None
    G = F.copy()
    G[cv2.dilate((F > 0.5).astype(np.uint8), np.ones((9, 9), np.uint8)) > 0] = 0   # 常亮的燈（非踏板）
    # 只看腳附近：落點雲外擴
    P = np.asarray(contacts_ref) * s
    spread = float(np.percentile(np.linalg.norm(P - np.median(P, 0), axis=1), 90))
    roi = np.zeros(G.shape, np.uint8)
    for x, y in P:
        cv2.circle(roi, (int(x), int(y)), int(max(40, 1.8 * spread)), 1, -1)
    G = cv2.GaussianBlur(G * roi, (0, 0), 2.5)
    if G.max() <= 0:
        return None
    # 候選亮區：各門檻的連通區塊（取質量前 6），不夠時補上 k-means 結果
    cands = []
    for thr in (0.25, 0.15):
        mask = (G > thr * G.max()).astype(np.uint8)
        nlab, lab_img = cv2.connectedComponents(mask)
        comps = []
        for k in range(1, nlab):
            yy, xx = np.where(lab_img == k)
            ww = G[yy, xx]
            if ww.sum() > 0:
                comps.append((float(ww.sum()), np.array([np.average(xx, weights=ww), np.average(yy, weights=ww)])))
        comps.sort(key=lambda c: -c[0])
        cands.append(comps[:6])
    ys, xs = np.where(G > 0.15 * G.max())
    if len(xs) >= 40:
        X = np.c_[xs, ys].astype(float); w = G[ys, xs]
        init = [X[np.argmax(w)]]
        for _ in range(3):
            d2 = np.min([((X - c) ** 2).sum(1) for c in init], axis=0)
            init.append(X[np.argmax(w * d2)])
        cen, lab = _wkmeans(X, w, np.array(init))
        cands.append([(float(w[lab == k].sum()), cen[k]) for k in range(4)])

    med = np.median(P, 0)
    best = None
    for comps in cands:
        if len(comps) < 4:
            continue
        tot = sum(c[0] for c in comps)
        for combo in combinations(range(len(comps)), 4):
            pts = np.array([comps[k][1] for k in combo]); ms = np.array([comps[k][0] for k in combo])
            r = _label_diamond(pts, fwd)
            if r is None:
                continue
            lab, tt, center = r
            if ms.min() < 0.02 * tot:
                continue
            dist = np.linalg.norm(center - med) / (spread + 1e-6)
            score = ms.sum() / tot * np.exp(-dist ** 2 / 2) * np.exp(-((tt - 0.5) ** 2).sum() / 0.02)
            if best is None or score > best[0]:
                best = (score, {k: pts[i] for k, i in lab.items()}, (ms / ms.sum()).tolist())
    if best is None:
        return None
    out = {k: v / s for k, v in best[1].items()}
    out['_mass'] = best[2]
    return out


def _label_diamond(pts, fwd):
    """四點 → {L,D,U,R: index}。需為凸四邊形、對角線互相交於中段；
    ↑↓ 為最接近腳尖方向的那條對角線，由上往下看 ↑→↓← 為順時針（影像 y 向下時外積 > 0）。"""
    c = pts.mean(0)
    order = np.argsort(np.arctan2(pts[:, 1] - c[1], pts[:, 0] - c[0]))
    q = pts[order]
    cr = [np.cross(q[(i + 1) % 4] - q[i], q[(i + 2) % 4] - q[(i + 1) % 4]) for i in range(4)]
    if not (all(v > 0 for v in cr) or all(v < 0 for v in cr)):
        return None                                       # 非凸
    diags = [(order[0], order[2]), (order[1], order[3])]
    def cosf(a, b):
        v = pts[b] - pts[a]
        return abs(v @ fwd) / (np.linalg.norm(v) + 1e-9)
    ud, lr = sorted(diags, key=lambda d: -cosf(*d))
    if cosf(*ud) < 0.6:
        return None
    u, d = ud if (pts[ud[0]] - pts[ud[1]]) @ fwd > 0 else (ud[1], ud[0])
    a, b = lr
    r_, l_ = (a, b) if np.cross(pts[a] - pts[u], pts[d] - pts[a]) > 0 else (b, a)
    # 對角線交點參數
    A = np.array([pts[d] - pts[u], -(pts[l_] - pts[r_])]).T
    try:
        t1, t2 = np.linalg.solve(A, pts[r_] - pts[u])
    except np.linalg.LinAlgError:
        return None
    if not (0.2 < t1 < 0.8 and 0.2 < t2 < 0.8):
        return None
    center = pts[u] + t1 * (pts[d] - pts[u])
    return {'L': l_, 'D': d, 'U': u, 'R': r_}, np.array([t1, t2]), center
