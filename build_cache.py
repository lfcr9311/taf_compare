#!/usr/bin/env python3
"""
Gera data/malha.parquet a partir de excel/malha.csv.

O painel (app.py) le esse parquet em vez do CSV: ~2 MB em disco contra 22 MB,
e o load cai de segundos para milissegundos. Rode sempre que o malha.csv mudar:

    python build_cache.py
"""
import os

import numpy as np
import pandas as pd

import core as C

# So o essencial vai para o disco. As colunas de conjunto (taf_tok, taf_base,
# taf_dng...) sao derivadas no load por lookup nas categorias - guardar elas
# aqui multiplicaria o arquivo sem ganho nenhum.
COLUMNS = ['icao', 'arr', 'taf', 'met', 'equipment', 'season', 'period',
           'month', 'group', 'forecast', 'observed', 'ex_hit', 'bs_hit']


def build():
    d = pd.read_csv(C.MALHA_CSV, sep=';', low_memory=False)
    d['icao'] = d['Alternado'].astype(str).str.strip().str.upper()
    d['arr'] = pd.to_datetime(d['Hora Chegada Alternado'], errors='coerce')
    d = d[d['arr'].notna() & d['TAF REDEMET'].notna() & d['METAR'].notna()].copy()

    taf = d['TAF REDEMET'].astype(str).str.strip()
    met = d['METAR'].astype(str).str.strip()

    out = pd.DataFrame({
        'icao':      d['icao'].astype('category'),
        'arr':       d['arr'],
        'taf':       taf.astype('category'),
        'met':       met.astype('category'),
        'equipment': d['EquipmentModel'].astype(str).str.strip().astype('category'),
        'season':    d['arr'].apply(C.season_of).astype('category'),
        'period':    d['arr'].apply(C.period_of).astype('category'),
        'month':     d['arr'].dt.month.astype('int8'),
    })
    out['group'] = pd.Series(
        np.where(d['icao'].isin(C.GROUP_A_SET), 'NavBrasil', 'CIMAER'),
        index=out.index).astype('category')
    out['forecast'] = taf.ne('NSW').values
    out['observed'] = met.ne('NSW').values

    # ex_hit / bs_hit dependem do par (taf, met), entao nao dao para derivar
    # de uma categoria sozinha - ficam gravados.
    tt = [frozenset(v.split()) for v in taf]
    mt = [frozenset(v.split()) for v in met]
    out['ex_hit'] = [bool(a & b) for a, b in zip(tt, mt)]
    tb = [frozenset(C.strip_intensity(x) for x in s) for s in tt]
    mb = [frozenset(C.strip_intensity(x) for x in s) for s in mt]
    out['bs_hit'] = [bool(a & b) for a, b in zip(tb, mb)]

    os.makedirs(os.path.dirname(C.MALHA_PARQUET), exist_ok=True)
    out[COLUMNS].to_parquet(C.MALHA_PARQUET, compression='zstd', index=False)
    return out[COLUMNS]


if __name__ == '__main__':
    df = build()
    csv_mb = os.path.getsize(C.MALHA_CSV) / 1024**2
    pq_mb = os.path.getsize(C.MALHA_PARQUET) / 1024**2
    print(f"{C.MALHA_PARQUET}")
    print(f"  {len(df):,} voos, {len(df.columns)} colunas")
    print(f"  csv {csv_mb:.1f} MB -> parquet {pq_mb:.2f} MB")
