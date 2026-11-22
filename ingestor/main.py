import argparse
from datetime import date
from hashlib import sha256
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import ssl
import sys
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import Request, HTTPRedirectHandler, HTTPSHandler, build_opener

import psycopg
import truststore

from demo import ISSUERS, ROOT, seed_demo
from pipeline import DOC_TYPES, embedding, extract_metrics, extract_pdf, ocr_available, tokens

HOSTS = {'ammc.ma', 'www.ammc.ma', 'casablanca-bourse.com', 'www.casablanca-bourse.com', 'media.casablanca-bourse.com'}
LISTINGS = [
    ('https://www.ammc.ma/fr/taxonomy/term/6', True),
    ('https://www.ammc.ma/fr/communiques-presse-emetteurs', True),
    ('https://www.casablanca-bourse.com/apropos/publications/emetteurs', False),
]
KNOWN = [
    {
        'title': 'AMMC · Priorités d’actions 2026',
        'source_url': 'https://www.ammc.ma/sites/default/files/Communiqu%C3%A9%20de%20presse%20-%20Priorit%C3%A9s%20d%27actions%202026%20VFR.pdf',
        'doc_type': 'communique_ammc', 'fiscal_year': 2026,
    },
    {
        'title': 'AMMC · Agréments et visas OPCVM de septembre 2025',
        'source_url': 'https://www.ammc.ma/sites/default/files/Liste%20des%20agr%C3%A9ments%20et%20visas%20d%27OPCVM%20accord%C3%A9s%20par%20l%27AMMC%20au%20titre%20du%20mois%20de%20Septembre%202025.pdf',
        'doc_type': 'opcvm', 'fiscal_year': 2025,
    },
    {
        'title': 'Attijariwafa bank · Indicateurs du troisième trimestre 2026',
        'source_url': 'https://www.casablanca-bourse.com/sites/default/files/pemetteur/2026-11/cp_awb_t3_26.pdf',
        'doc_type': 'communique_emetteur', 'company_id': 'ATW', 'published_at': '2026-11-17', 'fiscal_year': 2026,
    },
    {
        'title': 'OPCVM · Fiche signalétique',
        'source_url': 'https://www.ammc.ma/sites/default/files/fiche%20signal%C3%A9tique.pdf',
        'doc_type': 'opcvm',
    },
    {
        'title': 'Marsa Maroc · Communication financière annuelle 2025',
        'source_url': 'https://www.casablanca-bourse.com/sites/default/files/pemetteur/2026-07/marsa_2025.pdf',
        'doc_type': 'rapport_annuel', 'company_id': 'MSA', 'published_at': '2026-04-30', 'fiscal_year': 2025,
    },
    {
        'title': 'Marsa Maroc · Communication financière annuelle 2024',
        'source_url': 'https://www.ammc.ma/sites/default/files/Marsa_Maroc_CP_RFA_2024.pdf',
        'doc_type': 'rapport_annuel', 'company_id': 'MSA', 'fiscal_year': 2024,
    },
]
ANNUAL_RELEASES = [
    ('AFI', 'Afric Industries', 'CP_Afric_Industries_2025.pdf', '2026-03-24'),
    ('ATW', 'Attijariwafa bank', 'AWB_2025.pdf', '2026-02-24'),
    ('IAM', 'Maroc Telecom', 'Maroc_Telecom_2025.pdf', '2026-02-13'),
    ('LHM', 'LafargeHolcim Maroc', 'CP_LafargeHolcim_2025.pdf', '2026-03-10'),
]
KNOWN.extend(dict(company_id=ticker, title=f'{name} · Résultats annuels 2025', doc_type='rapport_annuel',
                  fiscal_year=2025, published_at=published, source_url=f'https://www.ammc.ma/sites/default/files/{file}')
             for ticker, name, file, published in ANNUAL_RELEASES)
MAX_BYTES = 25 * 1024 * 1024
DOCUMENT_COLUMNS = ('company_id', 'title', 'doc_type', 'fiscal_year', 'published_at', 'source_url', 'raw_hash', 'is_demo')
METRIC_COLUMNS = ('company_id', 'fiscal_year', 'revenue_mad', 'rnpg_mad', 'dividend_per_share', 'net_debt_mad', 'ebe_mad', 'ebitda_mad', 'equity_group_mad', 'assets_mad', 'income_social_mad', 'source_doc_id', 'source_page')


def public_url(url):
    parts = urlparse(url)
    return parts.scheme == 'https' and parts.netloc in HOSTS


class PublicRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not public_url(newurl):
            raise ValueError('Redirection hors des domaines publics autorisés.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, limit=MAX_BYTES):
    if not public_url(url):
        raise ValueError('Source HTTPS AMMC ou Bourse de Casablanca attendue.')
    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if ca_file := os.getenv('SSL_CERT_FILE'):
        context.load_verify_locations(cafile=ca_file)
    opener = build_opener(PublicRedirect(), HTTPSHandler(context=context))
    request = Request(url, headers={'User-Agent': 'MarketLensAI/1.0 (+lecteur de documents publics)'})
    with opener.open(request, timeout=10) as response:
        raw = response.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('Taille maximale de téléchargement dépassée.')
    return raw


class PublicationLinks(HTMLParser):
    def __init__(self, listing):
        super().__init__()
        self.listing = listing
        self.links = []
        self.heading = ''
        self.reading_heading = False
        self.date = None
        self.pending = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'article':
            self.date = None
            self.heading = ''
        if tag in ('h2', 'h3', 'h4'):
            self.heading = ''
            self.reading_heading = True
        if tag == 'time' and attrs.get('datetime'):
            self.date = attrs['datetime'][:10]
        if tag == 'a' and attrs.get('href'):
            url = urljoin(self.listing, attrs['href']).split('#')[0]
            if public_url(url) and urlparse(url).path.lower().endswith('.pdf'):
                self.pending = (url, [])

    def handle_endtag(self, tag):
        if tag in ('h2', 'h3', 'h4'):
            self.reading_heading = False
        if tag == 'a' and self.pending:
            url, parts = self.pending
            link_label = ' '.join(''.join(parts).split())
            label = link_label if len(link_label) > 15 else self.heading.strip()
            self.links.append((url, label, self.date))
            self.pending = None

    def handle_data(self, data):
        if self.reading_heading:
            self.heading += data
        if self.pending:
            self.pending[1].append(data)
        if re.fullmatch(r'20\d{2}-\d{2}-\d{2}', data.strip()):
            self.date = data.strip()


def discover(listing, ammc, issuers=ISSUERS, limit=15):
    parser = PublicationLinks(listing)
    parser.feed(download(listing, 4 * 1024 * 1024).decode('utf-8', errors='replace'))
    results = []
    for url, heading, published in parser.links:
        name = unquote(Path(urlparse(url).path).stem).replace('_', ' ')
        title = heading if heading and heading.lower() not in ('actualités', 'publications des émetteurs') else name
        tags = ' '.join(tokens(f'{title} {name}'))
        company = next((ticker for ticker, issuer, *_ in issuers
                        if f' {ticker.lower()} ' in f' {tags} ' or ' '.join(tokens(issuer)) in tags), None)
        if ammc and ('opcvm' in tags or 'fiche signaletique' in tags):
            kind = 'opcvm'
        elif 'rapport annuel' in tags or 'rapport financier annuel' in tags or re.search(r'\brfa\b', tags):
            kind = 'rapport_annuel'
        elif 'semestriel' in tags or 'semestre' in tags:
            kind = 'semestriel'
        elif 'communique' in tags or re.search(r'\bcp\b', tags):
            kind = 'communique_ammc' if ammc and not company and 'indicateurs' not in tags else 'communique_emetteur'
        else:
            continue
        years = set(re.findall(r'\b20[0-9]{2}(?![0-9])', tags))
        results.append(dict(source_url=url, title=title, doc_type=kind, company_id=company,
                            fiscal_year=int(years.pop()) if len(years) == 1 else None, published_at=published))
        if len(results) >= limit:
            break
    return results


def connection():
    url = os.getenv('DATABASE_URL')
    if not url:
        raise ValueError('Définissez DATABASE_URL pour utiliser PostgreSQL.')
    return psycopg.connect(url, connect_timeout=5, prepare_threshold=None)


def upsert_companies(conn, companies):
    with conn.cursor() as cur:
        cur.executemany('''
            INSERT INTO companies (ticker, name, sector, isin) VALUES (%s, %s, %s, %s)
            ON CONFLICT (ticker) DO NOTHING
        ''', [(c['ticker'], c['name'], c['sector'], c['isin']) for c in companies])


def insert_document(conn, document, pages, metrics=None):
    columns = ', '.join(DOCUMENT_COLUMNS)
    placeholders = ', '.join(['%s'] * len(DOCUMENT_COLUMNS))
    saved = conn.execute(
        f'INSERT INTO documents ({columns}) VALUES ({placeholders}) ON CONFLICT (raw_hash) DO NOTHING RETURNING id',
        tuple(document.get(column) for column in DOCUMENT_COLUMNS),
    ).fetchone()
    if not saved:
        return False
    document = {**document, 'id': saved[0]}
    with conn.cursor() as cur:
        cur.executemany('''
            INSERT INTO document_chunks (document_id, page_number, content, table_markdown, embedding)
            VALUES (%s, %s, %s, %s, %s::vector)
        ''', [(saved[0], p['page_number'], p['content'], p['table_markdown'],
               str(vector) if (vector := embedding(p['content'] + ' ' + p['table_markdown'])) else None) for p in pages])
        rows = extract_metrics(document, pages) if metrics is None else metrics
        columns = ', '.join(METRIC_COLUMNS)
        placeholders = ', '.join(['%s'] * len(METRIC_COLUMNS))
        cur.executemany(f'INSERT INTO financial_metrics ({columns}) VALUES ({placeholders})', [
            tuple(saved[0] if key == 'source_doc_id' else row.get(key) for key in METRIC_COLUMNS) for row in rows
        ])
    return True


def load_demo_database(corpus):
    with connection() as conn:
        upsert_companies(conn, corpus['companies'])
        for doc in corpus['documents']:
            insert_document(conn, doc,
                            [page for page in corpus['chunks'] if page['document_id'] == doc['id']],
                            [row for row in corpus['metrics'] if row['source_doc_id'] == doc['id']])


def sync_live(manifest=None, ocr=False, recent_only=False):
    entries = [] if recent_only else list(KNOWN)
    if manifest:
        entries = json.loads(manifest.read_text())
        if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
            raise ValueError('Le manifeste doit contenir une liste d’objets.')
    imported = duplicates = failed = scanned = 0
    attempted = set()
    with connection() as conn:
        upsert_companies(conn, [dict(zip(('ticker', 'name', 'sector', 'isin'), row[:4])) for row in ISSUERS])
        upsert_companies(conn, [dict(ticker='AFI', name='Afric Industries', sector='Industrie', isin=None)])
        issuers = conn.execute('SELECT ticker,name,sector,isin FROM companies ORDER BY length(name) DESC').fetchall()
        conn.commit()
        if not manifest:
            for url, ammc in LISTINGS:
                try:
                    entries.extend(discover(url, ammc, issuers))
                except (OSError, ValueError) as exc:
                    failed += 1
                    print(f'Catalogue inaccessible ({url}) · {exc}', file=sys.stderr)
        existing = {row[0] for row in conn.execute('SELECT source_url FROM documents WHERE NOT is_demo')} if recent_only else set()
        conn.commit()
        for entry in entries:
            url = entry.get('source_url')
            if not isinstance(url, str) or url in attempted:
                if not isinstance(url, str):
                    failed += 1
                    print('Document ignoré · URL absente.', file=sys.stderr)
                continue
            attempted.add(url)
            if url in existing:
                duplicates += 1
                continue
            try:
                if entry['doc_type'] not in DOC_TYPES or not entry['title']:
                    raise ValueError('Type ou titre invalide.')
                if entry.get('published_at'):
                    date.fromisoformat(entry['published_at'])
                raw = download(url)
                digest = sha256(raw).hexdigest()
                with conn.transaction():
                    if conn.execute('SELECT 1 FROM documents WHERE raw_hash = %s', (digest,)).fetchone():
                        duplicates += 1
                        continue
                    pages = extract_pdf(raw, ocr=ocr)
                    if not pages:
                        scanned += 1
                        print(f'PDF sans texte exploitable, ignoré ({url})', file=sys.stderr)
                        continue
                    if insert_document(conn, {**entry, 'raw_hash': digest, 'is_demo': False}, pages):
                        imported += 1
                    else:
                        duplicates += 1
            except Exception as exc:
                failed += 1
                print(f'Document ignoré ({url}) · {exc}', file=sys.stderr)
    print(f'{imported} document(s) indexé(s) · {duplicates} doublon(s) · {scanned} PDF sans texte · {failed} échec(s)')
    return 1 if failed or (recent_only and scanned) or not (imported or duplicates) else 0



def refresh_financial_metrics():
    with connection() as conn:
        from psycopg.rows import dict_row
        with conn.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT id, doc_type, company_id, fiscal_year, source_url FROM documents WHERE NOT is_demo AND doc_type='rapport_annuel'")
            documents = cursor.fetchall()
            for doc in documents:
                cursor.execute('SELECT page_number, content, table_markdown FROM document_chunks WHERE document_id=%s', (doc['id'],))
                rows = extract_metrics(doc, cursor.fetchall())
                columns = ', '.join(METRIC_COLUMNS)
                placeholders = ', '.join(['%s'] * len(METRIC_COLUMNS))
                mutable = METRIC_COLUMNS[2:-2]
                updates = ', '.join(f'{key}=COALESCE(EXCLUDED.{key},financial_metrics.{key})' for key in mutable)
                cursor.executemany(f'''INSERT INTO financial_metrics ({columns}) VALUES ({placeholders})
                    ON CONFLICT(company_id,fiscal_year,source_doc_id,source_page) DO UPDATE SET {updates}''',
                    [tuple(row.get(key) for key in METRIC_COLUMNS) for row in rows])
    print(f'{len(documents)} rapport(s) relu(s).')
    return 0


def main():
    parser = argparse.ArgumentParser(description='Ingestion documentaire MarketLens AI')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--seed-demo', action='store_true')
    mode.add_argument('--sync-live', action='store_true')
    mode.add_argument('--refresh-financials', action='store_true', help='Relire les pages de rapports annuels déjà indexées.')
    mode.add_argument('--sync-history', action='store_true', help='Importer les trois années publiques de cours ajustés et du MASI.')
    mode.add_argument('--sync-issuers', action='store_true', help='Importer les chiffres clés et dividendes des fiches officielles.')
    mode.add_argument('--sync-market', action='store_true', help='Indexer les bulletins de cote officiels.')
    parser.add_argument('--database', action='store_true', help='Copier aussi la démonstration dans PostgreSQL.')
    parser.add_argument('--manifest', type=Path, help='Limiter la synchronisation à un manifeste JSON.')
    parser.add_argument('--ocr', action='store_true', help='Reconnaître les premières pages scannées avec Tesseract (fra+eng).')
    parser.add_argument('--market-pages', type=int, default=1, help='Nombre de pages de bulletins à parcourir (1 à 25).')
    parser.add_argument('--refresh-market', action='store_true', help='Relire les bulletins déjà indexés.')
    args = parser.parse_args()
    if args.refresh_market and not args.sync_market:
        parser.error('--refresh-market requiert --sync-market.')
    if (args.database and not args.seed_demo) or ((args.manifest or args.ocr) and not args.sync_live):
        parser.error('Options incompatibles avec le mode sélectionné.')
    if args.sync_history:
        from issuer_facts import sync_history
        return sync_history()
    if args.sync_issuers:
        from issuer_facts import sync_issuers
        return sync_issuers()
    if args.refresh_financials:
        return refresh_financial_metrics()
    if args.sync_market:
        from market import sync_market
        if not 1 <= args.market_pages <= 25:
            parser.error('--market-pages doit être compris entre 1 et 25.')
        return sync_market(args.market_pages, refresh=args.refresh_market)
    if args.seed_demo:
        corpus = seed_demo()
        if args.database:
            load_demo_database(corpus)
        print(f"{len(corpus['companies'])} valeurs et {len(corpus['documents'])} PDF de démonstration indexés dans {ROOT.name}.")
        return 0
    if args.ocr and not ocr_available():
        parser.error('--ocr requiert Tesseract (fra et eng) ou Swift et Vision sur macOS.')
    return sync_live(args.manifest, args.ocr)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, psycopg.Error) as exc:
        sys.exit(str(exc))
