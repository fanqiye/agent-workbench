"""Tenant-scoped document search with versioned evidence and request auditing."""

import argparse
from contextlib import closing
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import sqlite3
from urllib.parse import parse_qs, unquote, urlsplit


SCHEMA = """
CREATE TABLE IF NOT EXISTS principals (
 subject TEXT PRIMARY KEY, tenant TEXT NOT NULL, role TEXT NOT NULL,
 token_hash TEXT UNIQUE NOT NULL, enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS documents (
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL, audience TEXT NOT NULL,
 version INTEGER NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
 checksum TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS audit (
 sequence INTEGER PRIMARY KEY, subject TEXT, action TEXT NOT NULL,
 resource_hash TEXT NOT NULL, status INTEGER NOT NULL, result_ids TEXT NOT NULL,
 occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""
IDENTIFIER = r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}'


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def connect(db):
    conn = sqlite3.connect(db, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(IDENTIFIER, value):
        raise ValueError('identifier must be 1-64 ASCII letters, digits, _ or -')
    return value


def init_db(db):
    with closing(connect(db)) as conn, conn:
        conn.executescript(SCHEMA)


def grant(db, subject, tenant, role):
    identifier(subject)
    identifier(tenant)
    if role not in ('support', 'admin'):
        raise ValueError('role must be support or admin')
    token = secrets.token_urlsafe(32)
    with closing(connect(db)) as conn, conn:
        # Re-granting rotates the old token, including for a revoked account.
        conn.execute('INSERT INTO principals(subject,tenant,role,token_hash) VALUES (?,?,?,?) ON CONFLICT(subject) DO UPDATE SET tenant=excluded.tenant,role=excluded.role,token_hash=excluded.token_hash,enabled=1',
                     (subject, tenant, role, sha(token)))
    return token


def revoke(db, subject):
    with closing(connect(db)) as conn, conn:
        if conn.execute('UPDATE principals SET enabled=0 WHERE subject=?', (subject,)).rowcount != 1:
            raise ValueError('subject not found')


def ingest(db, records):
    if not isinstance(records, list) or not 1 <= len(records) <= 1000:
        raise ValueError('expected 1-1000 documents')
    seen = set()
    with closing(connect(db)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        for doc in records:
            if not isinstance(doc, dict) or set(doc) != {'id', 'tenant', 'audience', 'version', 'title', 'body'}:
                raise ValueError('document fields: id, tenant, audience, version, title, body')
            identifier(doc['id'])
            identifier(doc['tenant'])
            if doc['id'] in seen:
                raise ValueError('duplicate document id')
            seen.add(doc['id'])
            if doc['audience'] not in ('support', 'admin') or type(doc['version']) is not int or doc['version'] < 1:
                raise ValueError('invalid audience or version')
            if not isinstance(doc['title'], str) or not 1 <= len(doc['title'].strip()) <= 200:
                raise ValueError('title must have 1-200 characters')
            if not isinstance(doc['body'], str) or not 1 <= len(doc['body'].strip()) <= 100000:
                raise ValueError('body must have 1-100000 characters')
            checksum = sha(json.dumps(doc, ensure_ascii=False, sort_keys=True))
            old = conn.execute('SELECT * FROM documents WHERE id=?', (doc['id'],)).fetchone()
            if old:
                if old['tenant'] != doc['tenant']:
                    raise ValueError('a document cannot move between tenants')
                if old['checksum'] == checksum:
                    continue
                if doc['version'] <= old['version']:
                    raise ValueError('changed content or permissions requires a higher version')
            conn.execute('INSERT INTO documents(id,tenant,audience,version,title,body,checksum) VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET audience=excluded.audience,version=excluded.version,title=excluded.title,body=excluded.body,checksum=excluded.checksum,active=1',
                         tuple(doc[key] for key in ('id', 'tenant', 'audience', 'version', 'title', 'body')) + (checksum,))
    return len(records)


def archive(db, doc_id):
    with closing(connect(db)) as conn, conn:
        if conn.execute('UPDATE documents SET active=0 WHERE id=?', (doc_id,)).rowcount != 1:
            raise ValueError('document not found')


def evidence(row, terms=()):
    lines = row['body'].splitlines()
    first = next((i for i, line in enumerate(lines) if any(term in line.casefold() for term in terms)), 0)
    last = min(first + 3, len(lines))
    return {'id': row['id'], 'title': row['title'], 'version': row['version'],
            'source_sha256': row['checksum'], 'line_start': first + 1, 'line_end': last,
            'quote': '\n'.join(lines[first:last])}


def query(db, token, action, resource):
    if action not in ('search', 'document'):
        raise ValueError('unknown action')
    with closing(connect(db)) as conn, conn:
        # Keep revocation and audit in the same transaction. SQLite serializes
        # these requests; a busier deployment needs a service database.
        conn.execute('BEGIN IMMEDIATE')
        principal = conn.execute('SELECT * FROM principals WHERE token_hash=? AND enabled=1', (sha(token),)).fetchone()
        status, result = 401, {'error': 'unauthorized'}
        ids = []
        if principal:
            clauses = ['tenant=?', 'active=1', "(audience='support' OR ?='admin')"]
            values = [principal['tenant'], principal['role']]
            terms = []
            if action == 'search':
                terms = resource.casefold().split()
                if not 1 <= len(terms) <= 6 or any(not 2 <= len(t) <= 80 for t in terms) or len(resource) > 200:
                    status, result = 400, {'error': 'use 1-6 terms of 2-80 characters; maximum 200 characters'}
                else:
                    # Bind every value. Tenant/role filtering happens before LIMIT.
                    for term in terms:
                        clauses.append('instr(casefold(title || char(10) || body), ?) > 0')
                        values.append(term)
                    conn.create_function('casefold', 1, str.casefold)
                    rows = conn.execute('SELECT * FROM documents WHERE ' + ' AND '.join(clauses) + ' ORDER BY id LIMIT 5', values).fetchall()
                    status, result = 200, {'results': [evidence(row, terms) for row in rows]}
                    ids = [row['id'] for row in rows]
            else:
                clauses.append('id=?')
                values.append(resource)
                row = conn.execute('SELECT * FROM documents WHERE ' + ' AND '.join(clauses), values).fetchone()
                if row:
                    status, result = 200, evidence(row) | {'body': row['body']}
                    ids = [row['id']]
                else:
                    status, result = 404, {'error': 'document not found'}
        conn.execute('INSERT INTO audit(subject,action,resource_hash,status,result_ids) VALUES (?,?,?,?,?)',
                     (principal['subject'] if principal else None, action, sha(resource), status, json.dumps(ids)))
    return status, result


def server(db, port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path == '/health':
                status, result = 200, {'status': 'ok'}
            elif parsed.path == '/search' or parsed.path.startswith('/documents/'):
                auth = self.headers.get('Authorization', '')
                token = auth[7:] if auth.startswith('Bearer ') and len(auth) < 200 else ''
                args = parse_qs(parsed.query, keep_blank_values=True)
                if parsed.path == '/search' and (set(args) != {'q'} or len(args['q']) != 1):
                    status, result = 400, {'error': 'supply exactly one q parameter'}
                else:
                    action = 'search' if parsed.path == '/search' else 'document'
                    resource = args['q'][0] if action == 'search' else unquote(parsed.path[len('/documents/'):])
                    try:
                        status, result = query(db, token, action, resource)
                    except sqlite3.Error:
                        status, result = 503, {'error': 'storage unavailable'}
            else:
                status, result = 404, {'error': 'not found'}
            raw = json.dumps(result, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(raw)
    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='runtime/evidence.sqlite3')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init')
    load = commands.add_parser('ingest')
    load.add_argument('file')
    create = commands.add_parser('grant')
    create.add_argument('--subject', required=True)
    create.add_argument('--tenant', required=True)
    create.add_argument('--role', required=True, choices=['support', 'admin'])
    remove = commands.add_parser('revoke')
    remove.add_argument('--subject', required=True)
    delete = commands.add_parser('archive')
    delete.add_argument('--id', required=True)
    serve = commands.add_parser('serve')
    serve.add_argument('--port', type=int, default=8792)
    commands.add_parser('audit')
    args = parser.parse_args()
    try:
        if args.command == 'init':
            Path(args.db).parent.mkdir(parents=True, exist_ok=True)
            init_db(args.db)
            print('Initialized.')
        elif args.command == 'ingest':
            print(json.dumps({'processed': ingest(args.db, json.loads(Path(args.file).read_text(encoding='utf-8')))}))
        elif args.command == 'grant':
            print(grant(args.db, args.subject, args.tenant, args.role))
        elif args.command == 'revoke':
            revoke(args.db, args.subject)
            print('Revoked.')
        elif args.command == 'archive':
            archive(args.db, args.id)
            print('Archived.')
        elif args.command == 'audit':
            with closing(connect(args.db)) as conn:
                print(json.dumps([dict(r) for r in conn.execute('SELECT * FROM audit ORDER BY sequence DESC LIMIT 100')], indent=2))
        else:
            service = server(args.db, args.port)
            print(f'Listening on http://127.0.0.1:{service.server_port}', flush=True)
            try:
                service.serve_forever()
            finally:
                service.server_close()
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2, str(exc) + '\n')


if __name__ == '__main__':
    main()
