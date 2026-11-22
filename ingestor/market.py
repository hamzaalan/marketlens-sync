import hashlib
import io
import json
import re
import unicodedata
from html.parser import HTMLParser
from datetime import datetime
from math import isfinite
from zoneinfo import ZoneInfo

import pdfplumber

ACTIONS = 'https://www.casablanca-bourse.com/live-market/actions'
BULLETINS = 'https://www.casablanca-bourse.com/market-data/bulletins-de-la-cote'
PRICE = r'(?:-?\d{1,3}(?:[ .\u00a0\u202f]\d{3})*|-?\d+)(?:,\d+)?'
CHANGE = r'[+-]?\d+(?:,\d+)?'
DAY = r'\d{2}/\d{2}/\d{2}'
MASI = re.compile(rf'^MASI ({PRICE}) ({CHANGE})\s*% ({CHANGE})\s*%$', re.M)
SESSION = re.compile(r'Séance du (\d{2}/\d{2}/\d{4})')
QUOTES = re.compile(
    rf'\b(?P<isin>MA[A-Z0-9]{{10}})\s+(?P<name>[^\n]+?)\s+(?:{PRICE}|-)\s+'
    rf'(?P<close>{PRICE}|-)\s+(?:{PRICE}|-)\s+(?:{PRICE}|-)\s+(?P<status>T|NT|R|S)(?:\s|$)'
)
REFERENCE = re.compile(rf'(?<![.\d])({PRICE}) {DAY} (?:{PRICE}|-)\s*$')
LAST_DIVIDEND = re.compile(rf'\b[CF]\s+({PRICE}) (20\d{{2}}) ({DAY})\s+(?:{PRICE})\s+(?:{DAY})\s+(?:{PRICE})\s*$')


# Le bulletin abrège certains noms qui sont développés dans le catalogue.
BULLETIN_ALIASES = {
    'ALLIANCES': 'ADI', 'BMCI': 'BCI', 'DOUJA PROM ADDOHA': 'ADH', 'SOTHEMA': 'SOT',
    'SODEP MARSA MAROC': 'MSA', 'SGTM': 'GTM', 'SONASID': 'SID', 'MINIERE TOUISSIT': 'CMT',
    'ENNAKL': 'NKL', 'PROMOPHARM': 'PRO', 'REALISATIONS MECANIQUES': 'SRM', 'OULMES': 'OUL',
    'S.M MONETIQUE': 'S2M', 'SNEP': 'SNP', 'STROC INDUSTRIE': 'STR', 'TGCC': 'TGC',
}


class MarketCatalog(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.reading = False

    def handle_starttag(self, tag, attrs):
        if tag == 'script' and dict(attrs).get('data-drupal-selector') == 'drupal-settings-json':
            self.reading = True

    def handle_endtag(self, tag):
        if tag == 'script':
            self.reading = False

    def handle_data(self, data):
        if self.reading:
            self.parts.append(data)

    def state(self):
        return json.loads(''.join(self.parts)).get('live_market', {})

    def companies(self):
        actions = self.state().get('actions', [])
        companies = {}
        for item in actions:
            ticker = item.get('symbol', '').strip()
            name = item.get('emetteur', {}).get('fr', '').strip()
            sector = item.get('secteur', {}).get('fr', '').strip()
            if re.fullmatch(r'[A-Z0-9]{1,12}', ticker) and name and sector:
                companies[ticker] = (ticker, name, sector, None)
        return list(companies.values())


    def activity(self, accepted):
        state = self.state()
        session = state.get('session', {})
        timestamp = session.get('timestamp')
        if session.get('status') != 'closed' or not isinstance(timestamp, (int, float)) or not isfinite(timestamp):
            return []
        day = datetime.fromtimestamp(timestamp, ZoneInfo('Africa/Casablanca')).date()
        quotes = {}
        for row in state.get('actions', []):
            symbol = row.get('symbol', '').strip()
            if symbol not in accepted:
                continue
            volume = market_number(row.get('volume'), zero=True)
            status = (row.get('statut') or {}).get('fr')
            if volume is None and row.get('nbTransactions') == 0 and status in ('NT', 'S'):
                volume = 0
            cap = market_number(row.get('capitalisation'))
            shares = market_number(row.get('nombreTitres'), integral=True)
            if any(value is not None for value in (volume, cap, shares)):
                quotes[symbol] = (day, symbol, volume, cap, shares, ACTIONS)
        return list(quotes.values())


def market_number(value, *, zero=False, integral=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
        return None
    if value == 0 and not zero:
        return None
    if integral:
        return int(value) if int(value) == value else None
    return value


def label_key(value):
    return ''.join(char for char in unicodedata.normalize('NFKD', value.upper()) if char.isascii() and char.isalnum())


def number(text):
    return float(re.sub(r'[.\s]', '', text).replace(',', '.'))


def parse_bulletin(pages, issuers, catalog=()):
    index = None
    quotes = {}
    tickers = {row[3]: row[0] for row in issuers if row[3]}
    available_codes = {row[0] for row in catalog}
    names = {label_key(alias): code for alias, code in BULLETIN_ALIASES.items() if code in available_codes}
    for code, name, _, _ in catalog:
        names[label_key(name)] = code
        names.setdefault(label_key(code), code)
    for page, text in pages:
        text = re.sub(r'[^\S\n]+', ' ', text)
        if not index and (session := SESSION.search(text)) and (match := MASI.search(text)):
            iso = datetime.strptime(session[1], '%d/%m/%Y').date().isoformat()
            index = (iso, *map(number, match.groups()), page)
        if 'Instruments Cours du jour Statut' not in text:
            continue
        for line in text.splitlines():
            match = QUOTES.search(line)
            if not match:
                continue
            ticker = tickers.get(match['isin']) or names.get(label_key(match['name']))
            if not ticker or ticker in quotes:
                continue
            prefix = line[:match.start()]
            actual = match['close']
            basis = 'cloture'
            if actual == '-':
                reference = REFERENCE.search(prefix) if match['status'] == 'S' else None
                if not reference:
                    continue
                actual = reference[1]
                basis = 'reference'
            info = LAST_DIVIDEND.search(prefix)
            dividend = number(info[1]) if info else None
            year = int(info[2]) if info else None
            date = datetime.strptime(info[3], '%d/%m/%y').date().isoformat() if info else None
            quotes[ticker] = (number(actual), dividend, year, date, page, basis, match['isin'])
    if index is None:
        raise ValueError('Indice MASI introuvable dans le bulletin.')
    return index, quotes


def parse_volume(text):
    for line in text.splitlines():
        columns = re.split(r' {2,}', line.strip())
        if len(columns) == 4 and columns[0] == 'TOTAL GENERAL' and re.fullmatch(PRICE, columns[2]):
            return number(columns[2])
    return None


def sync_market(pages, refresh=False):
    from demo import ISSUERS
    from main import connection, download, PublicationLinks, upsert_companies
    links = set()
    for page in range(pages):
        url = f'{BULLETINS}?page={page}' if page else BULLETINS
        parser = PublicationLinks(url)
        parser.feed(download(url, 4 * 1024 * 1024).decode('utf-8', errors='replace'))
        links.update(url for url, _, _ in parser.links if re.search(r'/BCFR_\d{8}\.pdf(?:\?.*)?$', url, re.I))
    catalog = []
    activity = []
    try:
        parser = MarketCatalog()
        parser.feed(download(ACTIONS, 4 * 1024 * 1024).decode('utf-8', errors='replace'))
        catalog = parser.companies()
        activity = parser.activity({item[0] for item in catalog})
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        print(f'Catalogue des valeurs inaccessible · {exc}')
    imported = skipped = failed = 0
    with connection() as conn:
        upsert_companies(conn, [dict(zip(('ticker', 'name', 'sector', 'isin'), row[:4])) for row in (*ISSUERS, *catalog)])
        with conn.cursor() as cursor:
            cursor.executemany('''INSERT INTO market_activity (session_date,ticker,volume_mad,market_cap_mad,share_count,source_url)
                VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(ticker,session_date) DO UPDATE SET
                volume_mad=EXCLUDED.volume_mad, market_cap_mad=EXCLUDED.market_cap_mad,
                share_count=EXCLUDED.share_count, source_url=EXCLUDED.source_url''', activity)
        conn.commit()
        for url in sorted(links):
            try:
                raw = download(url)
                digest = hashlib.sha256(raw).hexdigest()
                with conn.transaction():
                    if not refresh and conn.execute('''SELECT 1 FROM market_sessions s WHERE raw_hash=%s AND volume_mad IS NOT NULL
                            AND (%s OR (SELECT count(*) FROM market_quotes q WHERE q.session_date=s.session_date)>10)''',
                            (digest, len(catalog) < 15)).fetchone():
                        skipped += 1
                        continue
                    with pdfplumber.open(io.BytesIO(raw)) as pdf:
                        if len(pdf.pages) > 150:
                            raise ValueError('Bulletin de plus de 150 pages.')
                        texts = [(page.page_number, page.extract_text() or '') for page in pdf.pages]
                        index, quotes = parse_bulletin(texts, ISSUERS, catalog)
                        volume, volume_page = None, None
                        for page_number, text in texts:
                            if 'PHYSIONOMIE DES TRANSACTIONS' in text and 'TOTAL GENERAL' in text:
                                volume = parse_volume(pdf.pages[page_number - 1].extract_text(layout=True) or '')
                                if volume is not None:
                                    volume_page = page_number
                                    break
                    date, masi, change, ytd, page = index
                    conn.execute('''INSERT INTO market_sessions
                        (session_date,masi,day_change,ytd_change,source_url,source_page,raw_hash,volume_mad,volume_page)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (session_date) DO UPDATE SET masi=EXCLUDED.masi,day_change=EXCLUDED.day_change,
                        ytd_change=EXCLUDED.ytd_change,source_url=EXCLUDED.source_url,source_page=EXCLUDED.source_page,
                        raw_hash=EXCLUDED.raw_hash,volume_mad=EXCLUDED.volume_mad,volume_page=EXCLUDED.volume_page''',
                        (date, masi, change, ytd, url, page, digest, volume, volume_page))
                    conn.execute('DELETE FROM market_quotes WHERE session_date=%s', (date,))
                    with conn.cursor() as cursor:
                        cursor.executemany('''UPDATE companies c SET isin=%s WHERE ticker=%s AND isin IS NULL
                            AND NOT EXISTS (SELECT 1 FROM companies existing WHERE existing.isin=%s)''',
                                           [(quote[-1], ticker, quote[-1]) for ticker, quote in quotes.items()])
                        cursor.executemany('''INSERT INTO market_quotes
                            (session_date,ticker,close_mad,dividend_mad,dividend_year,dividend_date,source_page,quote_basis)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''',
                                           [(date, ticker, *quote[:-1]) for ticker, quote in quotes.items()])
                    imported += 1
            except Exception as exc:
                failed += 1
                print(f'Bulletin ignoré ({url}) · {exc}')
    print(f'{imported} séance(s) indexée(s) · {skipped} doublon(s) · {failed} échec(s)')
    return int(bool(failed) or not links)
