from hashlib import sha256
import io
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.platypus import Table, TableStyle

from pipeline import extract_metrics, extract_pdf

ROOT = Path(__file__).resolve().parents[1]
# Montants entièrement fictifs, en millions de MAD, à part le dividende.
ISSUERS = [
    ('ATW', 'Attijariwafa bank', 'Banques', 'MA0000012445', (28400, 7100, 16, None), (32100, 8420, 18, None)),
    ('IAM', 'Maroc Telecom', 'Télécommunications', 'MA0000011488', (36800, 5950, 4.2, 15100), (37100, 6220, 4.6, 14900)),
    ('BCP', 'Banque Centrale Populaire', 'Banques', 'MA0000011884', (22900, 3900, 9, None), (25600, 4450, 10, None)),
    ('MSA', 'Marsa Maroc', 'Transport & logistique', 'MA0000012312', (4310, 815, 8.5, 960), (4960, 1020, 9.75, 1040)),
    ('LHM', 'LafargeHolcim Maroc', 'Matériaux de construction', 'MA0000012320', (8080, 1290, 60, 5750), (8360, 1410, 65, 5490)),
]

NEWS = [
    ('visa', 'Visas et agréments des OPCVM', 'communique_ammc', '2026-03-26', [
        'Le communiqué récapitule les agréments et les visas délivrés au cours du mois.',
        'Les décisions concernent des fonds monétaires, obligataires et diversifiés.',
        'Les catégories de fonds figurent dans une annexe récapitulative.',
    ]),
    ('fonds', 'Atlas Trésorerie · Fiche mensuelle', 'opcvm', '2026-03-20', [
        'Atlas Trésorerie est classé dans la catégorie des OPCVM monétaires.',
        'La valeur liquidative au titre du mois est de 1 042,35 MAD.',
        'La fiche présente les règles de valorisation et la fréquence de publication.',
    ]),
    ('publication', 'Calendrier des publications financières', 'communique_ammc', '2026-03-16', [
        'Le calendrier rappelle les échéances de publication des émetteurs.',
        'Les rapports annuels regroupent les états financiers et les notes annexes.',
        'Les publications semestrielles couvrent la première moitié de l’exercice.',
    ]),
]


def fmt(value):
    return f'{value:,}'.replace(',', ' ').replace('.', ',')


def pdf_bytes(title, lines, table=None):
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, invariant=1)
    pdf.setTitle(title)

    def header(subtitle):
        pdf.setFillColor(colors.HexColor('#006752'))
        pdf.rect(0, 790, 595, 52, fill=1, stroke=0)
        pdf.setFillColor(colors.white)
        pdf.setFont('Helvetica-Bold', 14)
        pdf.drawString(45, 810, 'MarketLens AI / DOCUMENT DE DÉMONSTRATION')
        pdf.setFillColor(colors.black)
        pdf.setFont('Helvetica-Bold', 16)
        pdf.drawString(45, 746, title)
        pdf.setFont('Helvetica', 11)
        pdf.drawString(45, 720, subtitle)
        pdf.drawString(45, 45, f'Simulation · chiffres fictifs · page {pdf.getPageNumber()}')

    header('Présentation du document')
    if table:
        pdf.drawString(45, 690, 'Les indicateurs consolidés figurent à la page suivante.')
        pdf.showPage()
        header('Indicateurs financiers consolidés')
    y = 685
    for line in lines:
        pdf.drawString(45, y, line)
        y -= 23
    if table:
        grid = Table(table, colWidths=[260, 230])
        grid.setStyle(TableStyle([
            ('GRID', (0, 0), (-1, -1), 0.7, colors.grey),
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#d9eee7')),
        ]))
        _, height = grid.wrapOn(pdf, 500, 400)
        grid.drawOn(pdf, 45, y - height - 15)
    pdf.save()
    return buffer.getvalue()


def seed_demo_market():
    points = [16853.4, 16975.1, 16919.5, 17074.2, 17006.1, 17231.45, 17138.6, 17228.7, 17302.45, 17269.4, 17382.15, 17408.6]
    dates = ['09', '10', '11', '12', '13', '16', '17', '18', '19', '20', '23', '24']
    closes = {'ATW': 675, 'IAM': 119, 'BCP': 243, 'MSA': 770, 'LHM': 1840}
    shares = {'ATW': 215_140_839, 'IAM': 879_095_340, 'BCP': 203_312_473, 'MSA': 73_395_600, 'LHM': 23_431_240}
    history = []
    for index, (day, masi) in enumerate(zip(dates, points)):
        date = f'2026-03-{day}'
        change = round((masi / points[index - 1] - 1) * 100, 2) if index else 0
        ytd = round((masi / 16649.6 - 1) * 100, 2)
        rows = []
        for position, (ticker, name, _, _, previous, _) in enumerate(ISSUERS):
            close = round(closes[ticker] * (1 + (index + 3 * (position % 3)) * .002 - (index % 3) * .003), 2)
            rows.append({'ticker': ticker, 'close_mad': close, 'share_count': shares[ticker],
                         'market_cap_mad': round(close * shares[ticker], 2), 'activity_volume_mad': round(close * (9703 + index * 258 + position * 2492), 2),
                         'dividend_mad': previous[2], 'dividend_year': 2024, 'dividend_date': f'2025-07-{7 + position * 4:02}', 'source_page': 1})
        title = f'Bulletin simulé du {day}/03/2026'
        url = f'/demo/masi-{date}.pdf'
        volume = 137_910_500 + index * 9_621_500 + (index % 3) * 11_510_000
        raw = pdf_bytes(title, [
            f'Indice MASI : {fmt(masi)} points · Séance : {fmt(change)} % · Depuis janvier : {fmt(ytd)} %',
            f'Volume non doublé : {fmt(volume)} MAD',
            *[f"{item['ticker']} : volume {fmt(item['activity_volume_mad'])} MAD · {fmt(item['share_count'])} titres" for item in rows],
            *[f"{item['ticker']} : clôture {fmt(item['close_mad'])} MAD · dividende {fmt(item['dividend_mad'])} MAD · {item['dividend_date']}" for item in rows],
        ])
        (ROOT / 'web/public' / url.lstrip('/')).write_bytes(raw)
        history.append(dict(session_date=date, masi=masi, day_change=change, ytd_change=ytd, source_url=url, source_page=1, volume_mad=volume, volume_page=1, quotes=rows))
    (ROOT / 'web/lib/demo-market.json').write_text(json.dumps(history, indent=2) + '\n')


def seed_demo():
    target = ROOT / 'web/public/demo'
    target.mkdir(parents=True, exist_ok=True)
    corpus = {'companies': [], 'documents': [], 'chunks': [], 'metrics': []}

    def add(slug, title, kind, published, lines, company=None, year=None, table=None):
        raw = pdf_bytes(title, lines, table)
        (target / f'{slug}.pdf').write_bytes(raw)
        doc = {
            'id': str(uuid5(NAMESPACE_URL, f'casaquant-demo/{slug}')), 'company_id': company,
            'title': title, 'doc_type': kind, 'fiscal_year': year, 'published_at': published,
            'source_url': f'/demo/{slug}.pdf', 'raw_hash': sha256(raw).hexdigest(), 'is_demo': True,
        }
        pages = extract_pdf(raw)
        corpus['documents'].append(doc)
        corpus['chunks'].extend({'document_id': doc['id'], **page} for page in pages)
        corpus['metrics'].extend(extract_metrics(doc, pages))

    for ticker, name, sector, isin, previous, latest in ISSUERS:
        corpus['companies'].append(dict(ticker=ticker, name=name, sector=sector, isin=isin))
        for year, amounts in [(2024, previous), (2025, latest)]:
            label = 'Produit net bancaire' if sector == 'Banques' else 'Chiffre d’affaires'
            pairs = [(label, amounts[0], 'MDH'), ('RNPG', amounts[1], 'MDH'),
                     ('Dividende par action', amounts[2], 'MAD/action'), ('Dette nette', amounts[3], 'MDH'),
                     ('Capitaux propres part du groupe', round(amounts[1] * 6.7), 'MDH'),
                     ('Total actif', round(amounts[0] * (13 if sector == 'Banques' else 1.8)), 'MDH')]
            if sector != 'Banques':
                pairs.extend([('EBE', round(amounts[0] * .43), 'MDH'), ('EBITDA', round(amounts[0] * .39), 'MDH')])
            values = [(name, f'{fmt(amount)} {unit}') for name, amount, unit in pairs if amount is not None]
            add(f'{ticker.lower()}-{year}', f'{ticker} · Rapport annuel {year}', 'rapport_annuel', f'{year + 1}-03-12',
                [f'{name} — exercice {year}.', *[f'{key} : {value}' for key, value in values]],
                ticker, year, [['Indicateur', 'Montant'], *values])
    for slug, title, kind, published, lines in NEWS:
        add(slug, title, kind, published, lines, year=2026)

    output = ROOT / 'web/lib/demo-data.json'
    output.write_text(json.dumps(corpus, ensure_ascii=False, indent=2) + '\n')
    seed_demo_market()
    return corpus
