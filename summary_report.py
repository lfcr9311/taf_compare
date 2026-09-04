#!/usr/bin/env python3
"""
Gera TAF_METAR_Summary_Report.pdf - relatorio executivo em ingles com objetivo,
metodo, resultados consolidados e limitacoes do estudo TAF x METAR.

    python summary_report.py
"""
from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY, TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (BaseDocTemplate, Frame, PageTemplate, Paragraph,
                                Spacer, Table, TableStyle, KeepTogether)

OUT = "TAF_METAR_Summary_Report.pdf"
INK = colors.HexColor('#15202c')
ACCENT = colors.HexColor('#1c5c86')
MUTED = colors.HexColor('#5c6b7a')
RULE = colors.HexColor('#c8d2dc')
PANEL = colors.HexColor('#eef2f6')
CRIT = colors.HexColor('#9c3327')
OK = colors.HexColor('#2b6449')

_s = getSampleStyleSheet()
TITLE = ParagraphStyle('T', parent=_s['Title'], fontName='Helvetica-Bold', fontSize=21,
                       leading=25, textColor=INK, alignment=0, spaceAfter=4)
SUB = ParagraphStyle('Sub', parent=_s['Normal'], fontName='Helvetica', fontSize=12.5,
                     leading=16, textColor=MUTED, spaceAfter=16)
H1 = ParagraphStyle('H1', parent=_s['Normal'], fontName='Helvetica-Bold', fontSize=13,
                    leading=16, textColor=INK, spaceBefore=18, spaceAfter=7)
H2 = ParagraphStyle('H2', parent=_s['Normal'], fontName='Helvetica-Bold', fontSize=10.5,
                    leading=13, textColor=ACCENT, spaceBefore=11, spaceAfter=5)
BODY = ParagraphStyle('B', parent=_s['Normal'], fontName='Helvetica', fontSize=9.5,
                      leading=13.5, textColor=INK, alignment=TA_JUSTIFY, spaceAfter=7)
NOTE = ParagraphStyle('N', parent=BODY, fontSize=8.8, leading=12, textColor=MUTED)
CAP = ParagraphStyle('C', parent=_s['Normal'], fontName='Helvetica-Bold', fontSize=8,
                     leading=10, textColor=MUTED, spaceBefore=2, spaceAfter=3)
CELL = ParagraphStyle('Cell', parent=_s['Normal'], fontName='Helvetica', fontSize=8.3,
                      leading=10.5, textColor=INK)
CELLB = ParagraphStyle('CellB', parent=CELL, fontName='Helvetica-Bold')

W = 6.9 * inch


def table(data, widths, align_right_from=1, highlight=None):
    t = Table(data, colWidths=widths, repeatRows=1)
    cmd = [
        ('BACKGROUND', (0, 0), (-1, 0), INK),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 8.3),
        ('LEADING', (0, 0), (-1, -1), 10.5),
        ('ALIGN', (align_right_from, 1), (-1, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.4, RULE),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, PANEL]),
    ]
    if highlight:
        for r in highlight:
            cmd += [('BACKGROUND', (0, r), (-1, r), colors.HexColor('#dbe7f0')),
                    ('FONTNAME', (0, r), (-1, r), 'Helvetica-Bold')]
    t.setStyle(TableStyle(cmd))
    return t


def kv_band(pairs):
    row = [[Paragraph(f'<font color="#5c6b7a">{k}</font><br/><b>{v}</b>', CELL) for k, v in pairs]]
    t = Table(row, colWidths=[W / len(pairs)] * len(pairs))
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), PANEL),
        ('BOX', (0, 0), (-1, -1), 0.4, RULE),
        ('INNERGRID', (0, 0), (-1, -1), 0.4, RULE),
        ('TOPPADDING', (0, 0), (-1, -1), 7),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
    ]))
    return t


def callout(text, color=ACCENT):
    t = Table([[Paragraph(text, CELL)]], colWidths=[W])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), PANEL),
        ('LINEBEFORE', (0, 0), (0, -1), 2.5, color),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING', (0, 0), (-1, -1), 10),
        ('RIGHTPADDING', (0, 0), (-1, -1), 10),
    ]))
    return t


def footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.4)
    canvas.line(0.75 * inch, 0.62 * inch, A4[0] - 0.75 * inch, 0.62 * inch)
    canvas.setFont('Helvetica', 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(0.75 * inch, 0.45 * inch, "TAF x METAR Verification Study - Summary Report")
    canvas.drawRightString(A4[0] - 0.75 * inch, 0.45 * inch, f"{doc.page}")
    canvas.restoreState()


def build():
    doc = BaseDocTemplate(OUT, pagesize=A4, leftMargin=0.75 * inch, rightMargin=0.75 * inch,
                          topMargin=0.7 * inch, bottomMargin=0.85 * inch, title="TAF x METAR Verification Study")
    frame = Frame(doc.leftMargin, doc.bottomMargin, W, A4[1] - 1.55 * inch, id='n')
    doc.addPageTemplates([PageTemplate(id='all', frames=[frame], onPage=footer)])
    S = []
    P = lambda t, st=BODY: S.append(Paragraph(t, st))

    P("TAF &times; METAR Verification Study", TITLE)
    P("Forecast reliability at alternate aerodromes &mdash; methods and consolidated results", SUB)
    S.append(kv_band([("Study window", "Aug 2025 - Aug 2026"),
                      ("Comparable flights", "263,509"),
                      ("Alternate aerodromes", "57"),
                      ("Observations", "1.34 M METAR")]))
    S.append(Spacer(1, 14))

    P("1. Objective", H1)
    P("This study measures how reliably the TAF issued for an <b>alternate aerodrome</b> predicts the weather "
      "actually observed there, as reported by the METAR, at the moment a diverting aircraft would arrive.")
    P("The operational question behind it: when dispatch planning relied on a forecast condition at the alternate, "
      "did that condition materialise? A forecast condition that fails to occur imposes unnecessary fuel and "
      "alternate-selection penalties; a condition that occurs unforecast is a safety concern.")

    P("2. Data Sources", H1)
    P("The study draws on twelve months of scheduled operations, verified against the national forecast and "
      "observation archive for the same period.")
    S.append(kv_band([("Scheduled flights", "268,674"),
                      ("TAF groups", "2,517,183"),
                      ("METAR observations", "1,337,707"),
                      ("Aerodromes", "122")]))
    S.append(Spacer(1, 8))
    P("Alternate assignment follows the published alternate table for each destination and aircraft type. "
      "Fleet covered: A20N, A21N, AT76, E195, E295.")

    P("3. Method - Constructing the Observation Set", H1)
    P("Each flight row is built through four sequential steps; each consumes the output of the previous one.")
    for n, h, b in [
        ("1", "Assign the theoretical alternate",
         "The destination IATA code is resolved to ICAO, then matched against the alternate table for that "
         "aircraft type. The first-ranked alternate is taken, with its great-circle distance."),
        ("2", "Estimate arrival time at the alternate",
         "Scheduled arrival at destination (UTC) plus the diversion leg, computed as distance divided by cruise "
         "speed for the type (250 kt jets, 180 kt turboprop). This is the timestamp against which weather is evaluated."),
        ("3", "Retrieve the observation",
         "The most recent METAR at or before the estimated arrival time, for that aerodrome."),
        ("4", "Retrieve the forecast",
         "The most recent TAF bulletin issued before arrival; within it, the forecast group in force at that time."),
    ]:
        S.append(Table([[Paragraph(f'<font color="#1c5c86"><b>{n}</b></font>', CELLB),
                         Paragraph(f"<b>{h}.</b> {b}", CELL)]],
                       colWidths=[0.3 * inch, W - 0.3 * inch],
                       style=TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'),
                                         ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
                                         ('LEFTPADDING', (0, 0), (0, -1), 0)])))
    S.append(Spacer(1, 5))
    S.append(callout(
        "<b>Weather extraction.</b> Present-weather groups are parsed from the <b>raw METAR/TAF text</b>, not from "
        "the database's pre-parsed field. Scanning begins after the DDHHMMZ timestamp and stops at the first cloud, "
        "temperature or QNH group; RMK sections are discarded. This prevents station identifiers from being misread "
        "as weather (for example SNBR as SN + BR)."))
    S.append(Spacer(1, 7))
    P("Resulting sample: <b>263,509 comparable flights</b> across <b>57 alternate aerodromes</b>. "
      "5,165 rows (1.92%) were discarded for missing TAF or METAR.")

    P("4. Verification Framework", H1)
    P("Every comparison falls into one cell of a 2x2 contingency table:")
    S.append(table([
        ["", "Condition observed", "Not observed"],
        ["Forecast", "hit", "false alarm"],
        ["Not forecast", "miss", "correct negative"],
    ], [2.3 * inch, 2.3 * inch, 2.3 * inch], align_right_from=1))
    S.append(Spacer(1, 8))
    S.append(table([
        ["Metric", "Formula", "Question answered"],
        ["Precision", "hit / (hit + false alarm)", "When the TAF warned, was it right?"],
        ["Detection (POD)", "hit / (hit + miss)", "Of events that occurred, how many were forecast?"],
        ["CSI", "hit / (hit + false alarm + miss)", "Combined score; penalises both error types"],
        ["HSS", "skill above random chance", "Comparable across aerodromes and seasons"],
    ], [1.3 * inch, 2.2 * inch, 3.4 * inch], align_right_from=3))
    S.append(Spacer(1, 7))
    S.append(callout(
        "<b>Correct negatives are excluded from CSI by design.</b> For rare events they dominate the sample and make "
        "raw accuracy uninformative. CSI is reported for absolute magnitude; HSS is used whenever subsets are "
        "compared, because CSI itself is sensitive to event climatology."))

    P("5. Tracked Conditions and the Intensity Rule", H1)
    P("The study tracks a defined list of operationally restrictive conditions:")
    S.append(callout("<font face='Courier'>-FZDZ  FZDZ  +FZDZ  -FZRA  FZRA  +FZRA  FZFG<br/>"
                     "TS  +TS  +RA  +SHRA  +SN  +SHSN  +TSRA  TSRA</font>", MUTED))
    S.append(Spacer(1, 7))
    P("This list is <b>deliberately asymmetric with respect to intensity</b>, and the matching logic preserves that:")
    S.append(table([
        ["Group", "Rule applied", "Effect"],
        ["FZDZ, FZRA, FZFG, TS, TSRA", "Any intensity qualifies", "-TSRA matches as TSRA"],
        ["+RA, +SHRA, +SN, +SHSN", "The + prefix is required", "Plain RA does not qualify"],
    ], [2.3 * inch, 2.2 * inch, 2.4 * inch], align_right_from=3))
    S.append(Spacer(1, 7))
    P("Collapsing intensity uniformly would convert <font face='Courier'>+RA</font> into "
      "<font face='Courier'>RA</font> and sweep light and moderate rain into scope, inflating the universe from "
      "13,359 to 26,836 flights. The implemented rule prevents this.")
    P("<b>Occurrence in the study period:</b> only four tracked tokens appear at all - TSRA (10,280 forecasts), "
      "TS (2,725), +TSRA (73) and +RA (23). Freezing precipitation, snow and +TS did not occur in either forecast "
      "or observation over twelve months.")

    P("6. Consolidated Results", H1)
    P("6.1 Phenomenon presence (any weather vs. NSW)", H2)
    S.append(table([
        ["Contingency cell", "Flights", "Share"],
        ["Both NSW (trivial agreement)", "208,038", "78.9%"],
        ["Both reported weather", "12,448", "4.7%"],
        ["TAF forecast, METAR clear", "24,685", "9.4%"],
        ["METAR reported, TAF clear", "18,338", "7.0%"],
    ], [3.5 * inch, 1.7 * inch, 1.7 * inch]))
    S.append(Spacer(1, 6))
    S.append(kv_band([("Raw accuracy", "83.7%"), ("Precision", "33.5%"),
                      ("Detection", "40.4%"), ("HSS", "27.4")]))
    S.append(Spacer(1, 8))
    S.append(callout(
        "<b>Critical caveat.</b> A forecaster who always wrote NSW would score <b>88.3%</b> - above the TAF's 83.7%. "
        "Raw accuracy is therefore unusable as a performance headline. The positive HSS confirms genuine skill; the "
        "TAF simply over-forecasts, trading accuracy for detection, which is the operationally rational direction "
        "of error in aviation.", CRIT))

    P("6.2 Tracked conditions", H2)
    S.append(table([
        ["Scope", "Flights", "Forecast", "Hits", "False alarms", "Precision", "CSI"],
        ["All", "263,509", "13,359", "1,375", "11,984", "10.3%", "8.5%"],
        ["NavBrasil", "129,443", "6,366", "-", "-", "8.7%", "7.1%"],
        ["CIMAER", "134,066", "6,993", "-", "-", "11.7%", "9.8%"],
    ], [1.1 * inch, 1.0 * inch, 0.95 * inch, 0.7 * inch, 1.05 * inch, 1.0 * inch, 0.7 * inch],
        highlight=[1]))

    P("6.3 Operational outcome - was the alternate actually usable?", H2)
    P("Of the 13,359 flights where a tracked condition was forecast at the alternate, "
      "<b>11,984 (89.7%)</b> had a METAR free of every tracked condition: the alternate would have been serviceable.")
    P("The remaining 1,375 are <b>not</b> forecast failures in operational terms. Even where the specific condition "
      "differed - TAF TSRA against METAR TS - the aerodrome remained unsuitable for dispatch.")

    P("6.4 Structure of the error", H2)
    P("Restricting to the 12,448 flights where weather was forecast <b>and</b> something occurred, the TAF named the "
      "correct phenomenon in <b>41.6%</b> of cases. The remaining 58.4% are not random: they cluster in adjacent categories.")
    S.append(table([
        ["Forecast", "Correct", "Most frequent substitution"],
        ["RA", "67.9%", "VCSH 31%, BR 27%, DZ 25%"],
        ["FG", "47.2%", "BR 46%"],
        ["BR", "40.1%", "RA 61%"],
        ["TSRA", "19.9%", "RA 49%, VCSH 31%, VCTS 14%"],
        ["TS", "12.5%", "VCSH 44%, VCTS 17%"],
        ["DZ", "9.6%", "RA 73%"],
        ["SHRA", "4.7%", "RA 56%, VCSH 35%"],
    ], [1.3 * inch, 1.3 * inch, 4.3 * inch], align_right_from=1, highlight=[4, 5]))
    S.append(Spacer(1, 7))
    S.append(callout(
        "When a TSRA forecast fails, <b>45% of those cases show VCSH or VCTS</b> - convection was present in the "
        "vicinity but did not discharge over the station at that instant. SHRA to RA and DZ to RA are distinctions "
        "of reporting granularity rather than forecast failure.<br/><br/>"
        "This materially changes the reading. <i>The TAF was wrong 58% of the time</i> and <i>the TAF identified the "
        "correct meteorological family in most cases</i> describe the same data. The second is more faithful and "
        "withstands technical scrutiny.", OK))

    P("7. Validation and Known Limitations", H1)
    P("An independent audit of the pipeline was performed. Findings are stated with their measurement basis.")
    P("7.1 Verified sound", H2)
    S.append(table([
        ["Check", "Result"],
        ["Weather parser - 4,000 raw METARs re-parsed against stored values", "Zero divergences"],
        ["Temporal integrity - no match to a METAR or TAF issued after arrival", "No look-ahead leakage"],
        ["Coverage - twelve continuous months", "1.92% row loss"],
    ], [4.7 * inch, 2.2 * inch], align_right_from=1))

    P("7.2 Limitations requiring disclosure", H2)
    S.append(table([
        ["#", "Finding", "Measurement"],
        ["1", "TEMPO groups read as the prevailing forecast, though TEMPO denotes "
              "transient fluctuation under 50% of the period",
         "14.4% of comparisons; 72% of phenomenon forecasts; 91% of TSRA"],
        ["2", "Exact-token comparison measures notation, not skill. TAF omits intensity in "
              "93.9% of cases; METAR writes the minus prefix in 42.5%",
         "TSRA precision 1.9% exact vs 7.5% intensity-aware; 1,802 observed -TSRA uncounted"],
        ["3", "Expired forecast groups used", "9.5% of comparisons (n=10,000)"],
        ["4", "Vicinity phenomena counted as occurrence at the aerodrome, though they "
              "denote 8-16 km distant", "23.8% of all METAR-with-phenomenon cases"],
        ["5", "No maximum age on METAR matching",
         "Median 31 min, p95 58 min; 2.31% exceed 3 h, worst case 67 h"],
        ["6", "Pseudo-replication - flights sharing an alternate at the same hour share "
              "one weather situation", "263,509 flights map to 120,475 situations (2.2x)"],
        ["7", "Sample concentration", "Five aerodromes hold 54.5% of flights"],
    ], [0.3 * inch, 3.6 * inch, 3.0 * inch], align_right_from=3))
    S.append(Spacer(1, 8))
    S.append(callout(
        "<b>A correction made during the audit.</b> An earlier draft attributed the false-alarm rate to TEMPO "
        "contamination and reported 27.8% expired groups. Both were revised.<br/><br/>"
        "Measuring false alarms by group origin gives <b>55.1% for the main group, 69.2% for BECMG and 68.2% for "
        "TEMPO</b> - and 53.4% when restricted to the main group still within its validity window. The false-alarm "
        "rate is therefore <b>real, not a TEMPO artefact</b>.<br/><br/>"
        "The expired-group figure conflated two semantics. BECMG describes a <b>permanent</b> change: after its "
        "transition window its conditions legitimately prevail. TEMPO describes a <b>transient</b> fluctuation that "
        "ceases. Counting completed BECMG groups as errors overstated the defect by a factor of about 2.8. The "
        "corrected figure is 9.5%.", CRIT))
    S.append(Spacer(1, 7))
    P("7.3 Statistical guidance for presentation", H2)
    P("Report <b>HSS</b> when comparing aerodromes, seasons or periods - CSI varies with local climatology and is "
      "not equitable across regions. Report the <b>base rate alongside any raw accuracy figure</b>. Declare "
      "<i>n</i> as independent weather situations (120,475), not flights (263,509); confidence intervals computed "
      "on the flight count are approximately 1.5 times too narrow.")

    P("8. Deliverables", H1)
    S.append(table([
        ["Artefact", "Content"],
        ["Interactive dashboard",
         "Live filtering by aerodrome, group, season, UTC period, month, equipment and intensity reading; "
         "contingency matrix, per-condition and per-aerodrome breakdowns, phenomenon confusion matrix"],
        ["relatorio_NavBrasil.pdf / relatorio_CIMAER.pdf",
         "Four sections each: presence by season and period, tracked-condition accuracy, seasonal breakdown, "
         "consolidated view"],
        ["taf_accuracy_matrix / dangerous_matrix CSV",
         "Granular matrices by aerodrome, season and period"],
        ["voos_taf_previu_metar_nao_confirmou.csv",
         "11,984 flights where a tracked condition was forecast at the alternate and the METAR showed none - "
         "the alternate was serviceable"],
    ], [2.3 * inch, 4.6 * inch], align_right_from=2))

    P("9. Reproducibility", H1)
    P("All figures derive from the flight network dataset and the forecast and observation archive. "
      "Sampled measurements use fixed random "
      "seeds. Full-population figures are stated against n = 263,509; sampled figures state their sample size "
      "explicitly. Season boundaries follow astronomical definitions for the Southern Hemisphere, with fixed dates "
      "at the period mean - inter-annual drift of plus or minus one day is not modelled and affects the December "
      "and June boundaries.")

    doc.build(S)
    return OUT


if __name__ == '__main__':
    import os
    p = build()
    print(f"{p}  ({os.path.getsize(p)/1024:.0f} KB)")
