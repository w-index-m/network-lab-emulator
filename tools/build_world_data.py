#!/usr/bin/env python3
"""
/cyber-view の WORLD(地球儀)用の地図データを作る。出力: static/world/world.json

入力(npm レジストリから取れる公開データ):
  - world-atlas@2 の countries-110m.json(Natural Earth 1:110m、パブリックドメイン。world-atlas は ISC)
  - i18n-iso-countries の codes.json と langs/ja.json・en.json(MIT。ISO 3166 の英字2桁⇔数字3桁と国名)

  curl -sO https://registry.npmjs.org/world-atlas/-/world-atlas-2.0.2.tgz && tar xzf world-atlas-2.0.2.tgz
  python3 tools/build_world_data.py package/countries-110m.json <i18n-iso-countries の package ディレクトリ>

出力の形:
  {"lines": [[[lon, lat], ...], ...],            国境・海岸線(TopoJSON の arc を1本ずつ。重複なし)
   "countries": {"JP": ["Japan", "日本", lat, lon], ...}}   国の代表点(いちばん大きい陸地の重心)
"""
import json
import os
import sys


# 1:110m の地図に無い小さな国・地域の代表点(緯度, 経度)
SMALL = {'SG': (1.35, 103.82), 'HK': (22.32, 114.17), 'MO': (22.2, 113.55), 'BH': (26.07, 50.55), 'MT': (35.94, 14.38),
         'LU': (49.82, 6.13), 'MU': (-20.35, 57.55), 'AD': (42.55, 1.6), 'MC': (43.74, 7.42), 'LI': (47.17, 9.56),
         'SM': (43.94, 12.46), 'MV': (3.2, 73.22), 'BB': (13.19, -59.54), 'BM': (32.32, -64.76), 'GI': (36.14, -5.35),
         'IM': (54.24, -4.55), 'JE': (49.21, -2.13), 'GG': (49.45, -2.58), 'KY': (19.31, -81.25), 'AW': (12.52, -69.97),
         'CW': (12.17, -68.98), 'RE': (-21.12, 55.54), 'GP': (16.27, -61.55), 'MQ': (14.64, -61.02), 'GU': (13.44, 144.79),
         'PR': (18.22, -66.59), 'MS': (16.74, -62.19), 'SC': (-4.68, 55.49), 'CV': (16.0, -24.01), 'KN': (17.36, -62.78),
         'LC': (13.91, -60.98), 'VC': (12.98, -61.29), 'GD': (12.26, -61.6), 'AG': (17.06, -61.8), 'DM': (15.41, -61.37),
         'TO': (-21.18, -175.2), 'WS': (-13.76, -172.1), 'PW': (7.51, 134.58), 'FM': (7.43, 150.55), 'MH': (7.13, 171.18)}


def decode_arcs(topo):
    sx, sy = topo['transform']['scale']
    tx, ty = topo['transform']['translate']
    out = []
    for arc in topo['arcs']:
        x = y = 0
        pts = []
        for dx, dy in arc:
            x += dx
            y += dy
            pts.append([round(x * sx + tx, 2), round(y * sy + ty, 2)])
        out.append(pts)
    return out


def ring_points(ring, arcs):
    pts = []
    for i in ring:
        a = arcs[i] if i >= 0 else arcs[~i][::-1]
        pts.extend(a if not pts else a[1:])
    return pts


def centroid(pts):
    """平面の多角形の面積と重心(経緯度のまま。表示用の代表点なので十分)"""
    a = cx = cy = 0.0
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
        c = x1 * y2 - x2 * y1
        a += c
        cx += (x1 + x2) * c
        cy += (y1 + y2) * c
    if abs(a) < 1e-9:
        return 0.0, pts[0]
    return abs(a) / 2, [cx / (3 * a), cy / (3 * a)]


def main(topo_path, iso_dir, out_path):
    topo = json.load(open(topo_path, encoding='utf-8'))
    arcs = decode_arcs(topo)
    codes = {num: a2 for a2, _a3, num, _ in json.load(open(os.path.join(iso_dir, 'codes.json'), encoding='utf-8'))}
    names = {lang: json.load(open(os.path.join(iso_dir, 'langs', f'{lang}.json'), encoding='utf-8'))['countries']
             for lang in ('en', 'ja')}
    nm = lambda lang, a2: (lambda v: v[0] if isinstance(v, list) else v)(names[lang].get(a2, a2))  # noqa: E731
    countries = {}
    for g in topo['objects']['countries']['geometries']:
        a2 = codes.get(str(g.get('id', '')).zfill(3))
        if not a2:
            continue
        polys = g['arcs'] if g['type'] == 'MultiPolygon' else [g['arcs']]
        best = max((centroid(ring_points(p[0], arcs)) for p in polys), key=lambda t: t[0])
        lon, lat = best[1]
        countries[a2] = [nm('en', a2), nm('ja', a2), round(lat, 2), round(lon, 2)]
    for a2, (lat, lon) in SMALL.items():          # 1:110m に無い小さな国・地域(統計にはよく出る)
        countries.setdefault(a2, [nm('en', a2), nm('ja', a2), lat, lon])
    lines = [a for a in arcs if len(a) >= 2]
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump({'source': 'Natural Earth 1:110m (public domain) via world-atlas; names: i18n-iso-countries',
                   'lines': lines, 'countries': countries}, f, ensure_ascii=False, separators=(',', ':'))
    print(f'{out_path}: lines={len(lines)} countries={len(countries)} bytes={os.path.getsize(out_path)}')


if __name__ == '__main__':
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else os.path.join(root, 'static', 'world', 'world.json'))
