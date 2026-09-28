"""鏡頭晃動補償：每幀相對參考幀的單應矩陣（以背景特徵點估計，排除玩家）。

輸出 data/<vid>/motion.npz：
  M    (N,3,3)  原始解析度下，第 t 幀座標 → 參考幀座標
  ref  ()       參考幀索引
  inl  (N,)     關鍵幀的 RANSAC 內點數（非關鍵幀為 0）
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

KEY_EVERY = 5


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


def compute_motion(vid_dir: str | Path, orig_w: int):
    vid_dir = Path(vid_dir)
    z = np.load(vid_dir / 'pose.npz')
    K, Sc, n = z['kpts'], z['scores'], len(z['t'])
    cap = cv2.VideoCapture(str(vid_dir / 'preview.mp4'))
    pw = cap.get(cv2.CAP_PROP_FRAME_WIDTH) or orig_w
    scale = orig_w / pw                      # 預覽 → 原始
    orb = cv2.ORB_create(2500, fastThreshold=12)
    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)

    ref = n // 2
    cap.set(cv2.CAP_PROP_POS_FRAMES, ref)
    ok, fr = cap.read()
    if not ok:
        return None
    g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
    kr, dr = orb.detectAndCompute(g, _person_mask(g.shape, K[ref], Sc[ref], scale))
    if dr is None or len(kr) < 50:
        return None
    pr = np.float32([k.pt for k in kr])

    keys, Ms, inls = [], [], []
    prev = None                              # (關鍵點, 描述子, 該幀→參考)
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    for i in range(n):
        ok, fr = cap.read()
        if not ok:
            break
        if i % KEY_EVERY and i != n - 1:
            continue
        g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
        kc, dc = orb.detectAndCompute(g, _person_mask(g.shape, K[i], Sc[i], scale))
        M, ni = None, 0
        if dc is not None and len(kc) >= 30:
            pc = np.float32([k.pt for k in kc])
            mt = bf.match(dc, dr)
            if len(mt) >= 30:
                M, inl = cv2.findHomography(pc[[m.queryIdx for m in mt]], pr[[m.trainIdx for m in mt]],
                                            cv2.RANSAC, 3.0)
                ni = int(inl.sum()) if M is not None else 0
            if (M is None or ni < 60) and prev is not None and prev[1] is not None:
                # 直接對參考幀配不起來 → 先對上一個關鍵幀，再串接
                mt = bf.match(dc, prev[1])
                if len(mt) >= 30:
                    M2, inl2 = cv2.findHomography(pc[[m.queryIdx for m in mt]],
                                                  prev[0][[m.trainIdx for m in mt]], cv2.RANSAC, 3.0)
                    if M2 is not None and inl2.sum() >= 40:
                        M, ni = prev[2] @ M2, int(inl2.sum())
            prev = (pc, dc, M if M is not None else (prev[2] if prev else np.eye(3)))
        if M is None:
            M = Ms[-1] if Ms else np.eye(3)
        keys.append(i); Ms.append(M / M[2, 2]); inls.append(ni)
    cap.release()

    keys = np.array(keys); Ms = np.array(Ms)
    Mall = np.empty((n, 3, 3))
    for a in range(3):
        for b in range(3):
            Mall[:, a, b] = np.interp(np.arange(n), keys, Ms[:, a, b])
    S = np.diag([scale, scale, 1.0]); Si = np.diag([1 / scale, 1 / scale, 1.0])
    Mall = S @ Mall @ Si                     # 換到原始解析度座標
    inl_all = np.zeros(n, int); inl_all[keys] = inls
    np.savez_compressed(vid_dir / 'motion.npz', M=Mall, ref=ref, inl=inl_all)
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
