import hashlib
import io
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unicodedata
from tempfile import TemporaryDirectory
from decimal import Decimal
from functools import lru_cache

import pdfplumber

DOC_TYPES = ('rapport_annuel', 'semestriel', 'communique_ammc', 'opcvm', 'communique_emetteur')
DIMENSIONS = 1536
STOP = set('le la les de des du en un une et au aux a est sont sur pour par dans que quel quels quelle quelles ce ces se ses'.split())


def tokens(value):
    text = unicodedata.normalize('NFKD', value.lower())
    text = ''.join(char for char in text if not unicodedata.combining(char))
    return [word for word in re.findall(r'[a-z0-9]+', text) if len(word) > 1 and word not in STOP]


def embedding(value):
    vector = [0] * DIMENSIONS
    for word in tokens(value):
        digest = hashlib.sha256(word.encode()).digest()
        vector[int.from_bytes(digest[:4], 'big') % DIMENSIONS] += 1 if digest[4] & 1 else -1
    return vector if any(vector) else None


def table_to_markdown(table):
    def cell(value):
        return ' '.join((value or '').split()).replace('|', r'\|')

    rows = [[cell(value) for value in row] for row in table if any(row)]
    width = max(map(len, rows), default=0)
    if width < 2 or len(rows) < 2:
        return ''
    rows.insert(1, ['---'] * width)
    return '\n'.join('| ' + ' | '.join(row + [''] * (width - len(row))) + ' |' for row in rows)


def ocr_available():
    return bool(shutil.which('tesseract') or (sys.platform == 'darwin' and shutil.which('swift')))


@lru_cache(maxsize=1)
def macos_ocr_cache():
    return TemporaryDirectory(prefix='casaquant-ocr-')


def recognize_page(page):
    image = page.to_image(resolution=170).original
    data = io.BytesIO()
    image.save(data, format='PNG')
    if shutil.which('tesseract'):
        response = subprocess.run(
            ['tesseract', 'stdin', 'stdout', '-l', 'fra+eng'],
            input=data.getvalue(), capture_output=True, check=True, timeout=20,
        )
    elif sys.platform == 'darwin' and shutil.which('swift'):
        response = subprocess.run(
            ['swift', '-module-cache-path', macos_ocr_cache().name, str(Path(__file__).with_name('ocr_macos.swift'))],
            input=data.getvalue(), capture_output=True, check=True, timeout=60,
        )
    else:
        raise RuntimeError('Moteur OCR indisponible.')
    return response.stdout.decode('utf-8').strip()


def extract_pdf(raw, *, max_pages=250, ocr=False):
    if not raw.startswith(b'%PDF-'):
        raise ValueError('Le fichier reçu n’est pas un PDF.')
    pages = []
    ocr_pages = 0
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        if len(pdf.pages) > max_pages:
            raise ValueError(f'PDF de plus de {max_pages} pages.')
        for page in pdf.pages:
            content = (page.extract_text() or '').strip()
            tables = [table_to_markdown(table) for table in page.extract_tables()]
            markdown = '\n\n'.join(filter(None, tables))
            if not content and not markdown and ocr and ocr_pages < 8 and (page.images or page.curves or page.rects):
                content = recognize_page(page)
                ocr_pages += 1
            if content or markdown:
                pages.append({
                    'page_number': page.page_number,
                    'content': content,
                    'table_markdown': markdown,
                })
            page.close()
    return pages


# Capture seulement une unité explicite. Les valeurs comparatives ne sont pas
# déduites de tableaux dont les colonnes ne sont pas identifiées.
METRIC_LINE = re.compile(
    r'^(Chiffre d.affaires(?: consolidé)?|Produit net bancaire|RNPG|Résultat net part du groupe|'
    r'Dividende par action|Dette nette(?: consolidée)?|EBE|EBITDA|Excédent brut d.exploitation|'
    r'(?:Capitaux|Fonds) propres(?: part du groupe)|Total (?:de l.)?actif)\s*:\s*'
    r'(-?\d[\d \u00a0\u202f]*(?:,\d+)?)\s*(MAD/action|M MAD|MDH|MAD|DH/action|DH)\s*\.?$',
    re.IGNORECASE,
)


NARRATIVE_AMOUNT = r'(-?\d[\d \u00a0\u202f]*(?:,\d+)?) (million|milliard)s? de (?:dirhams|MAD)'
NARRATIVE_METRICS = {
    'revenue_mad': re.compile(r"chiffre d.affaires(?: consolidé)? (?:s.est établi à|de franchir la barre des [^,.;]+, à) " + NARRATIVE_AMOUNT, re.I),
    'ebe_mad': re.compile(r"excédent brut d.exploitation (?:a atteint|s.est établi à) " + NARRATIVE_AMOUNT, re.I),
    'ebitda_mad': re.compile(r"ebitda (?:a atteint|s.est établi à) " + NARRATIVE_AMOUNT, re.I),
    'rnpg_mad': re.compile(r"résultat net part du groupe s.est élevé à " + NARRATIVE_AMOUNT, re.I),
}


def labeled_annual_table(document, page):
    # Ces modèles sont associés au document et au tableau vérifiés, puisque
    # les mêmes libellés ailleurs dans un PDF peuvent concerner un autre périmètre.
    file = document.get('source_url', '').split('/')[-1]
    text = ' '.join(page['content'].split())
    lines = page['content'].splitlines()
    amount = r'-?\d{1,3}(?: \d{3})*'
    percent = r'[+-]?\d+(?:,\d+)?%'
    key_map = {}
    block = {}
    if file == 'CP_Afric_Industries_2025.pdf' and document['company_id'] == 'AFI' and 'En kdh 2024 2025 Variation' in text:
        key_map = {"Chiffre d.affaires": 'revenue_mad', 'Résultat net': 'income_social_mad'}
        pattern = rf'^({"|".join(key_map)}) ({amount}) ({amount})\s+[+-]'
        factor = 1_000
    elif file == 'CP_LafargeHolcim_2025.pdf' and document['company_id'] == 'LHM' and re.search(r'En MDH 2024 2025', text, re.I):
        key_map = {"Chiffre d.affaires": 'revenue_mad'}
        pattern = rf'^({"|".join(key_map)}) ({amount}) ({amount})$'
        factor = 1_000_000
    elif file == 'Maroc_Telecom_2025.pdf' and document['company_id'] == 'IAM' and 'IFRS en millions MAD' in text and '12M 2024 12M 2025' in text:
        key_map = {"Chiffre d.affaires": 'revenue_mad', 'EBITDA': 'ebitda_mad', 'Dette Nette': 'net_debt_mad', 'Résultat Net Part du Groupe': 'rnpg_mad'}
        pattern = rf'^({"|".join(key_map)}) {amount} {amount} {percent} {percent} ({amount}) ({amount}) {percent}'
        factor = 1_000_000
    elif file == 'AWB_2025.pdf' and document['company_id'] == 'ATW' and 'RÉSULTATS 31 décembre 2025' in text:
        key_map = {'Produit net bancaire': 'revenue_mad', 'Résultat net part du Groupe': 'rnpg_mad', 'Total bilan consolidé': 'assets_mad'}
        for label, key in key_map.items():
            match = re.search(rf'› {label} (\d+,\d+) {percent}[0-9]* milliards de dirhams', text)
            if match:
                block.setdefault(2025, {})[key] = float(Decimal(match[1].replace(',', '.')) * 1_000_000_000)
        return block
    else:
        return {}
    for line in lines:
        match = re.match(pattern, line, re.I)
        if not match:
            continue
        key = next((key for label, key in key_map.items() if re.fullmatch(label, match[1], re.I)), None)
        if key:
            for year, digits in zip((2024, 2025), match.groups()[1:]):
                block.setdefault(year, {})[key] = int(digits.replace(' ', '')) * factor
    return block


def extract_metrics(document, pages):
    if document['doc_type'] != 'rapport_annuel' or not document['company_id'] or not document['fiscal_year']:
        return []
    rows = []
    for page in pages:
        values = {}
        for line in page['content'].splitlines():
            match = METRIC_LINE.fullmatch(line.strip())
            if not match:
                continue
            label, value, unit = match.groups()
            label, unit = label.lower(), unit.lower()
            amount = Decimal(re.sub(r'\s+', '', value).replace(',', '.'))
            if label.startswith('dividende'):
                if not unit.endswith('/action'):
                    continue
                key = 'dividend_per_share'
            elif unit.endswith('/action'):
                continue
            elif label.startswith(('chiffre', 'produit')):
                key = 'revenue_mad'
            elif label.startswith('dette'):
                key = 'net_debt_mad'
            elif label.startswith(('capitaux', 'fonds')):
                key = 'equity_group_mad'
            elif label.startswith('total'):
                key = 'assets_mad'
            elif label == 'ebe' or label.startswith('excédent'):
                key = 'ebe_mad'
            elif label == 'ebitda':
                key = 'ebitda_mad'
            else:
                key = 'rnpg_mad'
            values[key] = float(amount * (1_000_000 if unit in ('mdh', 'm mad') else 1))
        continuous = re.sub(r'\s+', ' ', page['content'])
        for key, pattern in NARRATIVE_METRICS.items():
            matches = pattern.findall(continuous)
            if key not in values and len(matches) == 1:
                amount, unit = matches[0]
                value = Decimal(re.sub(r'\s+', '', amount).replace(',', '.'))
                values[key] = float(value * (1_000_000 if unit.lower() == 'million' else 1_000_000_000))
        by_year = labeled_annual_table(document, page)
        if values:
            by_year[document['fiscal_year']] = {**values, **by_year.get(document['fiscal_year'], {})}
        for year, annual_values in by_year.items():
            rows.append({
                'company_id': document['company_id'], 'fiscal_year': year,
                'source_doc_id': document['id'], 'source_page': page['page_number'], **annual_values,
            })
    return rows
