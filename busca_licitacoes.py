#!/usr/bin/env python3
"""
Busca Licitações (Soluções Comerciais)
--------------------------------------
Licitações abertas do PNCP com filtro por PORTAL (Compras.gov.br, ComprasMG, SISLOG, Licitar Digital...).

Etapa 1 (agora): diagnóstico para descobrir como o PNCP informa o portal de origem de cada licitação.

Como rodar:  python3 busca_licitacoes.py   (no Render: Start Command = python busca_licitacoes.py)
Não precisa instalar nada além do Python 3.8+.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8000"))
PNCP = "https://pncp.gov.br/api/consulta/v1"
UA = "busca-licitacoes/1.0"


def erro_txt(e):
    if isinstance(e, urllib.error.HTTPError):
        corpo = ""
        try:
            corpo = e.read()[:300].decode("utf-8", "replace")
        except Exception:
            pass
        return f"HTTP {e.code} {corpo}"
    if isinstance(e, urllib.error.URLError):
        return f"sem conexão ({e.reason})"
    return str(e)[:200]


def http_get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    if not data.strip():
        return None
    return json.loads(data.decode("utf-8"))


def dominio(url):
    try:
        return urllib.parse.urlparse(str(url or "")).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def diagnostico():
    """Lê algumas páginas de licitações abertas e conta os portais de origem."""
    linhas = ["DIAGNOSTICO BUSCA LICITACOES", ""]
    fim = (date.today() + timedelta(days=60)).strftime("%Y%m%d")
    usuarios, dominios, exemplos = Counter(), Counter(), {}
    primeiro = None
    for modalidade in (6, 8, 4, 5):  # 6 pregão eletrônico, 8 dispensa, 4 concorrência eletrônica, 5 concorrência presencial
        for pagina in range(1, 6):
            url = f"{PNCP}/contratacoes/proposta?dataFinal={fim}&codigoModalidadeContratacao={modalidade}&pagina={pagina}&tamanhoPagina=50"
            t0 = time.time()
            try:
                d = http_get(url)
            except Exception as e:
                linhas.append(f"modalidade {modalidade} pagina {pagina}: ERRO {erro_txt(e)} ({time.time() - t0:.1f}s)")
                break
            lst = (d or {}).get("data") or []
            if pagina == 1:
                linhas.append(f"modalidade {modalidade}: total={(d or {}).get('totalRegistros')} paginas={(d or {}).get('totalPaginas')} ({time.time() - t0:.1f}s)")
            for c in lst:
                if primeiro is None:
                    primeiro = c
                u = str(c.get("usuarioNome") or "?")
                dm = dominio(c.get("linkSistemaOrigem"))
                usuarios[u] += 1
                dominios[dm] += 1
                exemplos.setdefault(u, c.get("linkSistemaOrigem"))
            if not lst or (d or {}).get("paginasRestantes", 0) <= 0:
                break
            time.sleep(0.3)
    linhas.append("")
    linhas.append("== portais (usuarioNome): quantidade | exemplo de link")
    for u, n in usuarios.most_common(80):
        linhas.append(f"  {n:4d} | {u} | {exemplos.get(u)}")
    linhas.append("")
    linhas.append("== dominios do linkSistemaOrigem")
    for dm, n in dominios.most_common(60):
        linhas.append(f"  {n:4d} | {dm}")
    linhas.append("")
    linhas.append("== campos de um registro (exemplo)")
    if primeiro:
        linhas.append(json.dumps(primeiro, ensure_ascii=False, indent=1)[:4000])
    linhas.append("")
    linhas.append("== busca rapida do site do PNCP (teste)")
    for url in ("https://pncp.gov.br/api/search/?tipos_documento=edital&ordenacao=-data&pagina=1&tam_pagina=5&status=recebendo_proposta",):
        t0 = time.time()
        try:
            d = http_get(url, timeout=40)
            itens = (d or {}).get("items") or (d or {}).get("data") or []
            linhas.append(f"  OK ({time.time() - t0:.1f}s) total={(d or {}).get('total')} chaves={list((d or {}).keys())}")
            if itens:
                linhas.append("  item: " + json.dumps(itens[0], ensure_ascii=False)[:2500])
        except Exception as e:
            linhas.append(f"  ERRO {erro_txt(e)} ({time.time() - t0:.1f}s)")
    return "\n".join(linhas)


PAGINA = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Busca Licitações</title>
<style>body{margin:0;background:#0d1422;color:#e8eef8;font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:640px;margin:0 auto;padding:48px 16px}h1{margin:0 0 8px}p{color:#94a3bb}</style></head>
<body><main><h1>Busca Licitações</h1><p>Em construção. Em breve: licitações abertas com filtro por portal.</p></main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("  " + (fmt % args) + "\n")

    def _send(self, code, body, ctype):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/diagnostico":
            try:
                txt = diagnostico()
            except Exception as e:
                txt = "ERRO: " + erro_txt(e)
            return self._send(200, txt, "text/plain; charset=utf-8")
        if u.path in ("/", "/index.html"):
            return self._send(200, PAGINA, "text/html; charset=utf-8")
        self._send(404, "não encontrado", "text/plain; charset=utf-8")


def main():
    na_nuvem = "PORT" in os.environ
    srv = ThreadingHTTPServer(("0.0.0.0" if na_nuvem else "127.0.0.1", PORT), Handler)
    print(f"Busca Licitações rodando na porta {PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
