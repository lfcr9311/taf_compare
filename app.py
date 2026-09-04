#!/usr/bin/env python3
"""
Frontend: TAF REDEMET vs METAR, pesquisa por aerodromo + todos os filtros.

    python app.py [--host 0.0.0.0] [--port 8000] [--debug]   # local
    api/index.py                                             # serverless (Vercel)

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
    return PAGE


PAGE = r"""<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TAF REDEMET x METAR</title>
<style>
  :root {
    --bg: #0c0e13; --panel: #141821; --panel-2: #1a1f2b; --input: #1e2431;
    --border: #2a3040; --border-soft: #212734;
    --text: #e8eaf0; --muted: #8b93a5; --faint: #5c6479;
    --accent: #4c8dff; --accent-dim: #23324f;
    --good: #35c07a; --warn: #e5a94b; --bad: #ec6a5e;
    --tp: #2f6b48; --fp: #7a4a2c; --fn: #6a3340; --tn: #232a38;
    --radius: 10px; --radius-sm: 7px;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 14px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  button, input { font: inherit; color: inherit; }
  ::-webkit-scrollbar { width: 10px; height: 10px; }
  ::-webkit-scrollbar-thumb { background: #2b3242; border-radius: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }

  /* progress bar */
  #prog { position: fixed; top: 0; left: 0; height: 2px; width: 0; background: var(--accent);
          box-shadow: 0 0 8px var(--accent); transition: width .25s ease, opacity .3s; z-index: 100; opacity: 0; }
  #prog.on { opacity: 1; }

  header {
    display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
    padding: 12px 20px; border-bottom: 1px solid var(--border-soft);
    background: linear-gradient(180deg, #141821, #10131b); position: sticky; top: 0; z-index: 20;
  }
  header .brand { display: flex; align-items: baseline; gap: 10px; }
  header h1 { margin: 0; font-size: 15px; font-weight: 700; letter-spacing: .2px; }
  header .count { font-size: 12px; color: var(--muted); }
  header .count b { color: var(--text); font-variant-numeric: tabular-nums; }
  .active-pills { display: flex; gap: 6px; flex-wrap: wrap; flex: 1; }
  .pill {
    display: inline-flex; align-items: center; gap: 6px; font-size: 12px;
    background: var(--accent-dim); color: #cfe0ff; border: 1px solid #33507e;
    padding: 3px 8px; border-radius: 999px; cursor: pointer; white-space: nowrap;
  }
  .pill:hover { background: #2c4a7a; }
  .pill .x { opacity: .6; font-weight: 700; }
  .pill:hover .x { opacity: 1; }
  .clear-all { font-size: 12px; color: var(--muted); background: none; border: 1px solid var(--border);
               border-radius: 999px; padding: 3px 10px; cursor: pointer; }
  .clear-all:hover { color: var(--text); border-color: var(--faint); }
  .menu-btn { display: none; background: var(--input); border: 1px solid var(--border);
              border-radius: var(--radius-sm); padding: 6px 10px; cursor: pointer; }

  .layout { display: grid; grid-template-columns: 288px minmax(0, 1fr); align-items: start; max-width: 1560px; }

  aside {
    position: sticky; top: 53px; align-self: start;
    height: calc(100vh - 53px); overflow-y: auto;
    border-right: 1px solid var(--border-soft); background: var(--panel);
    padding: 16px 16px 40px;
  }
  .fgroup { margin-bottom: 20px; }
  .fgroup > .flabel {
    display: flex; align-items: center; justify-content: space-between;
    font-size: 11px; letter-spacing: .08em; text-transform: uppercase; color: var(--muted);
    margin-bottom: 8px;
  }
  .flabel .badge { background: var(--accent); color: #fff; border-radius: 999px; font-size: 10px;
                   padding: 1px 6px; letter-spacing: 0; }
  .flabel .mini { background: none; border: none; color: var(--faint); cursor: pointer; font-size: 11px;
                  text-transform: none; letter-spacing: 0; }
  .flabel .mini:hover { color: var(--text); }

  .search { position: relative; margin-bottom: 8px; }
  .search input { width: 100%; background: var(--input); border: 1px solid var(--border);
                  border-radius: var(--radius-sm); padding: 7px 9px 7px 30px; }
  .search input:focus { outline: none; border-color: var(--accent); }
  .search svg { position: absolute; left: 9px; top: 50%; transform: translateY(-50%); opacity: .5; }

  .checklist { border: 1px solid var(--border-soft); border-radius: var(--radius-sm);
               background: var(--panel-2); max-height: 236px; overflow-y: auto; }
  .checkrow {
    display: flex; align-items: center; gap: 8px; padding: 6px 10px; cursor: pointer;
    border-bottom: 1px solid var(--border-soft); font-size: 13px;
  }
  .checkrow:last-child { border-bottom: none; }
  .checkrow:hover { background: #202634; }
  .checkrow.on { background: #1b2740; }
  .checkrow input { accent-color: var(--accent); width: 15px; height: 15px; }
  .checkrow .ci { flex: 1; display: flex; justify-content: space-between; gap: 8px; }
  .checkrow .ci .meta { color: var(--faint); font-variant-numeric: tabular-nums; }
  .checkrow.pinned { background: #17203a; }

  .chips { display: flex; flex-wrap: wrap; gap: 6px; }
  .chip {
    border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 5px 10px;
    cursor: pointer; user-select: none; font-size: 12px; background: var(--panel-2); color: var(--muted);
    transition: background .12s, color .12s, border-color .12s;
  }
  .chip:hover { color: var(--text); border-color: var(--faint); }
  .chip.on { background: var(--accent); border-color: var(--accent); color: #fff; }
  .segmented { display: inline-flex; border: 1px solid var(--border); border-radius: var(--radius-sm);
               overflow: hidden; }
  .segmented .chip { border: none; border-radius: 0; }
  .segmented .chip + .chip { border-left: 1px solid var(--border); }
  .viewnote { margin-top: 7px; font-size: 11.5px; line-height: 1.4; color: var(--faint); }
  .viewnote.warn { color: #e5a94b; }
  .funnel { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 10px; }
  .fstep { background: var(--panel); border: 1px solid var(--border-soft); border-radius: var(--radius);
           padding: 13px 15px; position: relative; overflow: hidden; }
  .fstep .fill { position: absolute; left: 0; top: 0; bottom: 0; opacity: .16; }
  .fstep > * { position: relative; }
  .fstep .k { font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); }
  .fstep .v { font-size: 26px; font-weight: 700; margin-top: 4px; font-variant-numeric: tabular-nums; }
  .fstep .n { font-size: 11.5px; color: var(--faint); margin-top: 3px; font-variant-numeric: tabular-nums; }
  .f-bad .fill { background: #ec6a5e; }  .f-bad .v { color: #ec6a5e; }
  .f-mid .fill { background: #e5a94b; }  .f-mid .v { color: #e5a94b; }
  .f-ok  .fill { background: #35c07a; }  .f-ok  .v { color: #35c07a; }
  .cmx td.pc { position: relative; }
  .cmx td.pc i { position: absolute; inset: 2px auto 2px 0; background: rgba(76,141,255,.45); border-radius: 2px; }
  .cmx td.pc span { position: relative; }
  .cmx td.diag { background: rgba(53,192,122,.16); font-weight: 700; }
  .toggle2 { display: inline-flex; gap: 0; border: 1px solid var(--border); border-radius: var(--radius-sm);
             overflow: hidden; margin-bottom: 14px; }
  .toggle2 .chip { border: none; border-radius: 0; }
  .toggle2 .chip + .chip { border-left: 1px solid var(--border); }
  .ghead { display: flex; align-items: baseline; gap: 10px; margin: 18px 0 9px; }
  .ghead .gname { font-size: 15px; font-weight: 700; letter-spacing: .01em; }
  .ghead .gmeta { font-size: 11.5px; color: var(--faint); font-variant-numeric: tabular-nums; }
  .ghead .tag { font-size: 10px; text-transform: uppercase; letter-spacing: .07em; padding: 2px 7px;
                border: 1px solid var(--border); border-radius: 999px; color: var(--muted); }
  .dual { display: flex; align-items: stretch; gap: 12px; margin-top: 7px; }
  .dual .side { flex: 1 1 0; min-width: 0; }
  .dual .sep { width: 1px; background: var(--border); flex: 0 0 1px; }
  .dual .num { font-size: 21px; font-weight: 700; font-variant-numeric: tabular-nums; line-height: 1.2; }
  .dual .lab { font-size: 9.5px; text-transform: uppercase; letter-spacing: .06em;
               color: var(--faint); margin-top: 3px; }
  .dual .pri .num { color: var(--text); }
  .dual .sec .num { color: var(--muted); font-weight: 600; }
  .dual .sub { font-size: 10.5px; color: var(--faint); margin-top: 2px; font-variant-numeric: tabular-nums; }
  .scopenote { font-size: 11.5px; color: var(--faint); margin-bottom: 4px; }

  main { padding: 22px 28px 80px; min-width: 0; }
  .sec { margin-bottom: 34px; }
  .sec > h2 {
    font-size: 12px; text-transform: uppercase; letter-spacing: .09em; color: var(--muted);
    margin: 0 0 12px; display: flex; align-items: center; gap: 10px;
  }
  .sec > h2 .hint { text-transform: none; letter-spacing: 0; color: var(--faint); font-weight: 400; font-size: 12px; }

  .grid { display: grid; gap: 12px; }
  .cols-3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .cols-auto { grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); }

  .card { background: var(--panel); border: 1px solid var(--border-soft); border-radius: var(--radius);
          padding: 14px 16px; }
  .card .k { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }
  .card .v { font-size: 24px; font-weight: 700; margin-top: 5px; font-variant-numeric: tabular-nums; }
  .card .v small { font-size: 12px; color: var(--muted); font-weight: 400; }
  .card .bar { margin-top: 10px; height: 5px; border-radius: 3px; background: #232a38; overflow: hidden; }
  .card .bar > i { display: block; height: 100%; background: var(--accent); border-radius: 3px; }
  .card .sub2 { margin-top: 7px; font-size: 11.5px; color: var(--faint); }

  /* confusion matrix */
  .cmwrap { display: flex; flex-wrap: wrap; gap: 18px; align-items: stretch; }
  .cm { flex: 0 0 372px; max-width: 100%; display: grid;
        grid-template-columns: 74px 1fr 1fr; grid-template-rows: 26px 62px 62px; gap: 5px; }
  .cm .h { display: flex; align-items: center; justify-content: center; font-size: 10px;
           color: var(--muted); text-transform: uppercase; letter-spacing: .04em; text-align: center; }
  .cm .rh { writing-mode: vertical-rl; transform: rotate(180deg); }
  .cm .cell { min-width: 0; overflow: hidden; border-radius: var(--radius-sm); padding: 6px;
              display: flex; flex-direction: column; align-items: center; justify-content: center;
              gap: 2px; border: 1px solid var(--border-soft); }
  .cm .cell .n { font-size: 17px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .cm .cell .l { font-size: 9.5px; color: #cdd3e0; text-transform: uppercase; letter-spacing: .03em; white-space: nowrap; }
  .cm .tp { background: var(--tp); } .cm .fp { background: var(--fp); }
  .cm .fn { background: var(--fn); } .cm .tn { background: var(--tn); }
  .rates { flex: 1 1 340px; min-width: 0; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
  .rate { background: var(--panel); border: 1px solid var(--border-soft); border-radius: var(--radius); padding: 12px 14px; }
  .rate .k { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }
  .rate .v { font-size: 22px; font-weight: 700; margin-top: 4px; font-variant-numeric: tabular-nums; }
  .rate .bar { margin-top: 8px; height: 5px; border-radius: 3px; background: #232a38; overflow: hidden; }
  .rate .bar > i { display: block; height: 100%; border-radius: 3px; }
  .rate .frac { font-size: 11px; color: var(--faint); margin-top: 6px; font-variant-numeric: tabular-nums; }
  .rate.tp-bar .bar > i { background: var(--good); }
  .rate.det .bar > i { background: var(--accent); }
  .rate.prec .bar > i { background: var(--warn); }

  .tablewrap { border: 1px solid var(--border-soft); border-radius: var(--radius); overflow: hidden; background: var(--panel); }
  .tablescroll { max-height: 460px; overflow: auto; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { padding: 8px 12px; text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
  th:first-child, td:first-child { text-align: left; }
  th.l, td.l { text-align: left; }
  thead th {
    position: sticky; top: 0; z-index: 1; background: #10131b; color: var(--muted);
    font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: .05em;
    cursor: pointer; user-select: none; border-bottom: 1px solid var(--border);
  }
  thead th:hover { color: var(--text); }
  thead th.sorted { color: var(--accent); }
  thead th.sorted::after { content: " \25BC"; font-size: 8px; }
  thead th.sorted.asc::after { content: " \25B2"; }
  tbody tr { border-bottom: 1px solid var(--border-soft); }
  tbody tr:last-child { border-bottom: none; }
  tbody tr.clickable { cursor: pointer; }
  tbody tr.clickable:hover { background: #1b202c; }
  tbody tr.sel { background: #1c2b49; }
  tbody tr.sel:hover { background: #223152; }
  td.barcell { position: relative; }
  td.barcell .bg { position: absolute; left: 0; top: 0; bottom: 0; background: rgba(76, 141, 255, .13);
                   border-right: 1px solid rgba(76, 141, 255, .4); z-index: 0; }
  td.barcell > span { position: relative; z-index: 1; }
  .empty { text-align: center; color: var(--faint); padding: 24px; }
  .pos { color: var(--good); } .neg { color: var(--bad); } .mut { color: var(--faint); }

  @media (max-width: 900px) {
    .layout { grid-template-columns: 1fr; }
    aside { position: fixed; left: 0; top: 53px; width: 300px; z-index: 30; transform: translateX(-105%);
            transition: transform .2s ease; box-shadow: 0 0 40px #0008; height: calc(100vh - 53px); }
    body.nav-open aside { transform: none; }
    .menu-btn { display: block; }
    .cols-3 { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<div id="prog"></div>
<header>
  <button class="menu-btn" id="menu" aria-label="filtros">&#9776;</button>
  <div class="brand">
    <h1>TAF REDEMET &times; METAR</h1>
    <span class="count" id="count">&hellip;</span>
  </div>
  <div class="active-pills" id="pills"></div>
  <button class="clear-all" id="clearAll" hidden>limpar tudo</button>
</header>

<div class="layout">
  <aside id="aside">
    <div class="fgroup">
      <div class="flabel">Aerodromo <span class="badge" id="b-icao" hidden></span></div>
      <div class="search">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>
        <input type="text" id="q" placeholder="buscar ICAO&hellip;  (tecla /)" autocomplete="off">
      </div>
      <div class="checklist" id="icaoList"></div>
    </div>

    <div class="fgroup">
      <div class="flabel">Grupo</div>
      <div class="segmented" id="group">
        <span class="chip on" data-v="">Todos</span>
        <span class="chip" data-v="NavBrasil">NavBrasil</span>
        <span class="chip" data-v="CIMAER">CIMAER</span>
      </div>
    </div>

    <div class="fgroup">
      <div class="flabel">Leitura <button class="mini" data-help="exato">?</button></div>
      <div class="segmented" id="view">
        <span class="chip on" data-v="base">Sem intensidade</span>
        <span class="chip" data-v="exato">Exato</span>
      </div>
      <div class="viewnote" id="viewnote"></div>
    </div>

    <div class="fgroup">
      <div class="flabel">Estacao <span class="badge" id="b-season" hidden></span></div>
      <div class="chips" id="season"></div>
    </div>

    <div class="fgroup">
      <div class="flabel">Periodo UTC <span class="badge" id="b-period" hidden></span></div>
      <div class="chips" id="period"></div>
    </div>

    <div class="fgroup">
      <div class="flabel">Mes UTC <span class="badge" id="b-month" hidden></span></div>
      <div class="chips" id="month"></div>
    </div>

    <div class="fgroup">
      <div class="flabel">Equipamento <span class="badge" id="b-equipment" hidden></span></div>
      <div class="checklist" id="equipList"></div>
    </div>
  </aside>

  <main>
    <div class="sec">
      <h2>Presenca de fenomeno <span class="hint">TAF vs METAR concordam que ha (ou nao ha) fenomeno</span></h2>
      <div class="cmwrap">
        <div class="cm" id="cmPresence"></div>
        <div class="rates">
          <div class="rate tp-bar"><div class="k">Acerto</div><div class="v" id="p-acc">-</div>
            <div class="bar"><i id="p-acc-b"></i></div><div class="frac" id="p-acc-f"></div></div>
          <div class="rate det"><div class="k">Deteccao</div><div class="v" id="p-det">-</div>
            <div class="bar"><i id="p-det-b"></i></div><div class="frac" id="p-det-f"></div></div>
          <div class="rate prec"><div class="k">Precisao</div><div class="v" id="p-prec">-</div>
            <div class="bar"><i id="p-prec-b"></i></div><div class="frac" id="p-prec-f"></div></div>
        </div>
      </div>
    </div>

    <div class="sec">
      <h2>Condicoes consideradas <span class="hint" id="anyhint"></span></h2>
      <div class="grid cols-auto" id="anyCards"></div>
    </div>

    <div class="sec">
      <h2>Por grupo de aerodromos <span class="hint">NavBrasil x CIMAER, nas duas leituras</span></h2>
      <div class="scopenote" id="gnote"></div>
      <div id="byGroup"></div>
    </div>

    <div class="sec">
      <h2>Por condicao <span class="hint">clique numa linha para ver o detalhamento</span></h2>
      <div class="tablewrap"><div class="tablescroll">
        <table id="condTbl"><thead></thead><tbody></tbody></table>
      </div></div>
    </div>

    <div class="sec">
      <h2>Quando o TAF previu <span class="hint" id="focuslbl"></span>, o METAR mostrou</h2>
      <div class="tablewrap"><div class="tablescroll">
        <table id="brkTbl"><thead></thead><tbody></tbody></table>
      </div></div>
    </div>

    <div class="sec">
      <h2>Quando o TAF do alternado previu fenomeno <span class="hint" id="fchint"></span></h2>
      <div class="toggle2" id="obs">
        <span class="chip on" data-v="0">Tudo que foi previsto</span>
        <span class="chip" data-v="1">So onde ocorreu fenomeno</span>
      </div>
      <div class="funnel" id="funnel"></div>
      <h2 style="margin-top:26px">Por fenomeno previsto</h2>
      <div class="tablewrap"><div class="tablescroll">
        <table id="fcTbl"><thead></thead><tbody></tbody></table>
      </div></div>
      <h2 style="margin-top:26px">Matriz de confusao <span class="hint">linha = TAF previu &middot; coluna = METAR observou &middot; so voos com fenomeno observado</span></h2>
      <div class="tablewrap"><div class="tablescroll">
        <table id="cmxTbl" class="cmx"><thead></thead><tbody></tbody></table>
      </div></div>
    </div>

    <div class="sec">
      <h2>Por aerodromo <span class="hint" id="airn"></span> <span class="hint">clique para filtrar</span></h2>
      <div class="tablewrap"><div class="tablescroll">
        <table id="airTbl"><thead></thead><tbody></tbody></table>
      </div></div>
    </div>
  </main>
</div>

<script>
const HELP = {
  exato: "SEM INTENSIDADE (recomendada): ignora os prefixos + e - , entao TSRA = -TSRA = +TSRA.\n\n"
    + "EXATO: exige token identico, entao TAF 'TSRA' contra METAR '-TSRA' conta como erro.\n\n"
    + "Por que a exato distorce: o TAF omite a intensidade em 93,9% dos casos, enquanto o METAR "
    + "escreve '-' (fraco) em 42,5%. A visao exata acaba medindo diferenca de convencao de escrita, "
    + "nao erro de previsao. Em TSRA a precisao cai de 7,5% para 1,9% so por causa disso, e 1.802 "
    + "trovoadas reais registradas como -TSRA nem entram na contagem.",
};
const MN = ['','jan','fev','mar','abr','mai','jun','jul','ago','set','out','nov','dez'];
const F = { icao: [], group: '', season: [], period: [], month: [], equipment: [], view: 'base', focus: '', obs: '0' };
const SORT = { cond: { key: 'taf_forecast', dir: -1 }, air: { key: 'n', dir: -1 },
              fc: { key: 'n', dir: -1 } };
let META = null, LAST = null, ctrl = null;

const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const fmt = n => (n == null ? '–' : Number(n).toLocaleString('pt-BR'));
const pct = n => (n == null ? '–' : n.toFixed(1) + '%');
const clamp = n => Math.max(0, Math.min(100, n || 0));

/* ---------- filters state <-> URL ---------- */
function toHash() {
  const p = new URLSearchParams();
  for (const k of ['icao', 'season', 'period', 'month', 'equipment'])
    if (F[k].length) p.set(k, F[k].join(','));
  if (F.group) p.set('group', F.group);
  if (F.view !== 'base') p.set('view', F.view);
  if (F.focus) p.set('focus', F.focus);
  if (F.obs === '1') p.set('obs', '1');
  history.replaceState(null, '', '#' + p.toString());
}
function fromHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  for (const k of ['icao', 'season', 'period', 'month', 'equipment'])
    F[k] = (p.get(k) || '').split(',').filter(Boolean);
  F.group = p.get('group') || '';
  F.view = p.get('view') || 'base';
  F.focus = p.get('focus') || '';
  F.obs = p.get('obs') || '0';
}

/* ---------- builders ---------- */
function chipRow(el, values, key, label) {
  el.innerHTML = '';
  values.forEach(v => {
    const c = document.createElement('span');
    c.className = 'chip' + (F[key].includes(String(v)) ? ' on' : '');
    c.textContent = label ? label(v) : v;
    c.onclick = () => {
      const arr = F[key], i = arr.indexOf(String(v));
      i >= 0 ? arr.splice(i, 1) : arr.push(String(v));
      c.classList.toggle('on');
      refresh();
    };
    el.appendChild(c);
  });
}
function segmented(el, key, after) {
  $$('#' + el.id + ' .chip').forEach(c => {
    c.classList.toggle('on', c.dataset.v === F[key]);
    c.onclick = () => {
      F[key] = c.dataset.v;
      $$('#' + el.id + ' .chip').forEach(x => x.classList.toggle('on', x === c));
      if (after) after();
      refresh();
    };
  });
}
function renderIcaoList() {
  const q = $('#q').value.trim().toUpperCase();
  const rows = META.airports
    .map(a => ({ ...a, on: F.icao.includes(a.icao) }))
    .filter(a => a.on || !q || a.icao.includes(q))
    .sort((a, b) => (b.on - a.on) || (b.n - a.n));
  $('#icaoList').innerHTML = rows.map(a => `
    <label class="checkrow ${a.on ? 'on' : ''} ${a.on && q ? 'pinned' : ''}">
      <input type="checkbox" ${a.on ? 'checked' : ''} data-i="${a.icao}">
      <span class="ci"><span>${a.icao}</span><span class="meta">${a.group[0]} · ${fmt(a.n)}</span></span>
    </label>`).join('') || '<div class="empty">nada encontrado</div>';
  $$('#icaoList input').forEach(inp => inp.onchange = () => {
    const i = F.icao.indexOf(inp.dataset.i);
    inp.checked ? (i < 0 && F.icao.push(inp.dataset.i)) : (i >= 0 && F.icao.splice(i, 1));
    renderIcaoList(); refresh();
  });
}
function renderEquipList() {
  $('#equipList').innerHTML = META.equipment.map(e => `
    <label class="checkrow ${F.equipment.includes(e) ? 'on' : ''}">
      <input type="checkbox" ${F.equipment.includes(e) ? 'checked' : ''} data-e="${e}">
      <span class="ci"><span>${e}</span></span>
    </label>`).join('');
  $$('#equipList input').forEach(inp => inp.onchange = () => {
    const i = F.equipment.indexOf(inp.dataset.e);
    inp.checked ? (i < 0 && F.equipment.push(inp.dataset.e)) : (i >= 0 && F.equipment.splice(i, 1));
    inp.closest('.checkrow').classList.toggle('on', inp.checked);
    refresh();
  });
}

/* ---------- active pills + badges ---------- */
function renderPills() {
  const items = [];
  F.icao.forEach(v => items.push(['icao', v, v]));
  if (F.group) items.push(['group', '', 'Grupo: ' + F.group]);
  F.season.forEach(v => items.push(['season', v, v]));
  F.period.forEach(v => items.push(['period', v, v + ' UTC']));
  F.month.forEach(v => items.push(['month', v, MN[v]]));
  F.equipment.forEach(v => items.push(['equipment', v, v]));
  $('#pills').innerHTML = items.map(([k, v, lbl]) =>
    `<span class="pill" data-k="${k}" data-v="${v}">${lbl}<span class="x">×</span></span>`).join('');
  $$('#pills .pill').forEach(p => p.onclick = () => {
    const k = p.dataset.k, v = p.dataset.v;
    if (k === 'group') F.group = '';
    else { const i = F[k].indexOf(v); if (i >= 0) F[k].splice(i, 1); }
    syncControls(); refresh();
  });
  $('#clearAll').hidden = items.length === 0;
  for (const k of ['icao', 'season', 'period', 'month', 'equipment']) {
    const b = $('#b-' + k);
    b.hidden = !F[k].length; b.textContent = F[k].length;
  }
}
function syncControls() {
  chipRow($('#season'), META.seasons, 'season');
  chipRow($('#period'), META.periods, 'period');
  chipRow($('#month'), META.months, 'month', v => MN[v] || v);
  $$('#group .chip').forEach(c => c.classList.toggle('on', c.dataset.v === F.group));
  $$('#view .chip').forEach(c => c.classList.toggle('on', c.dataset.v === F.view));
  renderIcaoList(); renderEquipList();
}

/* ---------- fetch ---------- */
let t = null;
function refresh() { clearTimeout(t); t = setTimeout(fetchStats, 110); }
async function fetchStats() {
  toHash(); renderPills();
  const p = new URLSearchParams();
  for (const k of ['icao', 'season', 'period', 'month', 'equipment'])
    if (F[k].length) p.set(k, F[k].join(','));
  if (F.group) p.set('group', F.group);
  p.set('view', F.view);
  if (F.focus) p.set('focus', F.focus);
  if (F.obs === '1') p.set('obs', '1');
  if (ctrl) ctrl.abort();
  ctrl = new AbortController();
  const bar = $('#prog'); bar.classList.add('on'); bar.style.width = '35%';
  try {
    const r = await fetch('/api/stats?' + p, { signal: ctrl.signal });
    const s = await r.json();
    bar.style.width = '100%';
    LAST = s; render(s);
  } catch (e) { if (e.name !== 'AbortError') console.error(e); }
  finally { setTimeout(() => { bar.classList.remove('on'); bar.style.width = '0'; }, 250); }
}

/* ---------- render ---------- */
function matrix(el, tp, fp, fn, tn) {
  el.innerHTML = `
    <div class="h"></div><div class="h">METAR teve</div><div class="h">METAR nao</div>
    <div class="h rh">TAF previu</div>
    <div class="cell tp"><span class="n">${fmt(tp)}</span><span class="l">acerto</span></div>
    <div class="cell fp"><span class="n">${fmt(fp)}</span><span class="l">falso pos.</span></div>
    <div class="h rh">TAF nao</div>
    <div class="cell fn"><span class="n">${fmt(fn)}</span><span class="l">miss</span></div>
    <div class="cell tn"><span class="n">${tn == null ? '–' : fmt(tn)}</span><span class="l">ok (sem wx)</span></div>`;
}
function setRate(id, val, num, den) {
  $('#p-' + id).textContent = pct(val);
  $('#p-' + id + '-b').style.width = clamp(val) + '%';
  $('#p-' + id + '-f').textContent = (num == null ? '' : fmt(num) + ' / ' + fmt(den));
}
function card(k, v, sub) {
  return `<div class="card"><div class="k">${k}</div><div class="v">${v}</div>${sub ? `<div class="sub2">${sub}</div>` : ''}</div>`;
}
function sortRows(rows, st) {
  return rows.slice().sort((a, b) => {
    let x = a[st.key], y = b[st.key];
    x = (x == null ? -Infinity : x); y = (y == null ? -Infinity : y);
    if (typeof x === 'string') return st.dir * x.localeCompare(y);
    return st.dir * (x - y);
  });
}
function table(sel, cols, rows, st, opts = {}) {
  const el = $(sel);
  el.querySelector('thead').innerHTML = '<tr>' + cols.map(c =>
    `<th class="${c.align || ''} ${st.key === c.key ? 'sorted ' + (st.dir < 0 ? 'desc' : 'asc') : ''}" data-k="${c.key}">${c.label}</th>`
  ).join('') + '</tr>';
  const sorted = sortRows(rows, st);
  const max = opts.barKey ? Math.max(1, ...sorted.map(r => r[opts.barKey] || 0)) : 0;
  el.querySelector('tbody').innerHTML = sorted.map(r => {
    const cls = [opts.onRow ? 'clickable' : '', opts.selKey && String(r[opts.selKey]) === String(opts.selVal) ? 'sel' : ''].join(' ');
    return `<tr class="${cls}" data-key="${opts.selKey ? r[opts.selKey] : ''}">` + cols.map(c => {
      let v = c.fmt ? c.fmt(r[c.key], r) : r[c.key];
      if (c.key === opts.barKey) {
        const w = 100 * (r[c.key] || 0) / max;
        return `<td class="barcell ${c.align || ''}"><i class="bg" style="width:${w}%"></i><span>${v}</span></td>`;
      }
      return `<td class="${c.align || ''}">${v}</td>`;
    }).join('') + '</tr>';
  }).join('') || `<tr><td class="empty" colspan="${cols.length}">sem dados neste recorte</td></tr>`;
  el.querySelectorAll('th[data-k]').forEach(th => th.onclick = () => {
    if (st.key === th.dataset.k) st.dir *= -1; else { st.key = th.dataset.k; st.dir = -1; }
    if (LAST) render(LAST);
  });
  if (opts.onRow) el.querySelectorAll('tr[data-key]').forEach(tr => tr.onclick = () => opts.onRow(tr.dataset.key));
}

function render(s) {
  $('#count').innerHTML = `<b>${fmt(s.filtered_n)}</b> de ${fmt(META.total)} voos`;
  const pr = s.presence;
  matrix($('#cmPresence'), pr.tp, pr.false_positive, pr.miss, pr.tn);
  setRate('acc', pr.pct, pr.correct, pr.n);
  setRate('det', pr.detection_pct, pr.tp, pr.metar_wx);
  setRate('prec', pr.precision_pct, pr.tp, pr.taf_wx);

  const a = s.any_dangerous;
  const vlabel = s.view === 'base' ? 'sem intensidade' : 'exato';
  $('#anyhint').textContent = vlabel + '  ·  ' + (META.conditions[s.view] || []).join(' ');
  $('#anyCards').innerHTML = [
    card('Precisao', pct(a.precision_pct), 'confirmado / o que o TAF sinalizou'),
    card('TAF sinalizou', fmt(a.taf_flagged)),
    card('METAR teve', fmt(a.metar_observed)),
    card('Acerto / FP / Miss', `${fmt(a.acerto)} <small>/ ${fmt(a.false_positive)} / ${fmt(a.miss)}</small>`),
  ].join('');

  const cInt = (v) => fmt(v);
  table('#condTbl', [
    { key: 'cond', label: 'Cond', align: 'l' },
    { key: 'taf_forecast', label: 'TAF previu', fmt: cInt },
    { key: 'confirmed', label: 'Confirmado', fmt: cInt },
    { key: 'precision_pct', label: 'Precisao', fmt: pct },
    { key: 'metar_observed', label: 'METAR teve', fmt: cInt },
    { key: 'detected_pct', label: 'Deteccao', fmt: pct },
    { key: 'false_positive', label: 'Falso pos.', fmt: v => `<span class="neg">${fmt(v)}</span>` },
    { key: 'miss', label: 'Miss', fmt: v => `<span class="neg">${fmt(v)}</span>` },
  ], s.per_condition, SORT.cond, {
    barKey: 'taf_forecast', selKey: 'cond', selVal: s.breakdown.focus,
    onRow: k => { F.focus = k; refresh(); },
  });

  $('#focuslbl').textContent = s.breakdown.focus ? `${s.breakdown.focus} (${fmt(s.breakdown.n)}x)` : '–';
  table('#brkTbl', [
    { key: 'metar', label: 'METAR real', align: 'l' },
    { key: 'count', label: 'Qtd' },
    { key: 'pct', label: '%', fmt: pct },
  ], s.breakdown.rows, { key: 'count', dir: -1 }, { barKey: 'count' });

  // ---- por grupo x leitura ----
  $('#gnote').textContent = 'Ignora o filtro Grupo da barra lateral; respeita estacao, periodo, mes, '
    + 'aerodromo e equipamento. Valor em destaque = sem intensidade.';
  const dual = (k, a, b, sa, sb) => `<div class="card"><div class="k">${k}</div>
      <div class="dual">
        <div class="side pri"><div class="num">${a}</div><div class="lab">sem intensidade</div>
          ${sa ? `<div class="sub">${sa}</div>` : ''}</div>
        <div class="sep"></div>
        <div class="side sec"><div class="num">${b}</div><div class="lab">com intensidade</div>
          ${sb ? `<div class="sub">${sb}</div>` : ''}</div>
      </div></div>`;
  $('#byGroup').innerHTML = s.by_group.map(g => {
    if (!g.n) return `<div class="ghead"><span class="gname">${g.group}</span>
      <span class="gmeta">sem voos neste recorte</span></div>`;
    const b = g.base, e = g.exato, p = g.presence;
    return `<div class="ghead">
        <span class="gname">${g.group}</span>
        <span class="tag">${g.airports} aerodromos</span>
        <span class="gmeta">${fmt(g.n)} voos comparaveis &middot; presenca ${pct(p.pct)} (piso ${pct(p.base_rate)}) &middot; HSS ${p.hss}</span>
      </div>
      <div class="grid cols-auto">
        ${dual('Precisao', pct(b.precision_pct), pct(e.precision_pct))}
        ${dual('TAF sinalizou', fmt(b.taf_flagged), fmt(e.taf_flagged))}
        ${dual('METAR teve', fmt(b.metar_observed), fmt(e.metar_observed))}
        ${dual('Acerto', fmt(b.acerto), fmt(e.acerto),
               `FP ${fmt(b.false_positive)} &middot; miss ${fmt(b.miss)}`,
               `FP ${fmt(e.false_positive)} &middot; miss ${fmt(e.miss)}`)}
      </div>`;
  }).join('');

  // ---- previsao no alternado ----
  const f = s.forecast;
  $('#fchint').textContent = f.universe === 'observado'
    ? `${fmt(f.n)} voos com fenomeno previsto E observado`
    : `${fmt(f.n)} voos com fenomeno previsto pelo TAF`;
  const steps = f.universe === 'observado'
    ? [['Acertou o fenomeno', f.pct_base, f.base, 'f-ok', 'intensidade ignorada'],
       ['Token identico', f.pct_exato, f.exato, 'f-ok', 'inclui intensidade'],
       ['Veio outro fenomeno', f.pct_outro, f.outro, 'f-bad', 'ocorreu algo diferente']]
    : [['Nao veio nada', f.pct_nada, f.nada, 'f-bad', 'METAR NSW'],
       ['Veio outro fenomeno', f.pct_outro, f.outro, 'f-mid', 'ocorreu, mas diferente'],
       ['Acertou o fenomeno', f.pct_base, f.base, 'f-ok', 'intensidade ignorada'],
       ['Token identico', f.pct_exato, f.exato, 'f-ok', 'inclui intensidade']];
  $('#funnel').innerHTML = steps.map(([k, p, nn, cls, sub]) =>
    `<div class="fstep ${cls}"><i class="fill" style="width:${clamp(p)}%"></i>
      <div class="k">${k}</div><div class="v">${pct(p)}</div>
      <div class="n">${fmt(nn)} voos &middot; ${sub}</div></div>`).join('');

  table('#fcTbl', [
    { key: 'cond', label: 'Previsto', align: 'l' },
    { key: 'n', label: 'Voos', fmt: fmt },
    { key: 'nada', label: 'Nao veio nada', fmt: fmt },
    { key: 'pct_nada', label: '%', fmt: v => `<span class="neg">${pct(v)}</span>` },
    { key: 'outro', label: 'Outro fenomeno', fmt: fmt },
    { key: 'pct_outro', label: '%', fmt: pct },
    { key: 'acertou', label: 'Acertou', fmt: fmt },
    { key: 'pct_acertou', label: '%', fmt: v => `<span class="pos">${pct(v)}</span>` },
  ], f.by_token, SORT.fc, { barKey: 'n' });

  const cm = f.confusion;
  const cmEl = $('#cmxTbl');
  cmEl.querySelector('thead').innerHTML = '<tr><th class="l">TAF \\ METAR</th>'
    + cm.cols.map(c => `<th>${c}</th>`).join('') + '<th>Voos</th><th>Acerto</th></tr>';
  cmEl.querySelector('tbody').innerHTML = cm.rows.map(r =>
    `<tr><td class="l"><b>${r.cond}</b></td>`
    + r.cells.map((v, i) => `<td class="pc ${cm.cols[i] === r.cond ? 'diag' : ''}">`
        + `<i style="width:${clamp(v)}%"></i><span>${v}%</span></td>`).join('')
    + `<td>${fmt(r.n)}</td><td><span class="pos">${pct(r.hit_pct)}</span></td></tr>`).join('')
    || `<tr><td class="empty" colspan="${cm.cols.length + 3}">sem fenomeno observado neste recorte</td></tr>`;

  $('#airn').textContent = `(${s.per_airport.length})`;
  table('#airTbl', [
    { key: 'icao', label: 'ICAO', align: 'l' },
    { key: 'group', label: 'Grupo', align: 'l', fmt: v => `<span class="mut">${v}</span>` },
    { key: 'n', label: 'Comparaveis', fmt: fmt },
    { key: 'correct', label: 'Acerto', fmt: fmt },
    { key: 'pct', label: '%', fmt: pct },
    { key: 'false_positive', label: 'Falso pos.', fmt: v => `<span class="neg">${fmt(v)}</span>` },
    { key: 'miss', label: 'Miss', fmt: v => `<span class="neg">${fmt(v)}</span>` },
  ], s.per_airport, SORT.air, {
    barKey: 'n', selKey: 'icao', selVal: null,
    onRow: k => {
      const i = F.icao.indexOf(k);
      i >= 0 ? F.icao.splice(i, 1) : F.icao.push(k);
      renderIcaoList(); refresh();
    },
  });
}

/* ---------- boot ---------- */
async function boot() {
  META = await (await fetch('/api/meta')).json();
  fromHash();
  chipRow($('#season'), META.seasons, 'season');
  chipRow($('#period'), META.periods, 'period');
  chipRow($('#month'), META.months, 'month', v => MN[v] || v);
  segmented($('#group'), 'group');
  segmented($('#view'), 'view', () => { F.focus = ''; });
  segmented($('#obs'), 'obs');
  renderIcaoList(); renderEquipList();
  $('#q').oninput = renderIcaoList;
  $('#clearAll').onclick = () => {
    Object.assign(F, { icao: [], group: '', season: [], period: [], month: [], equipment: [], focus: '' });
    syncControls(); refresh();
  };
  $('#menu').onclick = () => document.body.classList.toggle('nav-open');
  $$('.mini[data-help]').forEach(b => b.onclick = () => alert(HELP[b.dataset.help]));
  document.addEventListener('keydown', e => {
    if (e.key === '/' && document.activeElement !== $('#q')) { e.preventDefault(); $('#q').focus(); }
    if (e.key === 'Escape' && document.activeElement === $('#q')) { $('#q').value = ''; renderIcaoList(); }
  });
  fetchStats();
}
boot();
</script>
</body>
</html>
"""


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
