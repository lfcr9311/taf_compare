#!/usr/bin/env python3
"""
Azul TAF x METAR - tudo num arquivo.

  python main.py pipeline [--steps a,b,...]   preenche excel/malha.csv (in place)
  python main.py reports                      gera todos os relatorios
  python main.py reports full_report          relatorio_NavBrasil.pdf + relatorio_CIMAER.pdf
  python main.py reports forecast_only        relatorio_previstos_{NavBrasil,CIMAER}.pdf
  python main.py all                         pipeline + relatorios
  python main.py list                         lista os relatorios
  python app.py [--port 8000]                frontend web (pesquisa por aerodromo + filtros)

Dois PDFs, mesmas 4 secoes cada, um por grupo de aerodromos:
  relatorio_NavBrasil.pdf   so os 43 ICAOs da lista (GROUP_A)
  relatorio_CIMAER.pdf      todos os outros aerodromos
Secoes (todas comparam TAF REDEMET vs METAR):
  1  presenca TAF REDEMET vs METAR por aeroporto x estacao x periodo
  2  fenomenos perigosos - acuracia (QUALQUER + por fenomeno, exato e sem intensidade)
  3  condicoes perigosas TAF vs METAR por estacao x periodo
  4  consolidado TAF REDEMET vs METAR (presenca + perigosas + breakdown)

Matrizes granulares -> output/{taf_accuracy,dangerous}_matrix_{NavBrasil,CIMAER}.csv
Pipeline steps: alternado, horario, metar, taf_redemet.
"""

import argparse
import logging
import os
import re
from datetime import datetime

import pandas as pd
import psycopg2

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle, Paragraph,
                                Spacer, PageBreak)
from reportlab.lib.enums import TA_CENTER

log = logging.getLogger("azul")


# ======================================================================
# CONFIG
# ======================================================================

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5666")
DB_NAME = os.getenv("DB_NAME", "metar_db")
DB_USER = os.getenv("DB_USER", "metar_user")
DB_PASS = os.getenv("DB_PASS", "metar_pass")

MALHA_CSV = "excel/malha.csv"          # semicolon-separated, edited in place
AIRPORTS_CSV = "excel/airports.csv"    # ident,iata_code,... (IATA -> ICAO)
ICAOS_TXT = "aiports.txt"              # 122 ICAOs, one per line
OUTPUT_DIR = "output"                  # CSV matrices; PDFs go to repo root

ALTERNADOS_FILES = {
    'A20N': 'excel/alternados_A20N.xlsx',
    'A21N': 'excel/alternados_A21N.xlsx',
    'AT76': 'excel/alternados_AT76.xlsx',
    'E195': 'excel/alternados_E195.xlsx',
    'E295': 'excel/alternados_E295.xlsx',
}

EQUIPMENT_TO_AIRCRAFT = {
    'A320-251NEO': 'A20N', 'A320-253NEO': 'A20N',
    'A321-251NEO': 'A21N', 'A321-231': 'A21N',
    'ATR72-600': 'AT76', 'ERJ 195LR': 'E195', 'ERJ': 'E295',
}
CRUISE_SPEEDS = {'A20N': 250, 'A21N': 250, 'AT76': 180, 'E195': 250, 'E295': 250}

DANGEROUS_CONDITIONS = [
    '-FZDZ', 'FZDZ', '+FZDZ', '-FZRA', 'FZRA', '+FZRA', 'FZFG',
    'TS', '+TS', '+RA', '+SHRA', '+SN', '+SHSN', '+TSRA', 'TSRA',
]

# Custom aerodrome group (LISTA). full_report emits one PDF for these ICAOs and
# one for all the others. 43 unique ICAOs (SBPP was listed twice).
GROUP_A = [
    'SBAR', 'SBAF', 'SBBG', 'SBBH', 'SBAE', 'SBBU', 'SBBW', 'SBCJ', 'SBCP', 'SBCZ',
    'SBPP', 'SBGO', 'SBGR', 'SBHT', 'SBIT', 'SBIL', 'SBIZ', 'SBJP', 'SBJR', 'SBJZ',
    'SBJV', 'SBKG', 'SBKP', 'SBLO', 'SBMA', 'SBMC', 'SBMK', 'SBMQ', 'SBMS', 'SBNF',
    'SBPB', 'SBPJ', 'SBPK', 'SBPL', 'SBRJ', 'SBRP', 'SBSN', 'SBTE', 'SBTF', 'SBUF',
    'SBUL', 'SBUR', 'SBVT',
]
GROUP_A_SET = set(GROUP_A)
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


# ======================================================================
# HELPERS - database
# ======================================================================

def get_conn():
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, database=DB_NAME,
                            user=DB_USER, password=DB_PASS)


# ======================================================================
# HELPERS - METAR present-weather extraction
#
# Weather group(s) sit between visibility and clouds/temp/QNH; RMK ignored;
# the DB 'condicao' column is not trusted. Scanning starts after the DDHHMMZ
# timestamp so a station id (e.g. SNBR = SN + BR) is never read as weather.
# ======================================================================

CLOUD_PREFIXES = ('FEW', 'SCT', 'BKN', 'OVC', 'VV', 'NSC', 'NCD', 'CLR', 'SKC')
_WX_TOKEN_RE = re.compile(
    r'^(?:[-+]|VC)?(?:MI|BC|PR|DR|BL|SH|TS|FZ|DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|'
    r'FU|VA|DU|SA|HZ|PO|SQ|FC|SS|DS){1,4}$'
)
_TEMP_DEWPOINT_RE = re.compile(r'^M?\d{2}/M?\d{2}$')
_QNH_RE = re.compile(r'^[QA]\d{4}$')
_TIMESTAMP_RE = re.compile(r'^\d{6}Z$')
_WIND_RE = re.compile(r'^(?:VRB|\d{3}|///|[NSEW]{1,3})P?\d{2,3}(?:GP?\d{2,3})?(?:KT|MPS|KMH)$')
_WIND_VAR_RE = re.compile(r'^\d{3}V\d{3}$')
_VIS_RE = re.compile(r'^(?:\d{4}[NSEW]{0,2}|\d{1,2}(?:/\d)?SM|////|\d{4})$')
_RVR_RE = re.compile(r'^R\d{2}[LRC]?/[\dMPUDN/]+$')


def extract_wx_tokens(raw_metar):
    if not raw_metar:
        return []
    text = str(raw_metar).split('RMK')[0].strip()
    if text.endswith('='):
        text = text[:-1].strip()
    toks = text.split()
    start = None
    for i, tok in enumerate(toks):
        if _TIMESTAMP_RE.match(tok):
            start = i + 1
            break
    if start is None:
        start = 0
        for tok in toks[:2]:
            if tok in ('METAR', 'SPECI', 'COR') or re.match(r'^[A-Z]{4}$', tok):
                start += 1
    out = []
    for tok in toks[start:]:
        if tok.startswith(CLOUD_PREFIXES):
            break
        if _TEMP_DEWPOINT_RE.match(tok) or _QNH_RE.match(tok):
            break
        if tok in ('AUTO', 'COR', 'CAVOK', 'METAR', 'SPECI', 'NIL', 'NSW', 'NDV', '9999'):
            continue
        if _WIND_RE.match(tok) or _WIND_VAR_RE.match(tok) or _RVR_RE.match(tok):
            continue
        if _VIS_RE.match(tok):
            continue
        if _WX_TOKEN_RE.match(tok):
            out.append(tok)
    return out


def strip_intensity(tok):
    """'-TSRA' -> 'TSRA'. 'VC' prefix kept (VCSH, VCTS)."""
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


# ======================================================================
# HELPERS - TAF segment parsing
# ======================================================================

def resolve_datetime(dd, hh, ref_dt):
    day, hour = int(dd), int(hh)
    extra = pd.Timedelta(hours=1) if hour == 24 else pd.Timedelta(0)
    if hour == 24:
        hour = 0
    cands = []
    for off in (-1, 0, 1):
        base = ref_dt.replace(day=1) + pd.DateOffset(months=off)
        try:
            cands.append(base.replace(day=day, hour=hour, minute=0, second=0, microsecond=0))
        except ValueError:
            pass
    return min(cands, key=lambda c: abs((c - ref_dt).total_seconds())) + extra


def parse_taf_segments(raw_taf, confeccao_data):
    lines = [ln.strip() for ln in str(raw_taf).replace('\r', '').split('\n') if ln.strip()]
    segs = []
    if not lines:
        return segs
    header = lines[0]
    m = re.search(r'(\d{2})(\d{2})/(\d{2})(\d{2})', header)
    if m:
        d1, h1, d2, h2 = m.groups()
        parts = header.split()
        segs.append({'type': 'main', 'start': resolve_datetime(d1, h1, confeccao_data),
                     'end': resolve_datetime(d2, h2, confeccao_data),
                     'rest': parts[4:] if len(parts) > 4 else []})
    for line in lines[1:]:
        m2 = re.match(r'(BECMG|TEMPO)\s+(\d{2})(\d{2})/(\d{2})(\d{2})', line)
        if m2:
            typ, d1, h1, d2, h2 = m2.groups()
            segs.append({'type': typ, 'start': resolve_datetime(d1, h1, confeccao_data),
                         'end': resolve_datetime(d2, h2, confeccao_data),
                         'rest': line.split()[2:]})
    return segs


def select_segment(segments, tipo_grupo, data_hora):
    cands = [s for s in segments if s['type'] == tipo_grupo]
    for s in cands:
        if s['start'] <= data_hora < s['end']:
            return s
    before = [s for s in cands if s['start'] <= data_hora]
    if before:
        return max(before, key=lambda s: s['start'])
    return cands[0] if cands else None


def extract_taf_weather(rest_tokens):
    wx = []
    for tok in rest_tokens:
        if tok.startswith(CLOUD_PREFIXES):
            break
        if tok.startswith(('TX', 'TN')) or tok == 'RMK':
            break
        if tok == 'CAVOK':
            continue
        if _WX_TOKEN_RE.match(tok):
            wx.append(tok)
    return ' '.join(wx) if wx else None


# ======================================================================
# HELPERS - season / period / airport lists
#
# Estacoes astronomicas, hemisferio sul, limites fixos na media do periodo
# (2016-2026); deriva interanual de +/- 1 dia nao modelada.
#   Verao 21 Dez-19 Mar | Outono 20 Mar-20 Jun | Inverno 21 Jun-21 Set | Primavera 22 Set-20 Dez
# Periodos: 4 faixas de 6 h em UTC.
# ======================================================================

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


def load_icaos_txt(path=ICAOS_TXT):
    with open(path) as f:
        return [ln.strip().upper() for ln in f if ln.strip()]


def malha_alternado_icaos(path=MALHA_CSV):
    s = pd.read_csv(path, sep=';', usecols=['Alternado'], low_memory=False)['Alternado']
    return sorted(s.dropna().astype(str).str.strip().str.upper().unique())


# ======================================================================
# PIPELINE
# ======================================================================

ARRIVAL_FMT = '%Y-%m-%d %H:%M:%S.%f'


def load_iata_to_icao():
    df = pd.read_csv(AIRPORTS_CSV)
    m = {}
    for _, row in df.iterrows():
        iata = str(row['iata_code']).strip().upper()
        icao = str(row['ident']).strip().upper()
        if iata and iata != 'NAN':
            m[iata] = icao
    log.info("IATA->ICAO: %d", len(m))
    return m


def load_alternados():
    out = {}
    for aircraft, fn in ALTERNADOS_FILES.items():
        df = pd.read_excel(fn)
        d = {}
        for _, row in df.iterrows():
            dest = str(row['destino']).strip().upper()
            alt = str(row['alternado_1']).strip()
            if not dest or dest == 'NAN' or not alt or alt == 'NAN':
                continue
            try:
                dist = float(row['distancia_1_nm'])
            except (ValueError, TypeError):
                dist = None
            d[dest] = {'alt': alt.upper(), 'dist': dist}
        out[aircraft] = d
        log.info("%s: %d rotas", fn, len(d))
    return out


def fill_alternado(df, iata_to_icao, alternados):
    col = pd.Series([pd.NA] * len(df), dtype=object, index=df.index)
    filled = 0
    for idx, row in df.iterrows():
        ac = EQUIPMENT_TO_AIRCRAFT.get(str(row['EquipmentModel']).strip().upper())
        if ac is None:
            continue
        icao = iata_to_icao.get(str(row['ArrivalStation']).strip().upper())
        if icao is None or icao not in alternados[ac]:
            continue
        col[idx] = alternados[ac][icao]['alt']
        filled += 1
    df['Alternado'] = col
    log.info("Alternado: %d/%d", filled, len(df))


def fill_horario(df, iata_to_icao, alternados):
    col = pd.Series([pd.NA] * len(df), dtype=object, index=df.index)
    filled = 0
    for idx, row in df.iterrows():
        if pd.isna(row['Alternado']) or str(row['Alternado']).strip() == '':
            continue
        ac = EQUIPMENT_TO_AIRCRAFT.get(str(row['EquipmentModel']).strip().upper())
        if ac is None:
            continue
        icao = iata_to_icao.get(str(row['ArrivalStation']).strip().upper())
        info = alternados[ac].get(icao) if icao else None
        if not info or info['dist'] is None:
            continue
        stautc = pd.to_datetime(row['STAUTC'], errors='coerce')
        if pd.isna(stautc):
            continue
        arr = stautc + pd.Timedelta(hours=info['dist'] / CRUISE_SPEEDS[ac])
        col[idx] = arr.strftime(ARRIVAL_FMT)[:-3]
        filled += 1
    df['Hora Chegada Alternado'] = col
    log.info("Hora Chegada Alternado: %d/%d", filled, len(df))


def _airport_id(conn, cache, icao):
    if icao not in cache:
        cur = conn.cursor()
        cur.execute("SELECT id FROM airports WHERE icao = %s", (icao,))
        r = cur.fetchone()
        cur.close()
        cache[icao] = r[0] if r else None
    return cache[icao]


def fill_metar(df, conn):
    cache = {}
    col = pd.Series([pd.NA] * len(df), dtype=object, index=df.index)
    filled = nsw = 0
    cur = conn.cursor()
    for idx, row in df.iterrows():
        icao = str(row['Alternado']).strip().upper()
        if not icao or icao == 'NAN':
            continue
        arr = pd.to_datetime(row['Hora Chegada Alternado'], errors='coerce')
        if pd.isna(arr):
            continue
        aid = _airport_id(conn, cache, icao)
        if not aid:
            continue
        cur.execute("SELECT raw_metar FROM metars WHERE airport_id = %s AND validade_inicial <= %s "
                    "ORDER BY validade_inicial DESC LIMIT 1", (aid, arr))
        r = cur.fetchone()
        if r is None:
            continue
        toks = extract_wx_tokens(r[0])            # keeps intensity (+/-), like the original
        col[idx] = ' '.join(toks) if toks else 'NSW'
        filled += bool(toks)
        nsw += not bool(toks)
    cur.close()
    df['METAR'] = col
    log.info("METAR: %d com fenomeno, %d NSW", filled, nsw)


def fill_taf_redemet(df, conn):
    """Coluna 'TAF REDEMET': segmento vigente do ultimo boletim tafs no horario de chegada."""
    cache = {}
    col = pd.Series([pd.NA] * len(df), dtype=object, index=df.index)
    cur = conn.cursor()
    filled = nsw = 0
    for idx, row in df.iterrows():
        icao = str(row['Alternado']).strip().upper()
        if not icao or icao == 'NAN':
            continue
        arr = pd.to_datetime(row['Hora Chegada Alternado'], errors='coerce')
        if pd.isna(arr):
            continue
        aid = _airport_id(conn, cache, icao)
        if not aid:
            continue
        cur.execute("""
            SELECT confeccao_data, tipo_grupo, data_hora, raw_taf FROM tafs
            WHERE airport_id = %s
              AND confeccao_data = (SELECT MAX(confeccao_data) FROM tafs
                                    WHERE airport_id = %s AND confeccao_data <= %s)
              AND data_hora <= %s
            ORDER BY data_hora DESC LIMIT 1
        """, (aid, aid, arr, arr))
        r = cur.fetchone()
        if r is None:
            continue
        confeccao_data, tipo_grupo, data_hora, raw_taf = r
        seg = select_segment(parse_taf_segments(raw_taf, confeccao_data), tipo_grupo, data_hora)
        if seg is None:
            continue
        cond = extract_taf_weather(seg['rest'])
        col[idx] = cond if cond else 'NSW'
        filled += bool(cond)
        nsw += not bool(cond)
    cur.close()
    df['TAF REDEMET'] = col
    log.info("TAF REDEMET: %d com fenomeno, %d NSW", filled, nsw)


PIPELINE_STEPS = ['alternado', 'horario', 'metar', 'taf_redemet']


def pipeline_run(steps=None):
    steps = PIPELINE_STEPS if steps is None else [s for s in PIPELINE_STEPS if s in steps]
    log.info("carregando %s", MALHA_CSV)
    df = pd.read_csv(MALHA_CSV, sep=';', low_memory=False)
    log.info("%d voos", len(df))

    iata_to_icao = alternados = None
    if {'alternado', 'horario'} & set(steps):
        iata_to_icao = load_iata_to_icao()
        alternados = load_alternados()
    conn = get_conn() if {'metar', 'taf_redemet'} & set(steps) else None
    try:
        if 'alternado' in steps:
            fill_alternado(df, iata_to_icao, alternados)
        if 'horario' in steps:
            fill_horario(df, iata_to_icao, alternados)
        if 'metar' in steps:
            fill_metar(df, conn)
        if 'taf_redemet' in steps:
            fill_taf_redemet(df, conn)
    finally:
        if conn:
            conn.close()
    df.to_csv(MALHA_CSV, sep=';', index=False)
    log.info("salvo %s", MALHA_CSV)
    return df


# ======================================================================
# REPORTS - shared
# ======================================================================

def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M')


def _load_malha():
    return pd.read_csv(MALHA_CSV, sep=';', low_memory=False)


# Airport scope for the current PDF being built (set by full_report).
# None = all airports. SCOPE_TAG names the group ('LISTA' / 'DEMAIS') and is
# appended to the CSV matrix filenames.
SCOPE_ICAOS = None
SCOPE_TAG = ''


def _scope_df(df):
    """Filter a malha frame by the active airport scope. Needs an 'icao' column."""
    if SCOPE_ICAOS is None:
        return df
    return df[df['icao'].isin(SCOPE_ICAOS)].copy()


def _scope_airports(icaos):
    """Filter an iterable of ICAOs by the active scope."""
    if SCOPE_ICAOS is None:
        return list(icaos)
    return [i for i in icaos if i in SCOPE_ICAOS]


def _csv_path(base):
    tag = f"_{SCOPE_TAG}" if SCOPE_TAG else ""
    return os.path.join(OUTPUT_DIR, f"{base}{tag}.csv")


def _scope_note():
    return f" | grupo {SCOPE_TAG}" if SCOPE_TAG else ""


def _styles():
    s = getSampleStyleSheet()
    return (
        ParagraphStyle('T', parent=s['Heading1'], fontSize=16, alignment=TA_CENTER,
                       fontName='Helvetica-Bold', textColor=colors.HexColor('#1a1a1a'), spaceAfter=6),
        ParagraphStyle('S', parent=s['Normal'], fontSize=9, alignment=TA_CENTER,
                       textColor=colors.HexColor('#666666'), spaceAfter=12),
        ParagraphStyle('N', parent=s['Normal'], fontSize=9,
                       textColor=colors.HexColor('#333333'), spaceAfter=8),
        ParagraphStyle('H', parent=s['Normal'], fontSize=10, fontName='Helvetica-Bold',
                       textColor=colors.HexColor('#1a1a1a'), spaceBefore=10, spaceAfter=4),
    )


def _table(data, widths, highlight_last=False, repeat=False):
    t = Table(data, colWidths=widths, repeatRows=1 if repeat else 0)
    cmds = [
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1a1a1a')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 8.5),
        ('ALIGN', (1, 0), (-1, -1), 'CENTER'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cccccc')),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]
    if highlight_last:
        cmds += [('ROWBACKGROUNDS', (0, 1), (-1, -2), [colors.white, colors.HexColor('#f5f5f5')]),
                 ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#e0e0e0')),
                 ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold')]
    else:
        cmds.append(('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f5f5f5')]))
    t.setStyle(TableStyle(cmds))
    return t


def _doc(path):
    return SimpleDocTemplate(path, pagesize=A4, topMargin=0.6 * inch, bottomMargin=0.6 * inch)


# ======================================================================
# REPORT 1 - seasonal_taf_accuracy
# ======================================================================

def _acc_line(sub):
    n = len(sub)
    c = int(sub['correct'].sum())
    return n, c, int(sub['false_positive'].sum()), int(sub['miss'].sum()), (100 * c / n if n else 0)


def seasonal_taf_accuracy():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    df = _load_malha()
    df['icao'] = df['Alternado'].astype(str).str.strip().str.upper()
    df = _scope_df(df)
    df['arr'] = pd.to_datetime(df['Hora Chegada Alternado'], errors='coerce')
    df = df[df['arr'].notna() & df['TAF REDEMET'].notna() & df['METAR'].notna()].copy()
    df['season'] = df['arr'].apply(season_of)
    df['period'] = df['arr'].apply(period_of)
    df['forecast'] = df['TAF REDEMET'].astype(str).str.strip().ne('NSW')
    df['observed'] = df['METAR'].astype(str).str.strip().ne('NSW')
    df['correct'] = df['forecast'].eq(df['observed'])
    df['false_positive'] = df['forecast'] & ~df['observed']
    df['miss'] = ~df['forecast'] & df['observed']

    icaos = _scope_airports(load_icaos_txt())
    by_icao = {k: v for k, v in df.groupby('icao')}
    rows = []

    def emit(icao, season, period, sub):
        n = len(sub)
        rows.append({'icao': icao, 'season': season, 'period': period, 'n_comparable': n,
                     'correct': int(sub['correct'].sum()),
                     'false_positive': int(sub['false_positive'].sum()),
                     'miss': int(sub['miss'].sum()),
                     'pct_correct': round(100 * sub['correct'].sum() / n, 1) if n else ''})

    def blocks(icao, sub_i):
        for season in SEASONS:
            ss = sub_i[sub_i['season'] == season]
            for period in PERIODS:
                emit(icao, season, period, ss[ss['period'] == period])
            emit(icao, season, 'TODOS', ss)
        for period in PERIODS:
            emit(icao, 'TODAS', period, sub_i[sub_i['period'] == period])
        emit(icao, 'TODAS', 'TODOS', sub_i)

    for icao in icaos:
        blocks(icao, by_icao.get(icao, df.iloc[0:0]))
    blocks('TODOS', df)
    out_csv = _csv_path('taf_accuracy_matrix')
    pd.DataFrame(rows).to_csv(out_csv, index=False)

    title, sub, note, _h = _styles()
    story = [Paragraph(f"TAF REDEMET vs METAR - Estacao x Periodo{_scope_note()}", title),
             Paragraph(f"Gerado {_now()} | malha.csv | {len(df):,} voos comparaveis | "
                       f"{df['icao'].nunique()} aeroportos com dado", sub),
             Paragraph("Regra: acerto = TAF e METAR concordam na presenca de fenomeno (ambos com "
                       "condicao, ou ambos NSW). Falso positivo = TAF previu, METAR nao confirmou. "
                       "Miss = METAR teve condicao, TAF nao previu. Janela: 'Hora Chegada "
                       "Alternado' UTC. Estacoes astronomicas (hem. sul).", note)]

    n, c, fp, ms, pct = _acc_line(df)
    story += [Paragraph("<b>Geral</b>", note),
              _table([['Comparaveis', 'Certo', '%', 'Falso Positivo', 'Miss'],
                      [f"{n:,}", f"{c:,}", f"{pct:.1f}%", f"{fp:,}", f"{ms:,}"]], [1.4 * inch] * 5),
              Spacer(1, 0.18 * inch)]
    for label, col, values in [('Por estacao', 'season', SEASONS), ('Por periodo (UTC)', 'period', PERIODS)]:
        d = [[label.split()[-1], 'Comparaveis', 'Certo', '%', 'Falso Pos.', 'Miss']]
        for v in values:
            n, c, fp, ms, pct = _acc_line(df[df[col] == v])
            d.append([v, f"{n:,}", f"{c:,}", f"{pct:.1f}%", f"{fp:,}", f"{ms:,}"])
        story += [Paragraph(f"<b>{label}</b>", note),
                  _table(d, [1.2 * inch, 1.2 * inch, 1.0 * inch, 0.8 * inch, 1.0 * inch, 0.9 * inch]),
                  Spacer(1, 0.18 * inch)]

    d = [['Estacao \\ Periodo'] + list(PERIODS) + ['TODOS']]
    for s in list(SEASONS) + ['TODAS']:
        base = df if s == 'TODAS' else df[df['season'] == s]
        row = [s]
        for p in list(PERIODS) + [None]:
            seg = base if p is None else base[base['period'] == p]
            n, _, _, _, pct = _acc_line(seg)
            row.append(f"{pct:.1f}%\n({n:,})")
        d.append(row)
    story += [Paragraph("<b>Estacao x periodo (% acerto / n)</b>", note),
              _table(d, [1.3 * inch] + [1.05 * inch] * 5), PageBreak()]

    per_air = sorted(((icao, *_acc_line(s)) for icao, s in df.groupby('icao')),
                     key=lambda r: r[1], reverse=True)
    story += [Paragraph(f"Aeroportos{_scope_note()}", title),
              Paragraph(f"Gerado {_now()} | {len(per_air)} aeroportos com voo comparavel "
                        f"| ordenado por volume", sub)]
    d = [['ICAO', 'Comparaveis', 'Certo', '%', 'Falso Pos.', 'Miss']]
    for icao, n, c, fp, ms, pct in per_air:
        d.append([icao, f"{n:,}", f"{c:,}", f"{pct:.1f}%", f"{fp:,}", f"{ms:,}"])
    story.append(_table(d, [0.9 * inch, 1.2 * inch, 1.0 * inch, 0.8 * inch, 1.0 * inch, 0.9 * inch], repeat=True))

    print(f"  {out_csv}")
    return story


# ======================================================================
# REPORT 2 - seasonal_dangerous
# ======================================================================

_DNG_EXACT = ['TS', '+RA', '+TSRA', 'TSRA']       # only tokens that occur in TAF REDEMET
_DNG_BASE = ['TS', 'TSRA', '+RA']   # da lista rastreada; RA/SHRA puros nao entram


def _dng_cell(sub, cond, tcol, mcol):
    tf = sub[tcol].apply(lambda s: cond in s)
    mo = sub[mcol].apply(lambda s: cond in s)
    h = int((tf & mo).sum())
    n, m = int(tf.sum()), int(mo.sum())
    return {'taf_forecast': n, 'taf_hit': h, 'taf_pct': round(100 * h / n, 1) if n else '',
            'metar_observed': m, 'metar_caught': h, 'metar_pct': round(100 * h / m, 1) if m else ''}


def seasonal_dangerous():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    df = _load_malha()
    df['icao'] = df['Alternado'].astype(str).str.strip().str.upper()
    df = _scope_df(df)
    df['arr'] = pd.to_datetime(df['Hora Chegada Alternado'], errors='coerce')
    df = df[df['arr'].notna() & df['TAF REDEMET'].notna() & df['METAR'].notna()].copy()
    df['season'] = df['arr'].apply(season_of)
    df['period'] = df['arr'].apply(period_of)
    df['taf_tok'] = df['TAF REDEMET'].astype(str).str.split().apply(set)
    df['met_tok'] = df['METAR'].astype(str).str.split().apply(set)
    df['taf_base'] = df['taf_tok'].apply(dangerous_hits)
    df['met_base'] = df['met_tok'].apply(dangerous_hits)

    views = [('exato', _DNG_EXACT, 'taf_tok', 'met_tok'),
             ('sem_intensidade', _DNG_BASE, 'taf_base', 'met_base')]
    icaos = _scope_airports(load_icaos_txt())
    by_icao = {k: v for k, v in df.groupby('icao')}
    rows = []

    def emit(icao, season, period, sub):
        for view, conds, tc, mc in views:
            for cond in conds:
                rows.append({'icao': icao, 'season': season, 'period': period,
                             'view': view, 'condition': cond, **_dng_cell(sub, cond, tc, mc)})

    def blocks(icao, sub_i):
        for season in SEASONS:
            ss = sub_i[sub_i['season'] == season]
            for period in PERIODS:
                emit(icao, season, period, ss[ss['period'] == period])
            emit(icao, season, 'TODOS', ss)
        for period in PERIODS:
            emit(icao, 'TODAS', period, sub_i[sub_i['period'] == period])
        emit(icao, 'TODAS', 'TODOS', sub_i)

    for icao in icaos:
        blocks(icao, by_icao.get(icao, df.iloc[0:0]))
    blocks('TODOS', df)
    out_csv = _csv_path('dangerous_matrix')
    pd.DataFrame(rows).to_csv(out_csv, index=False)

    title, sub, note, _h = _styles()
    hdr = ['Cond', 'TAF previu', 'Confirmado', 'Acerto TAF %', 'METAR teve', 'Detectado %']
    W = [0.9 * inch, 0.95 * inch, 1.0 * inch, 1.05 * inch, 0.95 * inch, 1.05 * inch]

    def crow(sub_df, cond, tc, mc):
        st = _dng_cell(sub_df, cond, tc, mc)
        tp = f"{st['taf_pct']}%" if st['taf_pct'] != '' else '-'
        mp = f"{st['metar_pct']}%" if st['metar_pct'] != '' else '-'
        return [cond, f"{st['taf_forecast']:,}", f"{st['taf_hit']:,}", tp, f"{st['metar_observed']:,}", mp]

    story = [Paragraph(f"Condicoes perigosas - TAF REDEMET vs METAR{_scope_note()}", title),
             Paragraph(f"Gerado {_now()} | malha.csv | {len(df):,} voos comparaveis | "
                       f"{df['icao'].nunique()} aeroportos", sub),
             Paragraph("Acerto TAF % = dos voos em que o TAF previu a condicao, quantos o METAR "
                       "confirmou (match exato). Detectado % = dos voos com a condicao no METAR, "
                       "quantos o TAF previra. Congelantes/+TS/+SHRA/+SN/+SHSN omitidos (zero no "
                       "TAF). Estacoes astronomicas (hem. sul), periodos UTC.", note)]
    for _v, conds, tc, mc, label in [
            ('exato', _DNG_EXACT, 'taf_tok', 'met_tok', 'Visao EXATA (token identico)'),
            ('base', _DNG_BASE, 'taf_base', 'met_base', 'Visao SEM INTENSIDADE (+/- removido)')]:
        story += [Paragraph(f"<b>{label} - geral</b>", note),
                  _table([hdr] + [crow(df, c, tc, mc) for c in conds], W), Spacer(1, 0.16 * inch)]

    for dim, values, col in [('estacao', SEASONS, 'season'), ('periodo (UTC)', PERIODS, 'period')]:
        d = [[f'{dim[:8]} \\ cond'] + _DNG_EXACT]
        for v in values:
            ss = df[df[col] == v]
            row = [v]
            for c in _DNG_EXACT:
                st = _dng_cell(ss, c, 'taf_tok', 'met_tok')
                row.append(f"{st['taf_pct']}%\n({st['taf_forecast']:,})" if st['taf_forecast'] else "-")
            d.append(row)
        story += [Paragraph(f"<b>Por {dim} - visao exata</b>", note),
                  _table(d, [1.2 * inch] + [1.1 * inch] * 4), Spacer(1, 0.16 * inch)]

    for cond in ('TSRA', 'TS'):
        d = [['Estacao \\ Periodo'] + list(PERIODS) + ['TODOS']]
        for s in list(SEASONS) + ['TODAS']:
            base = df if s == 'TODAS' else df[df['season'] == s]
            row = [s]
            for p in list(PERIODS) + [None]:
                seg = base if p is None else base[base['period'] == p]
                st = _dng_cell(seg, cond, 'taf_tok', 'met_tok')
                row.append(f"{st['taf_pct']}%\n({st['taf_forecast']:,})" if st['taf_forecast'] else "-")
            d.append(row)
        story += [Paragraph(f"<b>{cond} - estacao x periodo (acerto TAF % / n previsto)</b>", note),
                  _table(d, [1.3 * inch] + [1.0 * inch] * 5), Spacer(1, 0.16 * inch)]

    story.append(PageBreak())
    per_air = []
    for icao, s in df.groupby('icao'):
        tf = s['taf_tok'].apply(lambda x: bool({'TS', 'TSRA'} & x))
        mo = s['met_tok'].apply(lambda x: bool({'TS', 'TSRA'} & x))
        hit = tf & mo
        n, moN, h = int(tf.sum()), int(mo.sum()), int(hit.sum())
        if n == 0 and moN == 0:
            continue
        per_air.append((icao, len(s), n, h, moN, 100 * h / n if n else None,
                        100 * h / moN if moN else None))
    per_air.sort(key=lambda r: r[2], reverse=True)
    story += [Paragraph(f"Aeroportos - TS/TSRA (visao exata){_scope_note()}", title),
              Paragraph(f"Gerado {_now()} | {len(per_air)} aeroportos com TS/TSRA no TAF ou METAR. "
                        f"Ordenado por nº previsto.", sub)]
    d = [['ICAO', 'Voos', 'TAF previu', 'Confirmado', 'Acerto %', 'METAR teve', 'Detectado %']]
    for icao, tot, n, h, moN, ap, dp in per_air:
        d.append([icao, f"{tot:,}", f"{n:,}", f"{h:,}",
                  f"{ap:.1f}%" if ap is not None else '-', f"{moN:,}",
                  f"{dp:.1f}%" if dp is not None else '-'])
    story.append(_table(d, [0.85 * inch, 0.8 * inch, 0.95 * inch, 0.95 * inch, 0.85 * inch,
                            0.95 * inch, 1.0 * inch], repeat=True))

    print(f"  {out_csv}")
    return story


# ======================================================================
# REPORT 3 - dangerous_accuracy (geral, sem recorte)
# ======================================================================

_ACC_EXACT_SHOWN = ['TS', '+RA', '+SHRA', '+TSRA', 'TSRA']
_ACC_BASE_SHOWN = list(DANGEROUS_BASE)


def _acc_global(df, wanted, tcol, mcol):
    wanted = set(wanted)
    taf_d = df[tcol].apply(lambda s: bool(s & wanted))
    met_d = df[mcol].apply(lambda s: bool(s & wanted))
    acerto = int((taf_d & met_d).sum())
    fp = int((taf_d & ~met_d).sum())
    miss = int((~taf_d & met_d).sum())
    denom = acerto + fp + miss
    return {'flights_total': len(df), 'flights_relevant': int((taf_d | met_d).sum()),
            'taf_flagged': int(taf_d.sum()), 'metar_observed': int(met_d.sum()),
            'acerto': acerto, 'falso_positivo': fp, 'miss': miss,
            'accuracy_pct': round(100 * acerto / denom, 1) if denom else 0.0,
            'detection_pct': round(100 * acerto / (acerto + miss), 1) if (acerto + miss) else 0.0,
            'precision_pct': round(100 * acerto / (acerto + fp), 1) if (acerto + fp) else 0.0}


def _acc_per_cond(df, conds, tcol, mcol):
    out = []
    for c in conds:
        tf = df[tcol].apply(lambda s: c in s)
        mo = df[mcol].apply(lambda s: c in s)
        n, m = int(tf.sum()), int(mo.sum())
        h = int((tf & mo).sum())
        out.append({'cond': c, 'taf_forecast': n, 'confirmed': h,
                    'precision_pct': round(100 * h / n, 1) if n else None,
                    'metar_observed': m, 'detected_pct': round(100 * h / m, 1) if m else None,
                    'false_positive': n - h, 'miss': m - h})
    return out


def dangerous_accuracy():
    df = _load_malha()
    df['icao'] = df['Alternado'].astype(str).str.strip().str.upper()
    df = _scope_df(df)
    df = df[df['TAF REDEMET'].notna() & df['METAR'].notna()].copy()
    df['taf_tok'] = df['TAF REDEMET'].astype(str).str.split().apply(set)
    df['met_tok'] = df['METAR'].astype(str).str.split().apply(set)
    df['taf_base'] = df['taf_tok'].apply(dangerous_hits)
    df['met_base'] = df['met_tok'].apply(dangerous_hits)

    gx = _acc_global(df, DANGEROUS_CONDITIONS, 'taf_tok', 'met_tok')
    gb = _acc_global(df, DANGEROUS_BASE, 'taf_base', 'met_base')
    pe = _acc_per_cond(df, _ACC_EXACT_SHOWN, 'taf_tok', 'met_tok')
    pb = _acc_per_cond(df, _ACC_BASE_SHOWN, 'taf_base', 'met_base')

    def pct(v):
        return f"{v:.1f}%" if v is not None else '-'

    print("=" * 78)
    print(f"ACURACIA - FENOMENOS PERIGOSOS - TAF REDEMET vs METAR{_scope_note()}")
    print("=" * 78)
    print(f"Voos comparaveis: {gx['flights_total']:,}\n")
    for label, g in (("EXATO", gx), ("SEM INTENSIDADE", gb)):
        print(f"--- QUALQUER perigosa, {label} ---")
        print(f"  relevantes (TAF ou METAR): {g['flights_relevant']:,}   "
              f"TAF: {g['taf_flagged']:,}   METAR: {g['metar_observed']:,}")
        print(f"  acerto {g['acerto']:,} | falso pos {g['falso_positivo']:,} | miss {g['miss']:,}")
        print(f"  ACURACIA {g['accuracy_pct']:.1f}% | deteccao {g['detection_pct']:.1f}% | "
              f"precisao {g['precision_pct']:.1f}%\n")
    for label, rws in (("EXATO", pe), ("SEM INTENSIDADE", pb)):
        print(f"--- Por fenomeno, {label} ---")
        for r in rws:
            print(f"  {r['cond']:<7} TAF {r['taf_forecast']:>6,}  conf {r['confirmed']:>5,}  "
                  f"prec {pct(r['precision_pct']):>6}  METAR {r['metar_observed']:>6,}  "
                  f"detec {pct(r['detected_pct']):>6}  fpos {r['false_positive']:>6,}  miss {r['miss']:>6,}")
        print()

    title, sub, note, _h = _styles()
    story = [Paragraph(f"Acuracia - fenomenos perigosos{_scope_note()}", title),
             Paragraph(f"Gerado {_now()} | malha.csv | {gx['flights_total']:,} voos comparaveis | "
                       "TAF REDEMET vs METAR observado", sub),
             Paragraph("Lista: FZDZ FZRA FZFG TS +TS +RA +SHRA +SN +SHSN TSRA. 'QUALQUER perigosa' "
                       "= o voo tem pelo menos um desses tokens. Acuracia = acerto / (acerto + "
                       "falso positivo + miss).", note), Spacer(1, 0.1 * inch)]
    head = ['Leitura', 'Voos c/ perigo', 'Acerto', 'Falso pos.', 'Miss', 'Acuracia', 'Deteccao', 'Precisao']
    d = [head]
    for label, g in (('exato', gx), ('sem intensidade', gb)):
        d.append([label, f"{g['flights_relevant']:,}", f"{g['acerto']:,}", f"{g['falso_positivo']:,}",
                  f"{g['miss']:,}", f"{g['accuracy_pct']:.1f}%", f"{g['detection_pct']:.1f}%",
                  f"{g['precision_pct']:.1f}%"])
    story += [_table(d, [1.0 * inch, 1.1 * inch, 0.7 * inch, 0.8 * inch, 0.6 * inch, 0.8 * inch,
                         0.8 * inch, 0.75 * inch]), Spacer(1, 0.15 * inch),
              Paragraph("<b>Deteccao</b> = acerto / observados (1 - miss). <b>Precisao</b> = "
                        "acerto / sinalizados (1 - falso positivo).", note), PageBreak(),
              Paragraph("Por fenomeno", title),
              Paragraph(f"Gerado {_now()} | {gx['flights_total']:,} voos comparaveis", sub)]
    ph = ['Fenomeno', 'TAF previu', 'Confirmado', 'Precisao %', 'METAR teve', 'Detectado %', 'Falso pos.', 'Miss']
    W = [1.0 * inch, 0.9 * inch, 0.95 * inch, 0.9 * inch, 0.9 * inch, 0.95 * inch, 0.85 * inch, 0.7 * inch]
    for label, rws in (('Visao EXATA', pe), ('Visao SEM INTENSIDADE', pb)):
        d = [ph]
        for r in rws:
            d.append([r['cond'], f"{r['taf_forecast']:,}", f"{r['confirmed']:,}", pct(r['precision_pct']),
                      f"{r['metar_observed']:,}", pct(r['detected_pct']), f"{r['false_positive']:,}",
                      f"{r['miss']:,}"])
        story += [Paragraph(f"<b>{label}</b>", note), _table(d, W), Spacer(1, 0.16 * inch)]
    return story


# ======================================================================
# REPORT 4 - generate_reports (consolidado TAF REDEMET vs METAR)
# ======================================================================

def _agreement(taf_col, metar_col):
    tf = taf_col != 'NSW'
    mo = metar_col != 'NSW'
    correct = (tf & mo) | (~tf & ~mo)
    n = len(taf_col)
    return {'n': n, 'correct': int(correct.sum()),
            'false_positive': int((tf & ~mo).sum()), 'miss': int((~tf & mo).sum()),
            'pct': 100 * correct.sum() / n if n else 0}


def _exact_table(taf_col, metar_col, conds):
    tt = taf_col.astype(str).str.split()
    mt = metar_col.astype(str).str.split()
    rows = []
    tn = tc = 0
    for cond in conds:
        tf = tt.apply(lambda toks: cond in toks)
        mm = mt.apply(lambda toks: cond in toks)
        n = int(tf.sum())
        c = int((tf & mm).sum())
        rows.append((cond, n, c, n - c, 100 * c / n if n else 0))
        tn += n
        tc += c
    rows.append(('TOTAL', tn, tc, tn - tc, 100 * tc / tn if tn else 0))
    return rows


def _actual_breakdown(taf_col, metar_col, conds, top_n=8):
    tt = taf_col.astype(str).str.split()
    out = {}
    for cond in conds:
        mask = tt.apply(lambda toks: cond in toks)
        n = int(mask.sum())
        if n == 0:
            continue
        vc = metar_col[mask].value_counts()
        rows, other = [], 0
        for i, (val, cnt) in enumerate(vc.items()):
            if i < top_n:
                rows.append((val, int(cnt)))
            else:
                other += int(cnt)
        if other:
            rows.append(('Other', other))
        out[cond] = {'n': n, 'rows': rows}
    return out


def _breakdown_section(story, breakdown, conds, sh, note):
    story.append(Paragraph("O que o METAR mostrou, quando o TAF previu cada condicao:", note))
    for cond in conds:
        if cond not in breakdown:
            continue
        info = breakdown[cond]
        story.append(Paragraph(f"{cond} (previsto {info['n']:,}x)", sh))
        d = [['METAR real', 'Qtd', '%']]
        for val, cnt in info['rows']:
            d.append([str(val), f"{cnt:,}", f"{100*cnt/info['n']:.1f}%"])
        story.append(_table(d, [2.5 * inch, 1.2 * inch, 1.0 * inch]))


def generate_reports():
    df = _load_malha()
    df['icao'] = df['Alternado'].astype(str).str.strip().str.upper()
    df = _scope_df(df)
    title, sub, note, sh = _styles()
    gen = _now()

    comp = df[df['TAF REDEMET'].notna() & df['METAR'].notna()].copy()
    g = _agreement(comp['TAF REDEMET'], comp['METAR'])
    story = [Paragraph(f"TAF REDEMET vs METAR{_scope_note()}", title),
             Paragraph(f"Gerado {gen} | comparaveis: {len(comp):,} de {len(df):,}", sub),
             Paragraph("Regra: acerto quando TAF e METAR concordam na presenca de fenomeno "
                       "(ambos com condicao, ou ambos NSW).", note), Spacer(1, 0.15 * inch),
             _table([['', 'Certo', '%', 'Falso Positivo', 'Miss'],
                     ['TAF REDEMET', f"{g['correct']:,}", f"{g['pct']:.1f}%",
                      f"{g['false_positive']:,}", f"{g['miss']:,}"]],
                    [1.6 * inch, 1.1 * inch, 0.9 * inch, 1.6 * inch, 1.1 * inch]), PageBreak(),
             Paragraph("TAF REDEMET vs METAR - Condicoes Perigosas (match exato)", title),
             Paragraph(f"Gerado {gen} | comparaveis: {len(comp):,}", sub), Spacer(1, 0.1 * inch)]
    d = [['Condicao', 'TAF previu', 'METAR igual', 'Errado', 'Acerto %']]
    for cond, n, c, w, pct in _exact_table(comp['TAF REDEMET'], comp['METAR'], DANGEROUS_CONDITIONS):
        d.append([cond, f"{n:,}", f"{c:,}", f"{w:,}", f"{pct:.1f}%"])
    story += [_table(d, [1.3 * inch, 1.2 * inch, 1.2 * inch, 1.0 * inch, 1.1 * inch], highlight_last=True),
              Spacer(1, 0.15 * inch)]
    _breakdown_section(story, _actual_breakdown(comp['TAF REDEMET'], comp['METAR'], DANGEROUS_CONDITIONS),
                       DANGEROUS_CONDITIONS, sh, note)
    story += [PageBreak(),
              Paragraph("TAF REDEMET vs METAR - Perigosas (intensidade ignorada)", title),
              Paragraph(f"Gerado {gen} | comparaveis: {len(comp):,}", sub), Spacer(1, 0.1 * inch)]
    d = [['Condicao base', 'TAF previu', 'METAR igual', 'Errado', 'Acerto %']]
    tr = comp['TAF REDEMET'].astype(str).str.split().apply(lambda t: ' '.join(sorted(dangerous_hits(t))))
    mr = comp['METAR'].astype(str).str.split().apply(lambda t: ' '.join(sorted(dangerous_hits(t))))
    for cond, n, c, w, pct in _exact_table(tr, mr, DANGEROUS_BASE):
        d.append([cond, f"{n:,}", f"{c:,}", f"{w:,}", f"{pct:.1f}%"])
    story.append(_table(d, [1.3 * inch, 1.2 * inch, 1.2 * inch, 1.0 * inch, 1.1 * inch], highlight_last=True))
    return story


# ======================================================================
# REPORT - forecast_only (malha + alternados, SO os fenomenos PREVISTOS)
# ======================================================================

def _forecast_only_one():
    """Universo = voos cujo TAF REDEMET previu algum fenomeno no alternado.
    Nada de NSW-vs-NSW nem de miss: so o que foi previsto e como verificou.
    Respeita o escopo ativo (SCOPE_ICAOS / SCOPE_TAG)."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    df = _load_malha()
    df['icao'] = df['Alternado'].astype(str).str.strip().str.upper()
    df = _scope_df(df)
    df['arr'] = pd.to_datetime(df['Hora Chegada Alternado'], errors='coerce')
    df = df[df['TAF REDEMET'].notna() & df['METAR'].notna()].copy()

    # so os voos com fenomeno previsto
    fc = df[df['TAF REDEMET'].astype(str).str.strip().ne('NSW')].copy()
    fc['taf_tok'] = fc['TAF REDEMET'].astype(str).str.split().apply(set)
    fc['met_tok'] = fc['METAR'].astype(str).str.split().apply(set)
    fc['taf_base'] = fc['taf_tok'].apply(lambda s: {strip_intensity(t) for t in s})
    fc['met_base'] = fc['met_tok'].apply(lambda s: {strip_intensity(t) for t in s})
    fc['arr'] = pd.to_datetime(fc['Hora Chegada Alternado'], errors='coerce')
    fc['season'] = fc['arr'].apply(lambda x: season_of(x) if pd.notna(x) else None)
    fc['period'] = fc['arr'].apply(lambda x: period_of(x) if pd.notna(x) else None)

    n = len(fc)
    exact_ok = fc.apply(lambda r: bool(r['taf_tok'] & r['met_tok']), axis=1)
    base_ok = fc.apply(lambda r: bool(r['taf_base'] & r['met_base']), axis=1)
    metar_clear = fc['METAR'].astype(str).str.strip().eq('NSW')
    metar_other = ~base_ok & ~metar_clear          # METAR teve fenomeno, mas outro

    def p(x):
        return f"{100 * x / n:.1f}%" if n else "-"

    # matriz por fenomeno previsto (todos os tokens que aparecem no TAF)
    from collections import Counter
    tok_count = Counter(t for s in fc['taf_tok'] for t in s)
    tokens = [t for t, _ in tok_count.most_common()]
    rows = []
    for t in tokens:
        m = fc['taf_tok'].apply(lambda s: t in s)
        sub = fc[m]
        k = int(m.sum())
        ex = int(sub.apply(lambda r: t in r['met_tok'], axis=1).sum())
        exb = int(sub.apply(lambda r: strip_intensity(t) in r['met_base'], axis=1).sum())
        clr = int(sub['METAR'].astype(str).str.strip().eq('NSW').sum())
        rows.append((t, k, ex, exb, clr))
    csv_rows = [{'fenomeno_previsto': t, 'voos': k, 'confirmado_exato': ex,
                 'confirmado_base': exb, 'metar_nsw': clr,
                 'pct_exato': round(100 * ex / k, 1) if k else '',
                 'pct_base': round(100 * exb / k, 1) if k else '',
                 'pct_nsw': round(100 * clr / k, 1) if k else ''} for t, k, ex, exb, clr in rows]
    out_csv = _csv_path('forecast_only_matrix')
    pd.DataFrame(csv_rows).to_csv(out_csv, index=False)

    breakdown = _actual_breakdown(fc['TAF REDEMET'], fc['METAR'], tokens, top_n=8)

    title, sub, note, sh = _styles()
    story = [
        Paragraph(f"Fenomenos PREVISTOS - verificacao contra METAR{_scope_note()}", title),
        Paragraph(f"Gerado {_now()} | malha.csv + alternados | "
                  f"{n:,} voos com fenomeno previsto pelo TAF REDEMET no alternado "
                  f"(de {len(df):,} comparaveis)", sub),
        Paragraph("Universo restrito ao que o TAF previu (nao-NSW). 'Confirmado exato' = o "
                  "METAR observou o mesmo token. 'Confirmado base' = mesmo fenomeno, "
                  "intensidade ignorada. 'METAR NSW' = falso positivo (nao veio nada). "
                  "'METAR outro fenomeno' = veio algo, mas diferente do previsto.", note),
        Spacer(1, 0.12 * inch),
        Paragraph("<b>Resumo</b>", sh),
        _table([
            ['', 'Voos', '% dos previstos'],
            ['Confirmado exato', f"{int(exact_ok.sum()):,}", p(int(exact_ok.sum()))],
            ['Confirmado base (intensidade ignorada)', f"{int(base_ok.sum()):,}", p(int(base_ok.sum()))],
            ['METAR outro fenomeno', f"{int(metar_other.sum()):,}", p(int(metar_other.sum()))],
            ['METAR NSW (falso positivo)', f"{int(metar_clear.sum()):,}", p(int(metar_clear.sum()))],
        ], [3.0 * inch, 1.3 * inch, 1.4 * inch]),
        Spacer(1, 0.18 * inch),
        Paragraph("<b>Por fenomeno previsto</b>", sh),
    ]
    d = [['Previsto', 'Voos', 'Confirm. exato', '%', 'Confirm. base', '%', 'METAR NSW', '%']]
    for t, k, ex, exb, clr in rows:
        d.append([t, f"{k:,}", f"{ex:,}", f"{100*ex/k:.1f}%" if k else '-',
                  f"{exb:,}", f"{100*exb/k:.1f}%" if k else '-',
                  f"{clr:,}", f"{100*clr/k:.1f}%" if k else '-'])
    story += [_table(d, [0.95 * inch, 0.7 * inch, 1.05 * inch, 0.6 * inch, 1.0 * inch,
                         0.6 * inch, 0.85 * inch, 0.6 * inch], repeat=True), Spacer(1, 0.18 * inch)]

    # estacao x periodo: taxa de confirmacao exata dos previstos
    story.append(Paragraph("<b>Confirmacao exata por estacao x periodo (% / n previsto)</b>", sh))
    fc = fc.assign(_ex=exact_ok.values)
    dd = [['Estacao \\ Periodo'] + list(PERIODS) + ['TODOS']]
    for s in list(SEASONS) + ['TODAS']:
        base = fc if s == 'TODAS' else fc[fc['season'] == s]
        row = [s]
        for pr in list(PERIODS) + [None]:
            seg = base if pr is None else base[base['period'] == pr]
            k = len(seg)
            row.append(f"{100*seg['_ex'].sum()/k:.1f}%\n({k:,})" if k else '-')
        dd.append(row)
    story += [_table(dd, [1.3 * inch] + [1.05 * inch] * 5), PageBreak()]

    # o que o METAR mostrou de fato
    story.append(Paragraph(f"O que o METAR mostrou, quando o TAF previu cada condicao{_scope_note()}", title))
    _breakdown_section(story, breakdown, tokens, sh, note)

    # por aeroporto
    story.append(PageBreak())
    story.append(Paragraph(f"Por aeroporto{_scope_note()}", title))
    per = []
    for icao, g in fc.groupby('icao'):
        k = len(g)
        ex = int(g['_ex'].sum())
        per.append((icao, k, ex, 100 * ex / k if k else 0))
    per.sort(key=lambda r: r[1], reverse=True)
    d = [['ICAO', 'Voos previstos', 'Confirm. exato', 'Acerto %']]
    for icao, k, ex, pct in per:
        d.append([icao, f"{k:,}", f"{ex:,}", f"{pct:.1f}%"])
    story.append(_table(d, [1.0 * inch, 1.4 * inch, 1.3 * inch, 1.0 * inch], repeat=True))

    out_pdf = f"relatorio_previstos{('_' + SCOPE_TAG) if SCOPE_TAG else ''}.pdf"
    _doc(out_pdf).build(story)
    print(f"  {out_csv}\n  {out_pdf}")


def forecast_only():
    """Dois PDFs (so os fenomenos previstos): relatorio_previstos_NavBrasil.pdf
    e relatorio_previstos_CIMAER.pdf."""
    global SCOPE_ICAOS, SCOPE_TAG
    for tag, icaos in _groups():
        SCOPE_ICAOS, SCOPE_TAG = icaos, tag
        try:
            print(f"  [{tag}] fenomenos previstos")
            _forecast_only_one()
        finally:
            SCOPE_ICAOS, SCOPE_TAG = None, ''


# ======================================================================
# RELATORIO - duas versoes: um PDF por grupo (NavBrasil / CIMAER)
# ======================================================================

_SECTIONS = [
    ("1. Presenca TAF REDEMET vs METAR (estacao x periodo)", seasonal_taf_accuracy),
    ("2. Fenomenos perigosos - acuracia", dangerous_accuracy),
    ("3. Condicoes perigosas (estacao x periodo)", seasonal_dangerous),
    ("4. Consolidado TAF REDEMET vs METAR", generate_reports),
]


def _groups():
    """[('NavBrasil', [icaos...]), ('CIMAER', [icaos...])] -- particao de todos
    os aerodromos (aiports.txt + alternados da malha) pela lista GROUP_A."""
    all_i = set(load_icaos_txt()) | set(malha_alternado_icaos())
    return [('NavBrasil', sorted(GROUP_A_SET)),
            ('CIMAER', sorted(all_i - GROUP_A_SET))]


def _build_report(tag, icaos, out_pdf, descr):
    global SCOPE_ICAOS, SCOPE_TAG
    SCOPE_ICAOS, SCOPE_TAG = icaos, tag
    try:
        title, sub, note, _sh = _styles()
        story = [
            Paragraph(f"RELATORIO TAF x METAR - grupo {tag}", title),
            Paragraph(f"Gerado {_now()} | {descr} | fonte: excel/malha.csv + "
                      "tabelas metars / tafs", sub),
            Paragraph("Secoes: 1) presenca por estacao x periodo  2) fenomenos perigosos, "
                      "acuracia  3) perigosos por estacao x periodo  4) consolidado. "
                      f"Matrizes por aeroporto em output/*_{tag}.csv.", note),
        ]
        for header, fn in _SECTIONS:
            print(f"  [{tag}] {header}")
            story.append(PageBreak())
            story += fn()
        _doc(out_pdf).build(story)
        print(f"  {out_pdf}")
    finally:
        SCOPE_ICAOS, SCOPE_TAG = None, ''


def full_report():
    """Gera dois PDFs: relatorio_NavBrasil.pdf (os 43 ICAOs da lista) e
    relatorio_CIMAER.pdf (todo o resto). Matrizes granulares em
    output/*_NavBrasil.csv e *_CIMAER.csv."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    for tag, icaos in _groups():
        _build_report(tag, icaos, f"relatorio_{tag}.pdf", f"{len(icaos)} aerodromos")


# ======================================================================
# CLI
# ======================================================================

REPORTS = {'full_report': full_report, 'forecast_only': forecast_only}


def run_reports(names=None):
    for name in (names or list(REPORTS)):
        if name not in REPORTS:
            raise SystemExit(f"relatorio desconhecido: {name}. use 'main.py list'")
        print(f"[{name}]")
        REPORTS[name]()


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    ap = argparse.ArgumentParser(description="Azul TAF x METAR")
    sp = ap.add_subparsers(dest='cmd', required=True)

    p = sp.add_parser('pipeline', help='preenche excel/malha.csv')
    p.add_argument('--steps', help=f"subconjunto de {','.join(PIPELINE_STEPS)} (virgula)")

    p = sp.add_parser('reports', help='gera relatorios')
    p.add_argument('names', nargs='*', help='nomes (vazio = todos)')

    sp.add_parser('all', help='pipeline + todos os relatorios')
    sp.add_parser('list', help='lista os relatorios')

    args = ap.parse_args(argv)
    if args.cmd == 'list':
        for k in REPORTS:
            print(k)
    elif args.cmd == 'pipeline':
        pipeline_run(args.steps.split(',') if args.steps else None)
    elif args.cmd == 'reports':
        run_reports(args.names or None)
    elif args.cmd == 'all':
        pipeline_run()
        run_reports()


if __name__ == '__main__':
    main()
