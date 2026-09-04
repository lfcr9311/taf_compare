#!/usr/bin/env python3
"""
API TAF REDEMET vs METAR. Serve JSON; a interface vive no repositorio
taf_compare_web, que aponta para ca via config.js.

    python app.py [--host 0.0.0.0] [--port 8000] [--debug]   # local
    api/index.py                                             # serverless (Vercel)

Endpoints: /api/meta (listas dos filtros) e /api/stats (metricas por recorte).

Le data/malha.parquet, gerado por build_cache.py a partir de excel/malha.csv.
Rode `python build_cache.py` sempre que o malha.csv mudar.

Importa core.py, nao main.py: assim o bundle serverless nao carrega reportlab
nem psycopg2. Tudo e calculado ao vivo, por filtro, em cima do dataframe.
"""
import argparse
import os

from collections import Counter

import numpy as np
import pandas as pd
from flask import Flask, jsonify, request

import core as M

app = Flask(__name__)
DF = None  # carregado em load()

# O frontend e servido de outro dominio, entao o browser exige CORS.
# Dados publicos e somente leitura; restrinja com CORS_ORIGIN se precisar.
CORS_ORIGIN = os.getenv('CORS_ORIGIN', '*')


@app.after_request
def _cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = CORS_ORIGIN
    resp.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    resp.headers['Cache-Control'] = 'public, max-age=300'
    return resp

# tag -> (sufixo das colunas booleanas, lista de condicoes).
# 'E' compara o token cru; 'B' usa dangerous_hits (respeita +RA).
VIEWS = {'exato': ('E', M.DANGEROUS_CONDITIONS),
         'base':  ('B', M.DANGEROUS_BASE)}


def load():
    """Carrega o cache parquet (data/malha.parquet). Se nao existir, constroi
    a partir do CSV na hora - util em dev, inviavel em serverless."""
    if os.path.exists(M.MALHA_PARQUET):
        df = pd.read_parquet(M.MALHA_PARQUET)
    elif os.path.exists(M.MALHA_CSV):
        import build_cache            # so existe em dev; cortado pelo .vercelignore
        df = build_cache.build()
    else:
        raise RuntimeError(
            f"Cache nao encontrado em {M.MALHA_PARQUET} e sem {M.MALHA_CSV} para "
            f"reconstruir. Em deploy, confira 'includeFiles' no vercel.json e se "
            f"data/malha.parquet foi commitado. Local: rode python build_cache.py."
        )
    _derive(df)
    return df


def _derive(df):
    """Colunas derivadas, calculadas por categoria e espalhadas pelos codigos.

    Sao so 39 valores distintos de TAF e 102 de METAR. Resolvendo cada conjunto
    uma vez por categoria e indexando pelo codigo, as 263 mil linhas passam a
    compartilhar ~140 frozensets em vez de criar um objeto por linha - era isso
    que fazia o dataframe pesar 579 MB.
    """
    for src, base_col in (('taf', 'taf_base'), ('met', 'met_base')):
        cats = df[src].cat.categories
        codes = df[src].cat.codes.to_numpy()
        toks = [frozenset(c.split()) for c in cats]
        base = [frozenset(M.strip_intensity(t) for t in c.split()) for c in cats]
        dng = [frozenset(M.dangerous_hits(c.split())) for c in cats]

        # unica coluna de conjunto materializada: _forecast_analysis precisa dela
        df[base_col] = [base[i] for i in codes]

        pref = 't' if src == 'taf' else 'm'
        for tag, conds in VIEWS.values():
            sets = toks if tag == 'E' else dng
            for c in conds:
                hit = np.fromiter((c in s for s in sets), dtype=bool, count=len(sets))
                df[f'{pref}{tag}::{c}'] = hit[codes]
    return df


# ----------------------------------------------------------------------
# filtros
# ----------------------------------------------------------------------

def _multi(name):
    raw = request.args.get(name, '').strip()
    return [x for x in raw.split(',') if x] if raw else []


def _filtered(apply_group=True):
    d = DF
    for col, key, cast in [('icao', 'icao', str), ('season', 'season', str),
                           ('period', 'period', str), ('equipment', 'equipment', str),
                           ('month', 'month', int)]:
        vals = _multi(key)
        if vals:
            d = d[d[col].isin([cast(v) for v in vals])]
    grp = request.args.get('group', '').strip()
    if apply_group and grp in ('NavBrasil', 'CIMAER'):
        d = d[d['group'] == grp]
    return d


# ----------------------------------------------------------------------
# metricas
# ----------------------------------------------------------------------

def _presence(d):
    n = len(d)
    tf, ob = d['forecast'], d['observed']
    tp = int((tf & ob).sum())
    tn = int((~tf & ~ob).sum())
    fp = int((tf & ~ob).sum())
    fn = int((~tf & ob).sum())
    correct = tp + tn
    # Heidke Skill Score: desconta o acerto esperado por acaso (0 = sem destreza)
    exp = (((tp + fp) * (tp + fn) + (fn + tn) * (fp + tn)) / n) if n else 0
    hss = round(100 * (correct - exp) / (n - exp), 1) if n and (n - exp) else 0
    # piso trivial: previsor que sempre diz NSW
    base_rate = round(100 * (tn + fp) / n, 1) if n else 0
    return {
        'n': n,
        'correct': correct,
        'pct': round(100 * correct / n, 1) if n else 0,
        'hss': hss,
        'base_rate': base_rate,
        'tp': tp, 'tn': tn,
        'false_positive': fp,
        'miss': fn,
        'taf_wx': int(tf.sum()),
        'metar_wx': int(ob.sum()),
        'detection_pct': round(100 * tp / int(ob.sum()), 1) if ob.sum() else 0,
        'precision_pct': round(100 * tp / int(tf.sum()), 1) if tf.sum() else 0,
    }


def _per_condition(d, view):
    tag, conds = VIEWS[view]
    out = []
    for c in conds:
        nt = int(d[f't{tag}::{c}'].sum())
        nm = int(d[f'm{tag}::{c}'].sum())
        if nt == 0 and nm == 0:
            continue
        h = int((d[f't{tag}::{c}'] & d[f'm{tag}::{c}']).sum())
        out.append({
            'cond': c, 'taf_forecast': nt, 'confirmed': h,
            'precision_pct': round(100 * h / nt, 1) if nt else None,
            'metar_observed': nm, 'detected_pct': round(100 * h / nm, 1) if nm else None,
            'false_positive': nt - h, 'miss': nm - h,
        })
    out.sort(key=lambda r: r['taf_forecast'], reverse=True)
    return out


def _any_dangerous(d, view):
    tag, conds = VIEWS[view]
    tcols = [f't{tag}::{c}' for c in conds]
    mcols = [f'm{tag}::{c}' for c in conds]
    td = d[tcols].any(axis=1)
    md = d[mcols].any(axis=1)
    acerto = int((td & md).sum())
    fp = int((td & ~md).sum())
    miss = int((~td & md).sum())
    den = acerto + fp + miss
    return {
        'taf_flagged': int(td.sum()), 'metar_observed': int(md.sum()),
        'acerto': acerto, 'false_positive': fp, 'miss': miss,
        'accuracy_pct': round(100 * acerto / den, 1) if den else 0,
        'detection_pct': round(100 * acerto / (acerto + miss), 1) if (acerto + miss) else 0,
        'precision_pct': round(100 * acerto / (acerto + fp), 1) if (acerto + fp) else 0,
    }


def _per_airport(d):
    rows = []
    for icao, g in d.groupby('icao'):
        n = len(g)
        tf, ob = g['forecast'], g['observed']
        c = int((tf == ob).sum())
        rows.append({
            'icao': icao, 'group': g['group'].iat[0], 'n': n,
            'correct': c, 'pct': round(100 * c / n, 1) if n else 0,
            'false_positive': int((tf & ~ob).sum()), 'miss': int((~tf & ob).sum()),
        })
    rows.sort(key=lambda r: r['n'], reverse=True)
    return rows


def _breakdown(d, view, focus):
    tag, _conds = VIEWS[view]
    col = f't{tag}::{focus}'
    if col not in d.columns:
        return {'focus': focus, 'n': 0, 'rows': []}
    sub = d[d[col]]
    n = len(sub)
    vc = sub['met'].value_counts().head(12)
    return {'focus': focus, 'n': n,
            'rows': [{'metar': k, 'count': int(v), 'pct': round(100 * v / n, 1) if n else 0}
                     for k, v in vc.items()]}


def _forecast_analysis(d, only_observed=False):
    """Universo = voos em que o TAF do alternado previu fenomeno.
    only_observed descarta os casos em que o METAR nao acusou nada."""
    fc = d[d['forecast']]
    if only_observed:
        fc = fc[fc['observed']]
    n = len(fc)
    if not n:
        return {'n': 0, 'universe': 'observado' if only_observed else 'previsto',
                'nada': 0, 'outro': 0, 'base': 0, 'exato': 0,
                'by_token': [], 'confusion': {'cols': [], 'rows': []}}
    nada = int((~fc['observed']).sum())
    base = int(fc['bs_hit'].sum())
    exato = int(fc['ex_hit'].sum())
    outro = n - nada - base

    # por fenomeno previsto (token base do TAF)
    tally = Counter(t for s in fc['taf_base'] for t in s)
    by_token = []
    for tok, _ in tally.most_common(14):
        m = fc['taf_base'].apply(lambda s, t=tok: t in s)
        sub = fc[m]
        k = len(sub)
        z = int((~sub['observed']).sum())
        h = int(sub['met_base'].apply(lambda s, t=tok: t in s).sum())
        by_token.append({
            'cond': tok, 'n': k, 'nada': z, 'acertou': h, 'outro': k - z - h,
            'pct_nada': round(100 * z / k, 1), 'pct_acertou': round(100 * h / k, 1),
            'pct_outro': round(100 * (k - z - h) / k, 1),
        })

    # matriz de confusao: so onde houve fenomeno observado
    obs = fc[fc['observed']]
    conf = {'cols': [], 'rows': []}
    if len(obs):
        mt = Counter(t for s in obs['met_base'] for t in s)
        cols = [t for t, _ in mt.most_common(8)]
        tt = Counter(t for s in obs['taf_base'] for t in s)
        conf['cols'] = cols
        for tok, _ in tt.most_common(10):
            m = obs['taf_base'].apply(lambda s, t=tok: t in s)
            sub = obs[m]
            k = len(sub)
            cells = [round(100 * int(sub['met_base'].apply(lambda s, c=c: c in s).sum()) / k, 0)
                     for c in cols]
            hit = int(sub['met_base'].apply(lambda s, t=tok: t in s).sum())
            conf['rows'].append({'cond': tok, 'n': k, 'cells': cells,
                                 'hit_pct': round(100 * hit / k, 1)})
    return {'n': n, 'universe': 'observado' if only_observed else 'previsto',
            'nada': nada, 'outro': outro, 'base': base, 'exato': exato,
            'pct_nada': round(100 * nada / n, 1), 'pct_outro': round(100 * outro / n, 1),
            'pct_base': round(100 * base / n, 1), 'pct_exato': round(100 * exato / n, 1),
            'by_token': by_token, 'confusion': conf}


def _group_matrix(d):
    """Metricas por grupo (NavBrasil / CIMAER) x leitura (sem/com intensidade).
    Ignora o filtro de Grupo da barra lateral; respeita todos os demais filtros."""
    out = []
    for g in ('NavBrasil', 'CIMAER'):
        sub = d[d['group'] == g]
        out.append({
            'group': g,
            'n': int(len(sub)),
            'airports': int(sub['icao'].nunique()),
            'presence': _presence(sub) if len(sub) else None,
            'base': _any_dangerous(sub, 'base') if len(sub) else None,
            'exato': _any_dangerous(sub, 'exato') if len(sub) else None,
        })
    return out


# ----------------------------------------------------------------------
# rotas
# ----------------------------------------------------------------------

@app.get('/api/meta')
def api_meta():
    ap = []
    for icao, g in DF.groupby('icao'):
        ap.append({'icao': icao, 'group': g['group'].iat[0], 'n': int(len(g))})
    ap.sort(key=lambda r: r['n'], reverse=True)
    return jsonify({
        'airports': ap,
        'seasons': list(M.SEASONS),
        'periods': list(M.PERIODS),
        'months': sorted(int(x) for x in DF['month'].unique()),
        'equipment': sorted(DF['equipment'].unique().tolist()),
        'conditions': {'exato': list(M.DANGEROUS_CONDITIONS), 'base': list(M.DANGEROUS_BASE)},
        'total': int(len(DF)),
    })


@app.get('/api/stats')
def api_stats():
    view = request.args.get('view', 'base')
    if view not in VIEWS:
        view = 'base'
    d = _filtered()
    focus = request.args.get('focus', '').strip()
    per_cond = _per_condition(d, view)
    conds_here = {r['cond'] for r in per_cond}
    if focus not in conds_here:
        focus = per_cond[0]['cond'] if per_cond else ''
    return jsonify({
        'view': view,
        'filtered_n': int(len(d)),
        'presence': _presence(d),
        'any_dangerous': _any_dangerous(d, view),
        'per_condition': per_cond,
        'per_airport': _per_airport(d),
        'breakdown': _breakdown(d, view, focus) if focus else {'focus': '', 'n': 0, 'rows': []},
        'forecast': _forecast_analysis(d, request.args.get('obs') == '1'),
        'by_group': _group_matrix(_filtered(apply_group=False)),
    })


@app.get('/')
def index():
    """A pagina vive noutro repositorio (taf_compare_web). Aqui so a API."""
    return jsonify({
        'service': 'taf-compare-api',
        'rows': int(len(DF)) if DF is not None else None,
        'endpoints': ['/api/meta', '/api/stats'],
    })




def main_cli():
    ap = argparse.ArgumentParser(description="Frontend TAF REDEMET x METAR")
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8000)
    ap.add_argument('--debug', action='store_true')
    args = ap.parse_args()
    global DF
    print("carregando", M.MALHA_CSV, "...")
    DF = load()
    print(f"pronto: {len(DF):,} voos comparaveis, {DF['icao'].nunique()} aerodromos")
    print(f"http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == '__main__':
    main_cli()
