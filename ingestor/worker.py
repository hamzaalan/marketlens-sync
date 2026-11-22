import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
from email.message import EmailMessage
import os
import smtplib
import ssl
import sys
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from main import connection, download, sync_live
from market import ACTIONS, MarketCatalog, market_number, sync_market
from issuer_facts import sync_history, sync_issuers
from pipeline import ocr_available

CASA = ZoneInfo('Africa/Casablanca')


def now_ms():
    return int(time.time() * 1000)


def collect_quotes():
    parser = MarketCatalog()
    parser.feed(download(ACTIONS).decode('utf-8'))
    state = parser.state()
    timestamp = state.get('session', {}).get('timestamp')
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        raise ValueError('Horodatage de la séance absent.')
    session = datetime.fromtimestamp(timestamp, CASA).date()
    if session > datetime.now(CASA).date():
        raise ValueError('Horaire de séance invalide.')
    if not state.get('actions'):
        raise ValueError('Liste des valeurs absente du flux officiel.')
    with connection() as conn:
        companies = {row[0] for row in conn.execute('SELECT ticker FROM companies')}
        rows = []
        for row in state.get('actions', []):
            ticker = row.get('symbol', '').strip()
            price = market_number(row.get('dernierCours'))
            status = (row.get('statut') or {}).get('fr')
            if ticker in companies and price is not None and status == 'T' and (row.get('nbTransactions') or 0) > 0:
                rows.append((ticker, Decimal(str(price)), session, now_ms(), status, ACTIONS))
        with conn.cursor() as cursor:
            cursor.executemany('''INSERT INTO latest_quotes (ticker,close_mad,session_date,observed_at,market_status,source_url)
                VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(ticker) DO UPDATE SET
                close_mad=EXCLUDED.close_mad,session_date=EXCLUDED.session_date,observed_at=EXCLUDED.observed_at,
                market_status=EXCLUDED.market_status,source_url=EXCLUDED.source_url
                WHERE latest_quotes.session_date<=EXCLUDED.session_date''', rows)
    return 0


def queue_alerts():
    now = now_ms()
    today = datetime.now(CASA).date()
    public = os.getenv('CASAQUANT_PUBLIC_URL', '').rstrip('/')
    with connection() as conn:
        rows = conn.execute('''SELECT a.id,a.account_id,a.ticker,a.direction,a.threshold,q.close_mad,q.source_url
            FROM price_alerts a JOIN latest_quotes q USING(ticker)
            JOIN mail_verifications v ON v.account_id=a.account_id AND v.verified=1
            WHERE a.state='active' AND q.session_date=%s AND q.observed_at>=GREATEST(a.created_at,%s)
            AND ((a.direction='above' AND q.close_mad*100>=a.threshold)
              OR (a.direction='below' AND q.close_mad*100<=a.threshold))''', (today, now - 600_000)).fetchall()
        for alert, owner, ticker, direction, threshold, price, url in rows:
            claim = conn.execute('''UPDATE price_alerts SET state='queued',triggered_at=%s,triggered_price=%s
                WHERE id=%s AND state='active' RETURNING id''', (now, price, alert)).fetchone()
            if not claim:
                continue
            comparison = 'atteint ou dépassé' if direction == 'above' else 'atteint ou franchi à la baisse'
            body = (f'{ticker} a {comparison} votre seuil de {threshold / 100:.2f} MAD.\n'
                    f'Dernier cours publié : {price:.2f} MAD, séance du {today.isoformat()}.\n'
                    f'Source : {url}\n'
                    'Ce message signale un seuil que vous avez choisi, à partir du flux public différé.')
            if public:
                body += f'\nGérer vos alertes : {public}/alertes'
            conn.execute('''INSERT INTO mail_outbox (id,account_id,alert_id,subject,body,available_at)
                VALUES (%s,%s,%s,%s,%s,%s)''', (str(uuid4()), owner, alert, f'MarketLens · Seuil atteint pour {ticker}', body, now))
    return 0


def smtp_send(mail_id, target, subject, body):
    host, sender = os.getenv('CASAQUANT_SMTP_HOST'), os.getenv('CASAQUANT_SMTP_FROM')
    if not host or not sender:
        raise ValueError('SMTP absent.')
    mode = os.getenv('CASAQUANT_SMTP_SECURITY', 'starttls')
    port = int(os.getenv('CASAQUANT_SMTP_PORT', '465' if mode == 'ssl' else '587'))
    context = ssl.create_default_context()
    if mode not in ('ssl', 'starttls'):
        raise ValueError('Le transport SMTP nécessite TLS.')
    message = EmailMessage()
    message['From'], message['To'], message['Subject'] = sender, target, subject
    message['Message-ID'] = f'<marketlens-{mail_id}@{sender.split("@")[-1]}>'
    message.set_content(body)
    transport = smtplib.SMTP_SSL(host, port, timeout=15, context=context) if mode == 'ssl' else smtplib.SMTP(host, port, timeout=15)
    with transport as client:
        if mode == 'starttls':
            client.starttls(context=context)
        if user := os.getenv('CASAQUANT_SMTP_USER'):
            client.login(user, os.environ['CASAQUANT_SMTP_PASSWORD'])
        client.send_message(message)


def deliver_mail(send=smtp_send):
    if send is smtp_send:
        if os.getenv('CASAQUANT_MAIL_ENABLED') != '1':
            return 0
        if not (os.getenv('CASAQUANT_SMTP_HOST') and os.getenv('CASAQUANT_SMTP_FROM')):
            raise ValueError('SMTP absent.')
    now = now_ms()
    with connection() as conn:
        conn.execute('DELETE FROM mail_outbox WHERE expires_at<=%s AND alert_id IS NULL', (now,))
        rows = conn.execute('''SELECT m.id,m.alert_id,a.email,m.subject,m.body,m.attempts
            FROM mail_outbox m JOIN accounts a ON a.id=m.account_id
            WHERE m.sent_at IS NULL AND m.available_at<=%s AND m.attempts<10
              AND (m.expires_at IS NULL OR m.expires_at>%s)
            ORDER BY m.available_at LIMIT 25''', (now, now)).fetchall()
        for message, alert, target, subject, body, attempts in rows:
            try:
                send(message, target, subject, body)
            except (OSError, smtplib.SMTPException, ValueError, KeyError) as error:
                print(f"Relais SMTP · {type(error).__name__} · code {getattr(error, 'smtp_code', 'indisponible')}", file=sys.stderr)
                delay = min(3600, 30 * 2 ** attempts) * 1000
                conn.execute('UPDATE mail_outbox SET attempts=attempts+1,available_at=%s WHERE id=%s', (now_ms() + delay, message))
                if alert and attempts == 9:
                    conn.execute("UPDATE price_alerts SET state='failed' WHERE id=%s AND state='queued'", (alert,))
                print(f'Livraison e-mail en échec · {message} · tentative {attempts + 1}', file=sys.stderr)
                continue
            conn.execute("UPDATE mail_outbox SET sent_at=%s,body='' WHERE id=%s", (now_ms(), message))
            if alert:
                conn.execute("UPDATE price_alerts SET state='sent' WHERE id=%s AND state='queued'", (alert,))
    return 0


def update_status(name, state):
    with connection() as conn:
        conn.execute('''INSERT INTO synchronization_status (job,status,updated_at) VALUES (%s,%s,%s)
            ON CONFLICT(job) DO UPDATE SET status=EXCLUDED.status,updated_at=EXCLUDED.updated_at''', (name, state, now_ms()))


def job(name, operation):
    try:
        result = operation()
        update_status(name, 'ok' if not result else 'partiel')
        return not result
    except Exception as error:
        print(f'{name} · {error}', file=sys.stderr)
        update_status(name, 'erreur')
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--skip-history', action='store_true', help='Conserver les séries longues existantes pour cette exécution.')
    parser.add_argument('--jobs', nargs='+', choices=('quotes', 'alerts', 'mail', 'publications', 'bulletins', 'fundamentals', 'history'),
                        help='Sélectionner les tâches et leur ordre pour une exécution ponctuelle.')
    args = parser.parse_args()
    if args.jobs and (not args.once or args.skip_history):
        parser.error('--jobs s’utilise avec --once, sans --skip-history.')
    schedule = [
        ('quotes', 60, collect_quotes),
        ('alerts', 35, queue_alerts),
        ('mail', 30, deliver_mail),
        ('publications', 300, lambda: sync_live(ocr=ocr_available(), recent_only=True)),
        ('bulletins', 900, lambda: sync_market(1)),
        ('fundamentals', 4 * 3600, sync_issuers),
    ]
    if not args.skip_history:
        schedule.append(('history', 24 * 3600, sync_history))
    elapsed = {}
    with connection() as lock:
        if not lock.execute('SELECT pg_try_advisory_lock(72931582)').fetchone()[0]:
            return 0
        lock.commit()
        if args.jobs:
            operations = {name: action for name, _, action in schedule}
            results = [job(name, operations[name]) for name in dict.fromkeys(args.jobs)]
            return int(not all(results))
        serial = os.getenv('CASAQUANT_SYNC_SERIAL') == '1'
        with ThreadPoolExecutor(max_workers=1 if serial else len(schedule)) as pool:
            running = {}
            while True:
                for name, interval, action in schedule:
                    if name in running:
                        if not running[name].done():
                            continue
                        ok = running.pop(name).result()
                        if not ok:
                            elapsed[name] = min(elapsed[name], time.monotonic() + 300)
                    if time.monotonic() >= elapsed.get(name, 0):
                        running[name] = pool.submit(job, name, action)
                        elapsed[name] = time.monotonic() + interval
                if args.once:
                    return int(not all([future.result() for future in running.values()]))
                time.sleep(5)


if __name__ == '__main__':
    sys.exit(main())
