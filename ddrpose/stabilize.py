"""鏡頭移動補償：原則上鏡頭固定，只有真的移動不少時才換一組單應矩陣（分段常數）。

每 KEY_SEC 秒取一個關鍵幀，用背景特徵點（排除玩家）對參考幀估單應矩陣。
逐幀估計會把遊戲畫面、毛巾、人的動作誤當成鏡頭晃動，讓九宮格跟著抖，所以不逐幀補償：
相鄰關鍵幀的估計差距小於 MOVE_FRAC（畫面寬）就視為沒動；超過且下一個關鍵幀也確認，才切換新的一段。

輸出 data/<vid>/motion.npz：
  M    (N,3,3)  原始解析度下，第 t 幀座標 → 參考幀座標
  ref  ()       參考幀索引
  inl  (N,)     關鍵幀的 RANSAC 內點數（非關鍵幀為 0）
  ok   (N,)     該幀附近的關鍵幀有對上參考幀（False＝鏡頭拍到別處，例如拿起手機拍結算畫面）
  seg  (N,)     鏡頭位置分段編號
  ver  ()       格式版本
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

VERSION = 2
KEY_SEC = 2.5       # 每幾秒檢查一次鏡頭位置
MOVE_FRAC = 0.03    # 估計位移超過畫面寬的 3% 才算鏡頭真的移動
MIN_INLIERS = 60


def _person_mask(shape, kp, sc, scale):
    h, w = shape
    m = np.full((h, w), 255, np.uint8)
    ok = sc > 0.2
    if ok.sum() >= 3:
        pts = kp[ok] / scale
        x0, y0 = pts.min(0); x1, y1 = pts.max(0)
        pw, ph = (x1 - x0) * 0.25 + 30, (y1 - y0) * 0.15 + 30
        m[max(0, int(y0 - ph)):int(y1 + ph), max(0, int(x0 - pw)):int(x1 + pw)] = 0
    return m


def _disp(A, B, probes):
    """兩個單應矩陣把同一組點送到的位置平均差多少像素。"""
    pa = probes @ A.T; pb = probes @ B.T
    return float(np.mean(np.linalg.norm(pa[:, :2] / pa[:, 2:] - pb[:, :2] / pb[:, 2:], axis=1)))


def compute_motion(vid_dir: str | Path, orig_w: int):
    vid_dir = Path(vid_dir)
    z = np.load(vid_dir / 'pose.npz')
    K, Sc, n, valid = z['kpts'], z['scores'], len(z['t']), z['valid']
    fps = 1.0 / np.median(np.diff(z['t'])) if n > 1 else 30.0
    step = max(1, int(round(KEY_SEC * fps)))
    cap = cv2.VideoCapture(str(vid_dir / 'preview.mp4'))
    pw = cap.get(cv2.CAP_PROP_FRAME_WIDTH) or orig_w
    ph = cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or pw
    scale = orig_w / pw                      # 預覽 → 原始
    orb = cv2.ORB_create(2500, fastThreshold=12)
    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)

    # 參考幀：影片中間（手動校正的點存在參考幀座標，不要隨意改）；中間沒抓到人才改用有人的幀的中間
    vi = np.where(valid)[0]
    ref = n // 2 if valid[n // 2] or not len(vi) else int(vi[len(vi) // 2])
    cap.set(cv2.CAP_PROP_POS_FRAMES, ref)
    ok, fr = cap.read()
    if not ok:
        return None
    g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
    kr, dr = orb.detectAndCompute(g, _person_mask(g.shape, K[ref], Sc[ref], scale))
    if dr is None or len(kr) < 50:
        return None
    pr = np.float32([k.pt for k in kr])

    keys = sorted(set(range(0, n, step)) | {ref, n - 1})
    Ms, inls = [], []
    for i in keys:
        if i == ref:
            Ms.append(np.eye(3)); inls.append(len(kr)); continue
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ok, fr = cap.read()
        M, ni = None, 0
        if ok:
            g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
            kc, dc = orb.detectAndCompute(g, _person_mask(g.shape, K[i], Sc[i], scale))
            if dc is not None and len(kc) >= 30:
                pc = np.float32([k.pt for k in kc])
                mt = bf.match(dc, dr)
                if len(mt) >= 30:
                    M, inl = cv2.findHomography(pc[[m.queryIdx for m in mt]], pr[[m.trainIdx for m in mt]],
                                                cv2.RANSAC, 3.0)
                    ni = int(inl.sum()) if M is not None else 0
        Ms.append(M / M[2, 2] if M is not None and ni >= MIN_INLIERS else None); inls.append(ni)
    cap.release()

    # 分段：鏡頭原則上固定；估計位移超過門檻、且下一個有效關鍵幀也確認，才開新的一段
    probes = np.array([[x * pw, y * ph, 1.0] for x in (0.2, 0.5, 0.8) for y in (0.3, 0.6, 0.9)])
    thr = MOVE_FRAC * pw
    good = [k for k, M in enumerate(Ms) if M is not None]
    seg_of = {}
    segs = []                                # 每段的關鍵幀（keys 的索引）
    for j, k in enumerate(good):
        if not segs:
            segs.append([k]); seg_of[k] = 0; continue
        hold = np.median([Ms[q] for q in segs[-1]], 0)
        if _disp(Ms[k], hold, probes) > thr:
            nxt = good[j + 1] if j + 1 < len(good) else None
            if nxt is None or _disp(Ms[nxt], hold, probes) > thr:
                segs.append([k]); seg_of[k] = len(segs) - 1; continue
            continue                         # 單一關鍵幀估錯：忽略
        segs[-1].append(k); seg_of[k] = len(segs) - 1
    ref_k = keys.index(ref)
    Hs = [np.eye(3) if ref_k in s else np.median([Ms[q] for q in s], 0) for s in segs]

    # 每幀套用所屬段的矩陣；段與段在關鍵幀處切換
    Mall = np.tile(np.eye(3), (n, 1, 1)); seg = np.zeros(n, int); okf = np.zeros(n, bool)
    starts = [keys[s[0]] for s in segs]
    for si, H in enumerate(Hs):
        a = 0 if si == 0 else starts[si]
        b = starts[si + 1] if si + 1 < len(segs) else n
        Mall[a:b] = H; seg[a:b] = si
    for k, i in enumerate(keys):             # 關鍵幀有對上（且沒被當成估錯忽略）的附近才算 ok
        if k in seg_of:
            okf[max(0, i - step // 2):i + step // 2 + 1] = True
    S = np.diag([scale, scale, 1.0]); Si = np.diag([1 / scale, 1 / scale, 1.0])
    Mall = S @ Mall @ Si                     # 換到原始解析度座標
    inl_all = np.zeros(n, int); inl_all[keys] = inls
    np.savez_compressed(vid_dir / 'motion.npz', M=Mall, ref=ref, inl=inl_all, ok=okf, seg=seg, ver=VERSION)
    return Mall, ref


def stabilized_background(vid_dir: str | Path, M, orig_wh):
    """把 15 張分散的幀對齊到參考幀後取中位數，得到乾淨的背景（原始解析度的一半）。"""
    vid_dir = Path(vid_dir)
    cap = cv2.VideoCapture(str(vid_dir / 'preview.mp4'))
    n = len(M)
    pw = cap.get(cv2.CAP_PROP_FRAME_WIDTH); ph = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    s = pw / orig_wh[0]
    S = np.diag([s, s, 1.0]); Si = np.diag([1 / s, 1 / s, 1.0])
    frames = []
    for f in np.linspace(0, n - 1, 15).astype(int):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(f))
        ok, fr = cap.read()
        if ok:
            Mp = S @ M[f] @ Si
            frames.append(cv2.warpPerspective(fr, Mp, (int(pw), int(ph))))
    cap.release()
    if len(frames) < 3:
        return None, s
    return np.median(np.stack(frames), 0).astype(np.uint8), s
