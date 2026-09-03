#!/usr/bin/env python3
"""
Definicoes compartilhadas por main.py (pipeline/relatorios) e app.py (web).

Sem psycopg2, sem reportlab, sem dotenv: o bundle serverless do painel importa
so este modulo, entao nao carrega as dependencias de banco e de PDF.
"""
import os

_ROOT = os.path.dirname(os.path.abspath(__file__))

# Caminhos absolutos: o cwd nao e garantido em ambiente serverless.
MALHA_CSV = os.path.join(_ROOT, "excel", "malha.csv")
MALHA_PARQUET = os.path.join(_ROOT, "data", "malha.parquet")


# ----------------------------------------------------------------------
# Estacao / periodo
#
# Estacoes astronomicas, hemisferio sul, limites fixos na media do periodo
# (2016-2026); deriva interanual de +/- 1 dia nao modelada.
#   Verao 21 Dez-19 Mar | Outono 20 Mar-20 Jun
#   Inverno 21 Jun-21 Set | Primavera 22 Set-20 Dez
# Periodos: 4 faixas de 6 h em UTC.
# ----------------------------------------------------------------------

SEASONS = ('Verao', 'Outono', 'Inverno', 'Primavera')
PERIODS = ('00-06', '06-12', '12-18', '18-24')


def season_of(dt):
    md = dt.month * 100 + dt.day
    if md >= 1221 or md <= 319:
        return 'Verao'
    if md <= 620:
        return 'Outono'
    if md <= 921:
        return 'Inverno'
    return 'Primavera'


def period_of(dt):
    h = dt.hour
    return '00-06' if h < 6 else '06-12' if h < 12 else '12-18' if h < 18 else '18-24'


# ----------------------------------------------------------------------
# Grupos de aerodromos
# ----------------------------------------------------------------------

# Lista custom. full_report emite um PDF para estes ICAOs e outro para o resto.
# 43 ICAOs unicos (SBPP estava duplicado na origem).
GROUP_A = [
    'SBAR', 'SBAF', 'SBBG', 'SBBH', 'SBAE', 'SBBU', 'SBBW', 'SBCJ', 'SBCP', 'SBCZ',
    'SBPP', 'SBGO', 'SBGR', 'SBHT', 'SBIT', 'SBIL', 'SBIZ', 'SBJP', 'SBJR', 'SBJZ',
    'SBJV', 'SBKG', 'SBKP', 'SBLO', 'SBMA', 'SBMC', 'SBMK', 'SBMQ', 'SBMS', 'SBNF',
    'SBPB', 'SBPJ', 'SBPK', 'SBPL', 'SBRJ', 'SBRP', 'SBSN', 'SBTE', 'SBTF', 'SBUF',
    'SBUL', 'SBUR', 'SBVT',
]
GROUP_A_SET = set(GROUP_A)


# ----------------------------------------------------------------------
# Condicoes rastreadas
# ----------------------------------------------------------------------

DANGEROUS_CONDITIONS = [
    '-FZDZ', 'FZDZ', '+FZDZ', '-FZRA', 'FZRA', '+FZRA', 'FZFG',
    'TS', '+TS', '+RA', '+SHRA', '+SN', '+SHSN', '+TSRA', 'TSRA',
]

# A lista acima e intencionalmente assimetrica quanto a intensidade:
#   FZDZ / FZRA / FZFG / TS / TSRA aparecem sem o '+'  -> qualquer intensidade conta
#   +RA / +SHRA / +SN / +SHSN aparecem SO na forma forte -> exige o '+'
# Colapsar tudo com lstrip('+-') transformava '+RA' em 'RA' e arrastava chuva
# fraca e moderada para dentro do escopo. As listas abaixo preservam a regra.
_DNG_BY_BASE = {}
for _c in DANGEROUS_CONDITIONS:
    _DNG_BY_BASE.setdefault(_c.lstrip('+-'), set()).add(_c)

DANGEROUS_ANY_INT = sorted(b for b, t in _DNG_BY_BASE.items() if t != {'+' + b})
DANGEROUS_EXACT = sorted(t for b, ts in _DNG_BY_BASE.items() if ts == {'+' + b} for t in ts)
DANGEROUS_BASE = DANGEROUS_ANY_INT + DANGEROUS_EXACT


def strip_intensity(tok):
    """'-TSRA' -> 'TSRA'. Prefixo 'VC' preservado (VCSH, VCTS)."""
    return tok.lstrip('+-')


def dangerous_hits(tokens):
    """Rotulos de DANGEROUS_BASE satisfeitos por este conjunto de tokens.
    'RA' nao casa (a lista so tem '+RA'); '-TSRA' casa como 'TSRA'."""
    out = set()
    for t in tokens:
        if t in DANGEROUS_EXACT:
            out.add(t)
        b = strip_intensity(t)
        if b in DANGEROUS_ANY_INT:
            out.add(b)
    return out
