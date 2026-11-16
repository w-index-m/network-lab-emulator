"""
設定ファイル(YAML)の読み込み

装置とつながり(トポロジ)は手書き。データの取り込み口(Prometheus / Loki / Zabbix)は
使うものだけ書けばよい。値に ${ENV_NAME} と書くと環境変数で置き換える(トークン等)。
"""
import os
import re

import yaml

_ENV = re.compile(r'\$\{([A-Z0-9_]+)\}')


def _expand(v):
    if isinstance(v, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), ''), v)
    if isinstance(v, list):
        return [_expand(x) for x in v]
    if isinstance(v, dict):
        return {k: _expand(x) for k, x in v.items()}
    return v


class ConfigError(ValueError):
    pass


def load(path):
    with open(path, encoding='utf-8') as f:
        raw = yaml.safe_load(f) or {}
    return validate(_expand(raw))


def validate(cfg):
    cfg.setdefault('title', 'CYBER VIEW')
    cfg.setdefault('poll_interval', 10)
    cfg.setdefault('link_speed_bps', 1_000_000_000)
    cfg.setdefault('sources', {})
    devs = cfg.get('devices') or []
    if not devs:
        raise ConfigError('devices が空です')
    ids = set()
    for d in devs:
        if not d.get('id'):
            raise ConfigError(f'id の無い装置があります: {d}')
        if d['id'] in ids:
            raise ConfigError(f"装置 id が重複しています: {d['id']}")
        ids.add(d['id'])
        d.setdefault('hostname', d['id'])
        d.setdefault('type', 'router')
        d.setdefault('interfaces', [])
        if not d.get('mgmt_ip') and d['interfaces']:
            d['mgmt_ip'] = d['interfaces'][0].get('ip')
        for i in d['interfaces']:
            if not i.get('name'):
                raise ConfigError(f"{d['id']}: name の無いインタフェースがあります")
            i.setdefault('prefix', 24)
    for ln in cfg.get('links') or []:
        for k in ('a', 'b', 'a_if', 'b_if'):
            if not ln.get(k):
                raise ConfigError(f'links に {k} がありません: {ln}')
        for k in ('a', 'b'):
            if ln[k] not in ids:
                raise ConfigError(f'links が未定義の装置を指しています: {ln[k]}')
        ln.setdefault('speed', cfg['link_speed_bps'])
    cfg.setdefault('links', [])
    return cfg
