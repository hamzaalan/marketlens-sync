from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal
import re
from urllib.parse import urlencode

from market import ACTIONS, MarketCatalog, market_number

ORIGIN = 'https://www.casablanca-bourse.com'
DETAIL = f'{ORIGIN}/live-market/actions/cours'
FUNDAMENTALS = {
    'revenue': 'chiffreAffaire',
    'earnings': 'resultatNet',
    'equity': 'capitalPropre',
    'operating_income': 'resultatExploitation',
    'share_count': 'nombreTitres',
}


def issuer_catalog(data):
    result = {}
    for row in data.get('actions', []):
        ticker, code = row.get('symbol', '').strip(), row.get('ce', '').strip()
        if re.fullmatch(r'[A-Z0-9]{1,12}', ticker) and re.fullmatch(r'[A-Z0-9]{1,25}', code):
            result[ticker] = code
    return result


def valid_year(year):
    return type(year) is int and 1990 <= year <= 2100


def parse_issuer(action, ticker, code, as_of):
    if action.get('symbol', '').strip() != ticker or action.get('ce', '').strip() != code:
        raise ValueError('Identité de la fiche officielle différente de la demande.')
    figures = {}
    for row in action.get('financials_details', {}).get('chiffres', []):
        year = row.get('exercice')
        if not valid_year(year) or row.get('codeEmetteur') != code or not isinstance(row.get('compteConsolide'), bool):
            continue
        values = {}
        for key, field in FUNDAMENTALS.items():
            value = row.get(field)
            if key in ('earnings', 'equity', 'operating_income') and isinstance(value, (int, float)) and value < 0:
                amount = market_number(-value)
                value = -amount if amount is not None else None
            else:
                value = market_number(value, zero=key in ('earnings', 'operating_income', 'equity'), integral=key == 'share_count')
            if value is not None:
                values[key] = value
        if values:
            figures[year] = dict(year=year, consolidated=row['compteConsolide'], **values)
    dividends = {}
    seen = set()
    for row in action.get('dividend_details', []):
        year = row.get('exercice')
        value = market_number(row.get('montant'), zero=True)
        if not valid_year(year) or value is None:
            continue
        try:
            ex_date = date.fromisoformat(row['detachement'][:10])
        except (KeyError, ValueError, TypeError):
            continue
        if ex_date > as_of:
            continue
        record = (year, value, ex_date, (row.get('type') or {}).get('fr'))
        if record in seen:
            continue
        seen.add(record)
        amount, last = dividends.get(year, (Decimal(0), ex_date))
        dividends[year] = (amount + Decimal(str(value)), max(last, ex_date))
    return list(figures.values()), [
        dict(year=year, value=float(amount), date=ex_date.isoformat())
        for year, (amount, ex_date) in sorted(dividends.items(), reverse=True)
    ]


def read_catalog(download):
    parser = MarketCatalog()
    parser.feed(download(ACTIONS).decode('utf-8'))
    return issuer_catalog(parser.state())


def sync_issuers():
    from main import connection, download
    from psycopg.types.json import Jsonb
    catalog = read_catalog(download)
    with connection() as conn:
        companies = {row[0] for row in conn.execute('SELECT ticker FROM companies')}
        conn.commit()
        jobs = [(ticker, code) for ticker, code in catalog.items() if ticker in companies]
        if not jobs:
            raise ValueError('Aucune valeur officielle en base. Exécutez --sync-market.')
        as_of = conn.execute('SELECT max(session_date) FROM market_sessions').fetchone()[0]
        conn.commit()
        if as_of is None:
            raise ValueError('Aucune séance datée. Exécutez --sync-market.')

        def get(job):
            ticker, code = job
            url = f'{DETAIL}?{urlencode(dict(ce=code,symbol=ticker))}'
            try:
                parser = MarketCatalog()
                parser.feed(download(url).decode('utf-8'))
                figures, dividends = parse_issuer(parser.state().get('action_data', {}), ticker, code, as_of)
                return ticker, url, figures, dividends, None
            except (ValueError, OSError, AttributeError, TypeError) as exc:
                return ticker, url, [], [], str(exc)

        imported = failed = covered = 0
        with ThreadPoolExecutor(max_workers=4) as executor:
            for ticker, url, figures, dividends, error in executor.map(get, jobs):
                if error:
                    failed += 1
                    print(f'{ticker} · {error}')
                    continue
                conn.execute('''INSERT INTO issuer_facts (ticker,source_url,figures,dividends) VALUES (%s,%s,%s,%s)
                    ON CONFLICT(ticker) DO UPDATE SET source_url=EXCLUDED.source_url,
                    figures=CASE WHEN EXCLUDED.figures='[]'::jsonb THEN issuer_facts.figures ELSE EXCLUDED.figures END,
                    dividends=CASE WHEN EXCLUDED.dividends='[]'::jsonb THEN issuer_facts.dividends ELSE EXCLUDED.dividends END,
                    fetched_at=now()''', (ticker, url, Jsonb(figures), Jsonb(dividends)))
                imported += 1
                covered += bool(figures)
            conn.commit()
    print(f'{imported} fiches officielles lues · {covered} avec chiffres annuels · {failed} erreurs')
    return int(bool(failed))

HISTORY = f'{ORIGIN}/api/boursenova'


def observed_rows(payload, ticker, start, end):
    rows = {}
    for item in payload.get('items', []):
        try:
            day, month, year = map(int, item['seance'].split('/'))
            session = date(year, month, day)
        except (KeyError, AttributeError, ValueError):
            continue
        if not start <= session <= end:
            continue
        if ticker == 'MASI':
            value = item.get('indices', {}).get('MASI', {}).get('valeur')
        elif item.get('symbol', '').strip() == ticker:
            value = item.get('dernierCours')
        else:
            continue
        close = market_number(value)
        if close is not None:
            rows[session] = close
    return rows


def sync_history():
    import json
    from main import connection, download
    catalog = read_catalog(download)
    with connection() as conn:
        end = conn.execute('SELECT max(session_date) FROM market_sessions').fetchone()[0]
        tickers = [ticker for (ticker,) in conn.execute('SELECT ticker FROM companies ORDER BY ticker') if ticker in catalog]
        conn.commit()
        if end is None:
            raise ValueError('Aucune séance datée. Exécutez --sync-market.')
        try:
            start = end.replace(year=end.year - 3)
        except ValueError:
            start = end.replace(year=end.year - 3, day=28)
        # The public feed applies the limit against the current date, including non-trading days.
        start += timedelta(days=10)
        dates = dict(startDate=start.isoformat(), endDate=end.isoformat())

        def get(ticker):
            if ticker == 'MASI':
                uri = f'{HISTORY}/indices/historical?{urlencode(dict(code=ticker, **dates))}'
            else:
                params = dict(instrument=ticker, market='comptant', type='actions', isCoursAjuste='true', target='tv', **dates)
                uri = f'{HISTORY}/stock-historical?{urlencode(params)}'
            try:
                return ticker, uri, observed_rows(json.loads(download(uri)), ticker, start, end), None
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                return ticker, uri, {}, str(exc)

        benchmark, covered, failed = 0, 0, 0
        with ThreadPoolExecutor(max_workers=4) as executor:
            for ticker, source, rows, error in executor.map(get, ['MASI', *tickers]):
                if error:
                    print(f'{ticker} · {error}')
                    failed += 1
                    continue
                if not rows:
                    print(f'{ticker} · aucun cours publié sur la période')
                    continue
                conn.execute('''INSERT INTO observed_prices (ticker,session_date,close_mad,source_url)
                    SELECT %s, price.session_date, price.close, %s
                    FROM unnest(%s::date[],%s::numeric[]) AS price(session_date,close)
                    ON CONFLICT(ticker,session_date) DO UPDATE SET close_mad=EXCLUDED.close_mad,source_url=EXCLUDED.source_url''',
                    (ticker, source, list(rows), [Decimal(str(value)) for value in rows.values()]))
                if ticker == 'MASI':
                    benchmark = len(rows)
                else:
                    covered += 1
                conn.commit()
    print(f'{benchmark} séances du MASI · {covered} historiques actions · {failed} erreurs')
    return int(not benchmark or not covered or bool(failed))
