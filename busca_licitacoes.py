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


SEARCH = "https://pncp.gov.br/api/search/"


def diagnostico_busca():
    """Testa os filtros da busca rápida do PNCP e conta os portais escritos entre [colchetes]."""
    linhas = ["DIAGNOSTICO BUSCA RAPIDA", ""]
    base = {"tipos_documento": "edital", "status": "recebendo_proposta", "pagina": 1}
    testes = [
        ("tam 100", {"tam_pagina": 100, "ordenacao": "-data"}),
        ("tam 500", {"tam_pagina": 500, "ordenacao": "-data"}),
        ("uf DF", {"tam_pagina": 5, "ufs": "DF"}),
        ("uf MG (campo uf)", {"tam_pagina": 5, "uf": "MG"}),
        ("modalidade 6", {"tam_pagina": 5, "modalidades": "6"}),
        ("q=cadeira", {"tam_pagina": 5, "q": "cadeira"}),
        ("q=\"Compras.gov.br\"", {"tam_pagina": 5, "q": "\"Compras.gov.br\""}),
        ("ordem data_fim", {"tam_pagina": 5, "ordenacao": "data_fim_vigencia"}),
        ("ordem -valor", {"tam_pagina": 5, "ordenacao": "-valor_global"}),
        ("ordem relevancia", {"tam_pagina": 5, "ordenacao": "-relevancia"}),
    ]
    prefixos = Counter()
    for nome, extra in testes:
        q = dict(base, **extra)
        url = SEARCH + "?" + urllib.parse.urlencode(q)
        t0 = time.time()
        try:
            d = http_get(url, timeout=40) or {}
            itens = d.get("items") or []
            amostra = "; ".join(f"{i.get('uf')} {i.get('modalidade_licitacao_id')} fim={str(i.get('data_fim_vigencia'))[:10]} {str(i.get('orgao_nome'))[:25]}" for i in itens[:3])
            linhas.append(f"{nome}: {time.time() - t0:.1f}s total={d.get('total')} veio={len(itens)} | {amostra}")
            for i in itens:
                m = re.match(r"\s*\[([^\]]{2,120})\]", str(i.get("description") or ""))
                prefixos[m.group(1) if m else "(sem colchetes)"] += 1
        except Exception as e:
            linhas.append(f"{nome}: ERRO {erro_txt(e)} ({time.time() - t0:.1f}s)")
        time.sleep(0.3)
    linhas.append("")
    linhas.append("== portais escritos entre [colchetes] na descricao (amostra)")
    for p_, n in prefixos.most_common(60):
        linhas.append(f"  {n:4d} | {p_}")
    for termo in ("sislog", "licitar", "minas gerais", "compras.gov"):
        try:
            d = http_get(SEARCH + "?" + urllib.parse.urlencode(dict(base, tam_pagina=3, q=termo)), timeout=40) or {}
            linhas.append(f"q={termo}: total={d.get('total')} | " + " || ".join(str(i.get("description"))[:90] for i in (d.get("items") or [])))
        except Exception as e:
            linhas.append(f"q={termo}: ERRO {erro_txt(e)}")
    return "\n".join(linhas)


# ================================================================ busca
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor

MODALIDADES = {
    6: "Pregão Eletrônico", 8: "Dispensa", 4: "Concorrência Eletrônica", 7: "Pregão Presencial",
    5: "Concorrência Presencial", 12: "Credenciamento", 9: "Inexigibilidade", 1: "Leilão Eletrônico",
    13: "Leilão Presencial", 3: "Concurso", 2: "Diálogo Competitivo", 11: "Pré-qualificação",
    10: "Manifestação de Interesse",
}
MODALIDADES_PADRAO = (6, 8, 4, 7, 5, 12)

# Portais que a usuária mais usa: nome que aparece no PNCP ou endereço do link -> nome curto
PORTAIS_CONHECIDOS = [
    ("Compras.gov.br", ("compras.gov.br",), ("cnetmobile.estaleiro.serpro.gov.br", "comprasnet.gov.br", "compras.gov.br/")),
    ("ComprasMG", ("compras do estado de minas gerais", "comprasmg"), ("compras.mg.gov.br",)),
    ("Licitar Digital", ("licitar digital", "libre solucoes em tecnologia"), ("licitardigital.com.br",)),
    ("SISLOG", ("sislog", "estado de goias", "administracao de goias", "governo de goias"), ("sislog", "comprasnet.go.gov.br", ".go.gov.br")),
    ("BLL Compras", ("bll compras",), ("bllcompras.com",)),
    ("Licitanet", ("licitanet",), ("licitanet.com.br",)),
    ("Portal de Compras Públicas", ("ecustomize", "portal de compras publicas"), ("portaldecompraspublicas.com.br",)),
    ("BNC Compras", ("bolsa nacional de compras",), ("bnccompras.com",)),
    ("Compras RJ", ("compras publicas do estado do rio de janeiro",), ("compras.rj.gov.br",)),
    ("Banrisul (RS)", ("procergs",), ("pregaobanrisul.com.br", "compras.rs.gov.br")),
]


def sem_acento(s):
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()


def portal_de(c):
    usuario = sem_acento(c.get("usuarioNome"))
    links = " ".join(str(c.get(k) or "").lower() for k in ("linkSistemaOrigem", "linkProcessoEletronico"))
    for nome, nomes, dominios in PORTAIS_CONHECIDOS:
        if any(n in usuario for n in nomes) or any(d in links for d in dominios):
            return nome
    return str(c.get("usuarioNome") or "Não informado").strip()[:60]


def link_comprasgov(c):
    """Link da página pública de acompanhamento no Compras.gov.br (pregão e dispensa)."""
    uasg = re.sub(r"\D", "", str((c.get("unidadeOrgao") or {}).get("codigoUnidade") or ""))
    num = re.sub(r"\D", "", str(c.get("numeroCompra") or ""))
    mod = {6: "05", 8: "06"}.get(c.get("modalidadeId"))
    if len(uasg) != 6 or not num or not mod or not c.get("anoCompra"):
        return ""
    return ("https://cnetmobile.estaleiro.serpro.gov.br/comprasnet-web/public/compras/acompanhamento-compra?compra="
            f"{uasg}{mod}{int(num):05d}{c['anoCompra']}")


def resumir(c, portal):
    org, und = c.get("orgaoEntidade") or {}, c.get("unidadeOrgao") or {}
    cnpj = re.sub(r"\D", "", str(org.get("cnpj") or ""))
    controle = str(c.get("numeroControlePNCP") or "")
    pncp = ""
    m = re.match(r"^(\d{14})-\d+-(\d+)/(\d{4})$", controle)
    if m:
        pncp = f"https://pncp.gov.br/app/editais/{m.group(1)}/{m.group(3)}/{int(m.group(2))}"
    origem = c.get("linkSistemaOrigem") or ""
    if origem and not str(origem).startswith("http"):
        origem = "https://" + str(origem)
    return {
        "id": controle,
        "numero": f"{c.get('numeroCompra') or ''}/{c.get('anoCompra') or ''}".strip("/"),
        "modalidade": c.get("modalidadeId"),
        "modalidadeNome": c.get("modalidadeNome") or MODALIDADES.get(c.get("modalidadeId"), ""),
        "disputa": c.get("modoDisputaNome") or "",
        "srp": bool(c.get("srp")),
        "orgao": org.get("razaoSocial") or "",
        "cnpj": cnpj,
        "unidade": und.get("nomeUnidade") or "",
        "uasg": str(und.get("codigoUnidade") or ""),
        "municipio": und.get("municipioNome") or "",
        "uf": und.get("ufSigla") or "",
        "objeto": re.sub(r"\s+", " ", str(c.get("objetoCompra") or "")).strip(),
        "valor": c.get("valorTotalEstimado"),
        "abertura": str(c.get("dataAberturaProposta") or "")[:16],
        "encerramento": str(c.get("dataEncerramentoProposta") or "")[:16],
        "publicacao": str(c.get("dataPublicacaoPncp") or "")[:16],
        "portal": portal,
        "complemento": re.sub(r"\s+", " ", str(c.get("informacaoComplementar") or "")).strip()[:1500],
        "processo": str(c.get("processo") or ""),
        "amparo": (c.get("amparoLegal") or {}).get("nome") or "",
        "esfera": {"F": "Federal", "E": "Estadual", "M": "Municipal", "D": "Distrital"}.get(org.get("esferaId"), ""),
        "poder": {"E": "Executivo", "L": "Legislativo", "J": "Judiciário", "N": "Não se aplica"}.get(org.get("poderId"), ""),
        "situacao": c.get("situacaoCompraNome") or "",
        "linkPncp": pncp,
        "linkPortal": origem or (link_comprasgov(c) if portal == "Compras.gov.br" else ""),
    }


def _pagina_proposta(uf, modalidade, pagina, tentativas=3):
    fim = (date.today() + timedelta(days=120)).strftime("%Y%m%d")
    url = (f"{PNCP}/contratacoes/proposta?dataFinal={fim}&codigoModalidadeContratacao={modalidade}"
           f"&uf={uf}&pagina={pagina}&tamanhoPagina=50")
    for t in range(1, tentativas + 1):
        try:
            return http_get(url, timeout=45) or {}
        except urllib.error.HTTPError as e:
            if e.code == 204:
                return {}
            if e.code < 500 and e.code != 429:
                raise
        except Exception:
            if t == tentativas:
                raise
        time.sleep(1.5 * t)
    return {}


MAX_PAGINAS = 30  # até 1.500 licitações por modalidade em cada estado
_cache, _lock = {}, threading.Lock()


def buscar_modalidade(uf, modalidade):
    chave = f"{uf}|{modalidade}"
    with _lock:
        if chave in _cache and time.time() - _cache[chave][0] < 1800:
            return _cache[chave][1]
    primeira = _pagina_proposta(uf, modalidade, 1)
    itens = list(primeira.get("data") or [])
    total = primeira.get("totalRegistros") or len(itens)
    paginas = min(MAX_PAGINAS, primeira.get("totalPaginas") or 1)
    cortado = (primeira.get("totalPaginas") or 1) > MAX_PAGINAS
    if paginas > 1:
        with ThreadPoolExecutor(max_workers=3) as ex:  # poucos ao mesmo tempo, para o PNCP não cortar
            for r in ex.map(lambda pg: _pagina_proposta(uf, modalidade, pg), range(2, paginas + 1)):
                itens += r.get("data") or []
    agora = time.strftime("%Y-%m-%dT%H:%M")
    lista = []
    for c in itens:
        r = resumir(c, portal_de(c))
        if r["encerramento"] and r["encerramento"] < agora:
            continue  # prazo de proposta já acabou
        lista.append(r)
    res = {"itens": lista, "total": total, "cortado": cortado}
    with _lock:
        _cache[chave] = (time.time(), res)
    return res


PNCP_API = "https://pncp.gov.br/api/pncp/v1"


def _get_tentando(url, tentativas=3):
    for t in range(1, tentativas + 1):
        try:
            return http_get(url, timeout=40)
        except urllib.error.HTTPError as e:
            if e.code in (204, 404):
                return None
            if e.code in (301, 302, 307, 308):
                raise
            if e.code < 500 and e.code != 429:
                raise
        except Exception:
            if t == tentativas:
                raise
        time.sleep(1.5 * t)
    return None


def _get_bases(caminho):
    """O PNCP mudou alguns endereços de /api/pncp/v1 para /api/consulta/v1: tenta os dois."""
    ultimo = None
    for base in (PNCP, PNCP_API):
        try:
            r = _get_tentando(base + caminho)
            if r is not None:
                return r
        except urllib.error.HTTPError as e:
            ultimo = e
            if e.code not in (301, 302, 307, 308, 400, 404, 405):
                raise
    if ultimo is not None and ultimo.code not in (301, 302, 307, 308, 404):
        raise ultimo
    return None


def detalhes(id_pncp):
    """Documentos (edital, TR, anexos) e itens de uma licitação, direto do PNCP."""
    m = re.match(r"^(\d{14})-\d+-(\d+)/(\d{4})$", id_pncp or "")
    if not m:
        raise ValueError("Licitação inválida.")
    cnpj, seq, ano = m.group(1), int(m.group(2)), m.group(3)
    chave = f"det|{id_pncp}"
    with _lock:
        if chave in _cache and time.time() - _cache[chave][0] < 3600:
            return _cache[chave][1]
    caminho = f"/orgaos/{cnpj}/compras/{ano}/{seq}"
    base = f"{PNCP_API}{caminho}"
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_arq = ex.submit(_get_bases, f"{caminho}/arquivos")
        f_itens = ex.submit(_get_bases, f"{caminho}/itens?pagina=1&tamanhoPagina=500")
        arquivos, itens, erros = [], [], []
        try:
            for a in (f_arq.result() or []):
                if a.get("statusAtivo") is False:
                    continue
                url = a.get("url") or a.get("uri") or ""
                if not url and a.get("sequencialDocumento") is not None:
                    url = f"{base}/arquivos/{a['sequencialDocumento']}"
                arquivos.append({"titulo": a.get("titulo") or a.get("nomeArquivo") or "Documento",
                                 "tipo": a.get("tipoDocumentoNome") or a.get("tipoDocumentoDescricao") or "",
                                 "data": str(a.get("dataPublicacaoPncp") or "")[:10], "url": url})
        except Exception as e:
            erros.append("documentos: " + erro_txt(e))
        try:
            r = f_itens.result() or []
            r = r.get("data", []) if isinstance(r, dict) else r
            for i in r:
                itens.append({"n": i.get("numeroItem"), "descricao": re.sub(r"\s+", " ", str(i.get("descricao") or "")).strip(),
                              "qtd": i.get("quantidade"), "un": i.get("unidadeMedida") or "",
                              "vu": i.get("valorUnitarioEstimado"), "vt": i.get("valorTotal"),
                              "tipo": i.get("materialOuServicoNome") or "",
                              "me": i.get("tipoBeneficioNome") or "", "situacao": i.get("situacaoCompraItemNome") or ""})
        except Exception as e:
            erros.append("itens: " + erro_txt(e))
    res = {"arquivos": arquivos, "itens": sorted(itens, key=lambda x: x["n"] or 0), "erros": erros}
    if not erros:
        with _lock:
            _cache[chave] = (time.time(), res)
    return res


def licitacao(id_pncp):
    """Dados completos de uma licitação (para a página de detalhes)."""
    m = re.match(r"^(\d{14})-\d+-(\d+)/(\d{4})$", id_pncp or "")
    if not m:
        raise ValueError("Licitação inválida.")
    cnpj, seq, ano = m.group(1), int(m.group(2)), m.group(3)
    chave = f"lic|{id_pncp}"
    with _lock:
        if chave in _cache and time.time() - _cache[chave][0] < 1800:
            return _cache[chave][1]
    c = _get_bases(f"/orgaos/{cnpj}/compras/{ano}/{seq}")
    if not c:
        raise ValueError("Não encontrei essa licitação no PNCP.")
    res = resumir(c, portal_de(c))
    res["id"] = res["id"] or id_pncp
    with _lock:
        _cache[chave] = (time.time(), res)
    return res


CONSULTA_API = os.environ.get("CONSULTA_PAGADOR", "https://consulta-orgaos.onrender.com") + "/api/consulta?cnpj="
_vaga_score = threading.Semaphore(2)  # no máximo 2 consultas ao mesmo tempo no Consulta Pagador


def nota_orgao(cnpj):
    """Pergunta ao Consulta Pagador a nota do órgão e guarda por 24 horas."""
    cnpj = re.sub(r"\D", "", cnpj or "")
    if len(cnpj) != 14:
        raise ValueError("CNPJ inválido.")
    chave = f"nota|{cnpj}"
    with _lock:
        if chave in _cache and time.time() - _cache[chave][0] < 86400:
            return _cache[chave][1]
    with _vaga_score:
        with _lock:  # outra pessoa pode ter buscado enquanto esperava a vaga
            if chave in _cache and time.time() - _cache[chave][0] < 86400:
                return _cache[chave][1]
        d = http_get(CONSULTA_API + cnpj, timeout=150) or {}
    a = d.get("avaliacao") or {}
    res = {"nivel": a.get("nivel") or ("privado" if d.get("esfera") == "privado" else "sem_dados"),
           "titulo": a.get("titulo") or "", "score": a.get("score"),
           "prazo": (a.get("prazo") or {}).get("dias")}
    with _lock:
        _cache[chave] = (time.time(), res)
    return res


def buscar(uf, modalidades):
    uf = re.sub(r"[^A-Z]", "", uf.upper())[:2]
    if len(uf) != 2:
        raise ValueError("Escolha um estado.")
    mods = [m for m in modalidades if m in MODALIDADES] or list(MODALIDADES_PADRAO)
    itens, erros, cortadas, vistos = [], [], [], set()
    with ThreadPoolExecutor(max_workers=2) as ex:
        futuros = {m: ex.submit(buscar_modalidade, uf, m) for m in mods}
        for m, f in futuros.items():
            try:
                r = f.result()
                for i in r["itens"]:
                    if i["id"] not in vistos:
                        vistos.add(i["id"])
                        itens.append(i)
                if r["cortado"]:
                    cortadas.append(MODALIDADES[m])
            except Exception as e:
                erros.append(f"{MODALIDADES[m]}: {erro_txt(e)}")
    return {"uf": uf, "itens": itens, "erros": erros, "cortadas": cortadas,
            "atualizado": time.strftime("%d/%m/%Y %H:%M")}


PAGINA = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Busca Licitações</title>
<link rel="icon" type="image/png" sizes="64x64" href="data:image/png;base64,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">
<link rel="apple-touch-icon" href="data:image/png;base64,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">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;600&display=swap">
<style>
/* Layout: cabeçalho com logo, barra de busca, filtros à esquerda e cartões de licitação à direita */
:root{
  --bg:#0d1422; --surf:#151f32; --surf2:#1b2740; --line:#26344f; --line2:#31415f;
  --ink:#e8eef8; --muted:#94a3bb; --dim:#6b7a93;
  --accent:#38bdf8; --accent-ink:#06243a; --accent-soft:#12324a;
  --gold:#e6b032; --gold-soft:#3a2c0c;
  --good:#3ecf8e; --good-bg:#0f2e24; --warn:#f5b94a; --warn-bg:#33270f; --bad:#f6766f; --bad-bg:#371a1c;
  --font:"Plus Jakarta Sans",system-ui,-apple-system,"Segoe UI",sans-serif; --mono:"JetBrains Mono",ui-monospace,Consolas,monospace;
  color-scheme:dark;
}
*{box-sizing:border-box}
[hidden]{display:none!important}
html{background:var(--bg)}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 var(--font);-webkit-font-smoothing:antialiased}
.wrap{max-width:1240px;margin:0 auto;padding-block:22px 60px;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));display:grid;gap:18px}
a{color:var(--accent)}
:focus-visible{outline:2px solid var(--gold);outline-offset:2px}
button{font-family:inherit}

.top{display:flex;align-items:center;gap:18px;flex-wrap:wrap}
.marca{height:64px;width:auto;max-width:100%;display:block}
.top-t{border-left:1px solid var(--line2);padding-left:18px;min-width:0}
.top h1{margin:0;font-size:22px;font-weight:800}
.top p{margin:0;color:var(--muted);font-size:13.5px}

.busca{display:flex;gap:0;background:var(--surf);border:1px solid var(--line2);border-radius:999px;overflow:hidden;box-shadow:0 6px 24px rgba(0,0,0,.25)}
.busca .q{flex:1 1 auto;min-width:0;display:flex;align-items:center;gap:10px;padding:0 18px}
.busca .q svg{color:var(--dim);flex:none}
.busca input{flex:1;min-width:0;border:0;background:transparent;color:var(--ink);font:500 16px var(--font);padding:15px 0}
.busca input::placeholder{color:var(--dim)}
.busca input:focus{outline:none}
.busca select{border:0;border-left:1px solid var(--line2);background:var(--surf2);color:var(--ink);font:600 14px var(--font);padding:0 14px;max-width:200px}
.busca button{border:0;background:var(--accent);color:var(--accent-ink);font:800 15px var(--font);padding:0 28px;cursor:pointer}
.busca button:disabled{opacity:.6;cursor:wait}

.grade{display:grid;grid-template-columns:270px minmax(0,1fr);gap:20px;align-items:start}
aside{background:var(--surf);border:1px solid var(--line);border-radius:14px;padding:6px 0;position:sticky;top:12px;max-height:calc(100vh - 24px);overflow:auto}
aside details{border-bottom:1px solid var(--line);padding:10px 16px}
aside details:last-child{border-bottom:0}
aside summary{cursor:pointer;font:700 11.5px var(--font);letter-spacing:.08em;text-transform:uppercase;color:var(--muted);list-style:none;display:flex;justify-content:space-between;align-items:center;padding:4px 0}
aside summary::after{content:"▾";font-size:12px}
aside details:not([open]) summary::after{content:"▸"}
aside summary::-webkit-details-marker{display:none}
.opc{display:flex;align-items:center;gap:8px;padding:5px 0;font-size:14px;cursor:pointer}
.opc input{accent-color:var(--gold);width:16px;height:16px;flex:none}
.opc .n{margin-left:auto;font:600 12px var(--mono);color:var(--dim)}
.opc.meu span:first-of-type{font-weight:700}
.sub{font-size:11.5px;color:var(--dim);margin:8px 0 2px;text-transform:uppercase;letter-spacing:.06em}
.mini-bt{background:none;border:0;color:var(--accent);font:600 12.5px var(--font);padding:2px 0;cursor:pointer}
.ordem{display:grid;gap:4px;margin-top:4px}
.ordem button{text-align:left;border:0;background:transparent;color:var(--ink);font:500 14px var(--font);padding:7px 10px;border-radius:8px;cursor:pointer}
.ordem button[aria-pressed="true"]{background:var(--accent);color:var(--accent-ink);font-weight:700}
.data-box{display:grid;gap:8px;margin-top:6px}
.data-box input[type=date]{width:100%;min-width:0;background:var(--bg);border:1px solid var(--line2);border-radius:8px;color:var(--ink);font:600 14px var(--font);padding:8px 10px;color-scheme:dark}
.data-box .modo{display:flex;gap:6px}
.data-box .modo button,.atalhos button{flex:1;font:600 12.5px var(--font);padding:6px 8px;border-radius:8px;border:1px solid var(--line2);background:transparent;color:var(--muted);cursor:pointer}
.data-box .modo button[aria-pressed="true"]{background:var(--gold);border-color:var(--gold);color:#241a00}
.atalhos{display:flex;gap:6px;flex-wrap:wrap}
.atalhos button{flex:0 0 auto}
.atalhos button:hover,.data-box .modo button:hover{border-color:var(--accent);color:var(--ink)}
.vals{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:6px}
.vals input{width:100%;min-width:0;background:var(--bg);border:1px solid var(--line2);border-radius:8px;color:var(--ink);font:500 13.5px var(--font);padding:8px}

.res{display:grid;gap:12px;min-width:0}
.cab{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;color:var(--muted)}
.cab b{color:var(--ink);font-size:17px}
.cab .at{margin-left:auto;font-size:12.5px;color:var(--dim)}
.aviso{background:var(--warn-bg);border:1px solid #6e5317;border-radius:10px;padding:10px 14px;font-size:13.5px}
.erro{background:var(--bad-bg);border:1px solid #7a2f2f;border-radius:10px;padding:12px 16px}

.card{background:var(--surf);border:1px solid var(--line);border-radius:14px;padding:16px 18px;display:grid;grid-template-columns:minmax(0,1fr) 220px;gap:10px 22px}
.card:hover{border-color:var(--line2)}
.card.meu{border-left:4px solid var(--gold)}
.tags{display:flex;gap:6px;flex-wrap:wrap;grid-column:1}
.tag{font:600 12px var(--font);padding:3px 9px;border-radius:7px;border:1px solid var(--line2);color:var(--muted);background:var(--surf2);white-space:nowrap}
.tag.num{font-family:var(--mono);color:var(--ink)}
.tag.mod{color:#9cc8ff;border-color:#2b4a74;background:#12243d}
.tag.srp{color:#c9a7ff;border-color:#4b3a74;background:#211a3d}
.tag.prazo{color:var(--warn);border-color:#6e5317;background:var(--warn-bg)}
.tag.prazo.urg{color:var(--bad);border-color:#7a2f2f;background:var(--bad-bg)}
.tag.portal{color:var(--muted)}
.tag.portal.meu{color:var(--gold);border-color:#7a5a12;background:var(--gold-soft)}
.selo-cg{display:inline-flex;align-items:center;gap:6px;font:800 12px var(--font);padding:3px 10px 3px 4px;border-radius:7px;background:#1351b4;color:#fff;border:1px solid #2a6bd1;white-space:nowrap;letter-spacing:.01em}
.selo-cg i{display:inline-grid;place-items:center;width:18px;height:18px;border-radius:5px;background:#ffcd07;color:#0c326f;font-style:normal}
.selo-cg i svg{display:block}
.selo-cg em{font-style:normal;color:#ffcd07}
.corpo{grid-column:1;min-width:0}
.corpo h3{margin:4px 0 2px;font-size:16px;font-weight:800;text-transform:uppercase;letter-spacing:.01em;overflow-wrap:anywhere}
.obj{color:var(--muted);font-size:14px;margin:0;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;cursor:pointer;overflow-wrap:anywhere}
.obj.aberto{-webkit-line-clamp:unset;display:block}
.loc{font-size:12.5px;color:var(--dim);margin-top:6px}
.lado{grid-column:2;grid-row:1 / span 2;display:grid;grid-template-columns:1fr;gap:10px;align-content:start;border-left:1px solid var(--line);padding-left:18px}
.lado span{display:block;font:700 10.5px var(--font);letter-spacing:.08em;text-transform:uppercase;color:var(--dim)}
.lado b{font:600 15px var(--mono);font-variant-numeric:tabular-nums}
.lado .ate b{color:var(--bad)}
.lado .ate b.ok{color:var(--ink)}
.acoes{grid-column:1 / -1;display:flex;gap:8px;flex-wrap:wrap;border-top:1px solid var(--line);padding-top:10px}
.acoes a,.acoes button{font:600 13px var(--font);padding:6px 12px;border-radius:8px;border:1px solid var(--line2);color:var(--ink);background:transparent;text-decoration:none;cursor:pointer}
.acoes a.pri{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.acoes a.pag{color:var(--gold);border-color:#7a5a12}
.nota{display:inline-flex;align-items:center;gap:7px;font:700 13px var(--font);padding:6px 12px;border-radius:8px;border:1px solid var(--line2);color:var(--muted);text-decoration:none;white-space:nowrap}
.nota b{font:800 13px var(--mono)}
.nota .pt{width:9px;height:9px;border-radius:50%;background:var(--dim);flex:none}
.nota.bom{color:var(--good);border-color:#1f6b4c;background:var(--good-bg)}.nota.bom .pt{background:var(--good)}
.nota.regular{color:var(--warn);border-color:#6e5317;background:var(--warn-bg)}.nota.regular .pt{background:var(--warn)}
.nota.ruim{color:var(--bad);border-color:#7a2f2f;background:var(--bad-bg)}.nota.ruim .pt{background:var(--bad)}
.nota.esp .pt{animation:pisca 1s ease-in-out infinite}
@keyframes pisca{50%{opacity:.25}}
@media (prefers-reduced-motion:reduce){.nota.esp .pt{animation:none}}
.acoes a:hover,.acoes button:hover{border-color:var(--accent)}
.det{grid-column:1 / -1;border-top:1px dashed var(--line2);padding-top:12px;display:grid;gap:12px;min-width:0}
.det h4{margin:0 0 6px;font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.docs{display:grid;gap:6px}
.doc{display:flex;gap:10px;align-items:center;background:var(--surf2);border:1px solid var(--line);border-radius:10px;padding:8px 12px;text-decoration:none;color:var(--ink);min-width:0}
.doc:hover{border-color:var(--accent)}
.doc svg{flex:none;color:var(--gold)}
.doc span{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.doc small{margin-left:auto;color:var(--dim);font-size:12px;white-space:nowrap}
.dados{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px 18px;background:var(--surf2);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.dados div{min-width:0}
.dados span{display:block;font:700 10.5px var(--font);letter-spacing:.07em;text-transform:uppercase;color:var(--dim)}
.dados b{font-weight:600;font-size:13.5px;overflow-wrap:anywhere}
.dados b.m{font-family:var(--mono);font-size:12.5px}
.txt{margin:0;color:var(--ink);font-size:14px;white-space:pre-wrap;overflow-wrap:anywhere}
.tipo{font:600 11px var(--font);padding:1px 7px;border-radius:6px;background:var(--surf2);border:1px solid var(--line2);color:var(--muted);margin-left:6px;white-space:nowrap}
.bts{display:flex;gap:8px;flex-wrap:wrap}
.bts button{font:600 13px var(--font);padding:6px 12px;border-radius:8px;border:1px solid var(--line2);color:var(--ink);background:transparent;cursor:pointer}
.bts button:hover{border-color:var(--accent)}
.tw{overflow-x:auto;border:1px solid var(--line);border-radius:10px}
.tw table{border-collapse:collapse;width:100%;font-size:13.5px;font-variant-numeric:tabular-nums}
.tw th,.tw td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:right;vertical-align:top}
.tw th{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--dim);background:var(--surf2);white-space:nowrap}
.tw td.d{text-align:left;min-width:260px;color:var(--ink)}
.tw td.d small{display:block;color:var(--dim)}
.tw tr:last-child td{border-bottom:0}
.tw tfoot td{font-weight:700;background:var(--surf2)}
.corpo h3 a,.obj a{color:inherit;text-decoration:none}
.corpo h3 a:hover,.obj a:hover{color:var(--accent);text-decoration:underline}
.pg{display:grid;gap:18px;min-width:0}
.voltar{color:var(--accent);text-decoration:none;font-weight:600;font-size:14px}
.pg h2{margin:6px 0 4px;font-size:clamp(19px,2.6vw,26px);font-weight:800;line-height:1.3;text-transform:uppercase;overflow-wrap:anywhere}
.pg .org{color:var(--accent);font-weight:700}
.pg .org small{color:var(--muted);font-weight:500}
.quad{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));border:1px solid var(--line);border-radius:12px;overflow:hidden;background:var(--surf)}
.quad div{padding:14px 16px;border-right:1px solid var(--line);min-width:0}
.quad div:first-child{background:var(--surf2)}
.quad span{display:block;font:700 10.5px var(--font);letter-spacing:.08em;text-transform:uppercase;color:var(--dim)}
.quad b{font:600 16px var(--mono);font-variant-numeric:tabular-nums}
.quad .urg{color:var(--bad)}
.quad small{display:block;color:var(--muted);font-size:12.5px;margin-top:2px}
.pg .acoes{border-top:0;padding-top:0}
.secao{background:var(--surf);border:1px solid var(--line);border-radius:14px;padding:16px 18px;display:grid;gap:12px;min-width:0}
.secao>h3{margin:0;font-size:17px;font-weight:800;border-bottom:1px solid var(--line);padding-bottom:8px}
.mais{justify-self:center;font:700 14px var(--font);padding:10px 22px;border-radius:999px;border:1px solid var(--line2);background:var(--surf);color:var(--ink);cursor:pointer}
.vazio{background:var(--surf);border:1px dashed var(--line2);border-radius:14px;padding:30px;text-align:center;color:var(--muted)}
.vazio b{display:block;color:var(--ink);font-size:17px;margin-bottom:6px}
.carregando{display:flex;gap:12px;align-items:center;background:var(--surf);border:1px solid var(--line);border-radius:14px;padding:22px;color:var(--muted)}
.spin{width:20px;height:20px;border:2px solid var(--line2);border-top-color:var(--accent);border-radius:50%;animation:g .8s linear infinite;flex:none}
@keyframes g{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.spin{animation:none}}
footer{font-size:12px;color:var(--dim)}

@media (max-width:900px){
  .grade{grid-template-columns:1fr}
  aside{position:static;max-height:none}
  .card{grid-template-columns:1fr}
  .lado{grid-column:1;grid-row:auto;grid-template-columns:1fr 1fr;border-left:0;padding-left:0;border-top:1px solid var(--line);padding-top:10px}
}
@media (max-width:560px){
  .top-t{border-left:0;padding-left:0}.marca{height:52px}
  .busca{border-radius:14px;flex-wrap:wrap}
  .busca .q{flex:1 1 100%}
  .busca select{flex:1 1 auto;border-left:0;border-top:1px solid var(--line2);padding:12px;max-width:none}
  .busca button{padding:12px 20px}
}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <img class="marca" src="data:image/png;base64,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" alt="Soluções Comerciais - Qualidade em cada negócio" width="170" height="64">
    <div class="top-t"><h1>Busca Licitações</h1><p>Licitações com proposta em aberto no PNCP, com filtro por portal</p></div>
  </div>

  <form class="busca" id="f" autocomplete="off">
    <label class="q"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
      <input id="q" placeholder="O que sua empresa vende? Ex.: material de escritório" aria-label="Palavra-chave"></label>
    <select id="uf" aria-label="Estado"></select>
    <button id="bt" type="submit">Buscar</button>
  </form>

  <div class="grade">
    <aside id="filtros">
      <details open><summary>Portal</summary>
        <div id="f-portal"></div>
      </details>
      <details open><summary>Data da disputa</summary>
        <div class="data-box">
          <input type="date" id="dia" aria-label="Data da disputa (prazo da proposta)">
          <div class="modo" id="modo-dia">
            <button type="button" data-m="no" aria-pressed="true">No dia</button>
            <button type="button" data-m="apartir" aria-pressed="false">A partir do dia</button>
          </div>
          <div class="atalhos" id="atalhos-dia">
            <button type="button" data-d="0">Hoje</button><button type="button" data-d="1">Amanhã</button><button type="button" data-d="3">+3 dias</button><button type="button" data-d="7">+7 dias</button><button type="button" data-d="">Limpar</button>
          </div>
        </div>
      </details>
      <details open><summary>Ordenar por</summary>
        <div class="ordem" id="f-ordem">
          <button type="button" data-o="encerra" aria-pressed="true">Encerra primeiro</button>
          <button type="button" data-o="recentes" aria-pressed="false">Publicadas há menos tempo</button>
          <button type="button" data-o="maior" aria-pressed="false">Maior valor</button>
          <button type="button" data-o="menor" aria-pressed="false">Menor valor</button>
        </div>
      </details>
      <details open><summary>Modalidade</summary>
        <div id="f-mod"></div>
      </details>
      <details><summary>Prazo para proposta</summary>
        <div id="f-prazo"></div>
      </details>
      <details><summary>Valor estimado (R$)</summary>
        <div class="vals"><input id="vmin" inputmode="numeric" placeholder="mínimo" aria-label="Valor mínimo"><input id="vmax" inputmode="numeric" placeholder="máximo" aria-label="Valor máximo"></div>
      </details>
    </aside>

    <main class="res" id="res" aria-live="polite"></main>
  </div>
  <footer>Dados: PNCP, Portal Nacional de Contratações Públicas. Confira sempre o edital no portal antes de participar.</footer>
</div>

<script>
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const brl = (v) => v == null ? "não informado" : "R$ " + Number(v).toLocaleString("pt-BR", {minimumFractionDigits: 2, maximumFractionDigits: 2});
const semAcento = (s) => String(s || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
const CONSULTA = "https://consulta-orgaos.onrender.com/?cnpj=";
const MEUS = ["Compras.gov.br", "ComprasMG", "SISLOG", "Licitar Digital"];
const UFS = [["AC","Acre"],["AL","Alagoas"],["AP","Amapá"],["AM","Amazonas"],["BA","Bahia"],["CE","Ceará"],["DF","Distrito Federal"],["ES","Espírito Santo"],["GO","Goiás"],["MA","Maranhão"],["MT","Mato Grosso"],["MS","Mato Grosso do Sul"],["MG","Minas Gerais"],["PA","Pará"],["PB","Paraíba"],["PR","Paraná"],["PE","Pernambuco"],["PI","Piauí"],["RJ","Rio de Janeiro"],["RN","Rio Grande do Norte"],["RS","Rio Grande do Sul"],["RO","Rondônia"],["RR","Roraima"],["SC","Santa Catarina"],["SP","São Paulo"],["SE","Sergipe"],["TO","Tocantins"]];
const MODS = [[6,"Pregão Eletrônico"],[8,"Dispensa"],[4,"Concorrência Eletrônica"],[7,"Pregão Presencial"],[5,"Concorrência Presencial"],[12,"Credenciamento"],[9,"Inexigibilidade"],[1,"Leilão Eletrônico"]];
const PRAZOS = [["", "Qualquer prazo"], ["2", "Encerra em até 2 dias"], ["7", "Encerra em até 7 dias"], ["30", "Encerra em até 30 dias"]];

let DADOS = null, ORDEM = "encerra", MOSTRAR = 40, PRAZO = "", DIA = "", MODO_DIA = "no";
let PREF = { uf: "DF", portais: ["Compras.gov.br"], mods: [6, 8, 4, 7, 5, 12] };
try { Object.assign(PREF, JSON.parse(localStorage.getItem("busca_pref") || "{}")); } catch (e) {}
function salvar() { try { localStorage.setItem("busca_pref", JSON.stringify(PREF)); } catch (e) {} }

$("#uf").innerHTML = UFS.map(([s, n]) => `<option value="${s}">${s} — ${n}</option>`).join("");
$("#uf").value = PREF.uf;
$("#f-prazo").innerHTML = PRAZOS.map(([v, t], i) => `<label class="opc"><input type="radio" name="prazo" value="${v}"${i === 0 ? " checked" : ""}><span>${t}</span></label>`).join("");

function diasAte(s) {
  if (!s) return null;
  const d = new Date(s.length === 16 ? s + ":00" : s);
  if (isNaN(d)) return null;
  return (d - new Date()) / 86400000;
}
function textoPrazo(dias) {
  if (dias == null) return "";
  if (dias < 1) return "Encerra hoje";
  const n = Math.ceil(dias);
  return n === 1 ? "Falta 1 dia" : `Faltam ${n} dias`;
}
function dataHora(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(s || "");
  return m ? `${m[3]}/${m[2]}/${m[1]} às ${m[4]}:${m[5]}` : "não informado";
}
function numero(v) { const n = parseFloat(String(v || "").replace(/\./g, "").replace(",", ".")); return isNaN(n) ? null : n; }

function contar(lista, campo) { const c = {}; lista.forEach((x) => { c[x[campo]] = (c[x[campo]] || 0) + 1; }); return c; }

function filtrar() {
  if (!DADOS) return [];
  const q = semAcento($("#q").value).trim().split(/\s+/).filter(Boolean);
  const vmin = numero($("#vmin").value), vmax = numero($("#vmax").value);
  const lim = PRAZO ? parseFloat(PRAZO) : null;
  return DADOS.itens.filter((x) =>
    (!PREF.portais.length || PREF.portais.includes(x.portal)) &&
    (!q.length || q.every((p) => semAcento(x.objeto + " " + x.orgao + " " + x.unidade + " " + x.municipio).includes(p))) &&
    (vmin == null || (x.valor || 0) >= vmin) && (vmax == null || (x.valor != null && x.valor <= vmax)) &&
    (lim == null || (diasAte(x.encerramento) != null && diasAte(x.encerramento) <= lim)) &&
    (!DIA || (x.encerramento && (MODO_DIA === "no" ? x.encerramento.slice(0, 10) === DIA : x.encerramento.slice(0, 10) >= DIA)))
  ).sort((a, b) => {
    if (ORDEM === "recentes") return (b.publicacao || "").localeCompare(a.publicacao || "");
    if (ORDEM === "maior") return (b.valor || 0) - (a.valor || 0);
    if (ORDEM === "menor") return (a.valor ?? Infinity) - (b.valor ?? Infinity);
    return (a.encerramento || "9").localeCompare(b.encerramento || "9");
  });
}

function desenharPortais() {
  const c = DADOS ? contar(DADOS.itens, "portal") : {};
  const outros = Object.keys(c).filter((p) => !MEUS.includes(p)).sort((a, b) => c[b] - c[a]);
  const linha = (p, meu) => `<label class="opc${meu ? " meu" : ""}"><input type="checkbox" data-portal="${esc(p)}"${PREF.portais.includes(p) ? " checked" : ""}><span>${meu ? "★ " : ""}${esc(p)}</span><span class="n">${c[p] || 0}</span></label>`;
  $("#f-portal").innerHTML =
    `<div class="sub">Meus portais</div>` + MEUS.map((p) => linha(p, true)).join("") +
    `<div style="display:flex;gap:12px;margin:6px 0 2px"><button type="button" class="mini-bt" data-sel="meus">Só os meus</button><button type="button" class="mini-bt" data-sel="todos">Todos</button></div>` +
    (outros.length ? `<details><summary style="font-size:11px">Outros portais (${outros.length})</summary>${outros.map((p) => linha(p, false)).join("")}</details>` : "");
}
function desenharMods() {
  const c = DADOS ? contar(DADOS.itens, "modalidade") : {};
  $("#f-mod").innerHTML = MODS.map(([id, nome]) => `<label class="opc"><input type="checkbox" data-mod="${id}"${PREF.mods.includes(id) ? " checked" : ""}><span>${nome}</span><span class="n">${DADOS && PREF.mods.includes(id) ? (c[id] || 0) : ""}</span></label>`).join("");
}

function cartao(x) {
  const dias = diasAte(x.encerramento), meu = MEUS.includes(x.portal);
  const urg = dias != null && dias <= 2;
  return `<article class="card${meu ? " meu" : ""}">
    <div class="tags">
      ${x.numero ? `<span class="tag num">${esc(x.numero)}</span>` : ""}
      <span class="tag mod">${esc(x.modalidadeNome)}</span>
      ${x.srp ? `<span class="tag srp">Registro de preços</span>` : ""}
      ${dias != null ? `<span class="tag prazo${urg ? " urg" : ""}">${textoPrazo(dias)}</span>` : ""}
      <span class="tag">📍 ${esc(x.uf)}</span>
      ${x.uasg ? `<span class="tag">UASG ${esc(x.uasg)}</span>` : ""}
      ${seloPortal(x.portal)}
    </div>
    <div class="corpo">
      <h3><a href="/licitacao?id=${encodeURIComponent(x.id)}" target="_blank" rel="noopener">${esc(x.orgao)}</a></h3>
      <p class="obj"><a href="/licitacao?id=${encodeURIComponent(x.id)}" target="_blank" rel="noopener" title="Abrir a página da licitação">${esc(x.objeto)}</a></p>
      <div class="loc">${esc([x.municipio, x.uf].filter(Boolean).join(" · "))}${x.unidade ? " · " + esc(x.unidade) : ""}</div>
    </div>
    <div class="lado">
      <div><span>Valor estimado</span><b>${brl(x.valor)}</b></div>
      <div class="ate"><span>Propostas até</span><b class="${urg ? "" : "ok"}">${dataHora(x.encerramento)}</b></div>
    </div>
    <div class="acoes">
      ${x.linkPortal ? `<a class="pri" href="${esc(x.linkPortal)}" target="_blank" rel="noopener">Abrir no ${esc(x.portal.length > 22 ? "portal" : x.portal)}</a>` : ""}
      ${x.linkPncp ? `<a href="${esc(x.linkPncp)}" target="_blank" rel="noopener">Ver no PNCP</a>` : ""}
      ${x.cnpj ? htmlNota(x.cnpj) : ""}
    </div>
    ${ABERTOS[x.id] ? `<div class="det">${painelDet(DET[x.id], x)}</div>` : ""}
  </article>`;
}

const ABERTOS = {}, DET = {};
function seloPortal(p) {
  if (p === "Compras.gov.br") return `<span class="selo-cg" title="Licitação do Compras.gov.br"><i><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M3 4h2l2.5 11h11L21 7H6.5"/><circle cx="9" cy="20" r="1.4"/><circle cx="18" cy="20" r="1.4"/></svg></i><span>Compras<em>.gov.br</em></span></span>`;
  return `<span class="tag portal${MEUS.includes(p) ? " meu" : ""}">${esc(p)}</span>`;
}
// ===== botão Score: abre o Consulta Pagador com o CNPJ do órgão =====
function htmlNota(cnpj) {
  return `<a class="pag" href="${CONSULTA}${cnpj}" target="_blank" rel="noopener" title="Ver a nota do órgão no Consulta Pagador">📊 Score</a>`;
}
function pedirNotas() {}
const ICDOC = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 3H6v18h12V7l-4-4z"/><path d="M14 3v4h4M12 11v6M9 14l3 3 3-3"/></svg>';
function qtd(v) { return v == null ? "" : Number(v).toLocaleString("pt-BR", {maximumFractionDigits: 4}); }
function dataSo(s) { const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s || ""); return m ? `${m[3]}/${m[2]}/${m[1]}` : ""; }
function cnpjFmt(c) { return c && c.length === 14 ? c.replace(/^(\d{2})(\d{3})(\d{3})(\d{4})(\d{2})$/, "$1.$2.$3/$4-$5") : c || ""; }
function blocoDados(x) {
  const campos = [["Órgão", x.orgao], ["CNPJ", cnpjFmt(x.cnpj), 1], ["Unidade", x.unidade], ["Código da unidade (UASG)", x.uasg, 1],
    ["Local", [x.municipio, x.uf].filter(Boolean).join(" - ")], ["Esfera", x.esfera], ["Poder", x.poder], ["Modalidade", x.modalidadeNome],
    ["Modo de disputa", x.disputa], ["Amparo legal", x.amparo], ["Processo", x.processo, 1], ["Registro de preços", x.srp ? "Sim" : "Não"],
    ["Abertura das propostas", dataHora(x.abertura)], ["Propostas até", dataHora(x.encerramento)], ["Publicado no PNCP", dataSo(x.publicacao)],
    ["Situação", x.situacao], ["Portal", x.portal], ["Nº de controle PNCP", x.id, 1]];
  return `<div class="bts"><button type="button" data-cal="${esc(x.id)}">📅 Salvar no calendário</button><button type="button" data-copiar="${esc(x.linkPortal || x.linkPncp)}">🔗 Copiar link</button></div>
    <div><h4>Objeto completo</h4><p class="txt">${esc(x.objeto)}</p></div>
    ${x.complemento && x.complemento !== x.objeto ? `<div><h4>Informação complementar</h4><p class="txt">${esc(x.complemento)}</p></div>` : ""}
    <div><h4>Dados da contratação</h4><div class="dados">${campos.filter((c) => c[1]).map(([k, v, m]) => `<div><span>${k}</span><b${m ? ' class="m"' : ""}>${esc(v)}</b></div>`).join("")}</div></div>`;
}
function painelDet(d, x) {
  const dados = x ? blocoDados(x) : "";
  if (!d) return dados + `<div class="carregando" style="padding:12px"><div class="spin"></div>Buscando documentos e itens no PNCP…</div>`;
  if (d.erro) return dados + `<div class="aviso">${esc(d.erro)}</div>`;
  let h = dados; `<div class="carregando" style="padding:12px"><div class="spin"></div>Buscando documentos e itens no PNCP…</div>`;
  if (d.erro) return `<div class="aviso">${esc(d.erro)}</div>`;
  if (d.erros && d.erros.length) h += `<div class="aviso">O PNCP não respondeu para ${esc(d.erros.join("; "))}. Tente de novo em alguns minutos.</div>`;
  h += `<div><h4>Documentos (${d.arquivos.length})</h4>` + (d.arquivos.length
    ? `<div class="docs">${d.arquivos.map((a) => `<a class="doc" href="${esc(a.url)}" target="_blank" rel="noopener">${ICDOC}<span>${esc(a.titulo)}${a.tipo && a.tipo !== a.titulo ? " · " + esc(a.tipo) : ""}</span><small>${esc(a.data.split("-").reverse().join("/"))}</small></a>`).join("")}</div>`
    : `<p class="loc">Nenhum documento publicado no PNCP. Veja no portal da licitação.</p>`) + `</div>`;
  if (d.itens.length) {
    const tot = d.itens.reduce((s, i) => s + (i.vt || 0), 0);
    h += `<div><h4>Itens (${d.itens.length})</h4><div class="tw"><table><thead><tr><th>Item</th><th style="text-align:left">Descrição</th><th>Qtd</th><th>Un.</th><th>Valor unit.</th><th>Total</th></tr></thead><tbody>` +
      d.itens.map((i) => `<tr><td>${esc(i.n)}</td><td class="d">${esc(i.descricao)}${i.tipo ? `<span class="tipo">${esc(i.tipo)}</span>` : ""}${i.me && !/sem benef|nao se aplica|não se aplica/i.test(i.me) ? `<small>${esc(i.me)}</small>` : ""}</td><td>${qtd(i.qtd)}</td><td>${esc(i.un)}</td><td>${i.vu != null ? brl(i.vu) : "—"}</td><td>${i.vt != null ? brl(i.vt) : "—"}</td></tr>`).join("") +
      `</tbody>${tot ? `<tfoot><tr><td colspan="5">Total estimado</td><td>${brl(tot)}</td></tr></tfoot>` : ""}</table></div></div>`;
  }
  return h;
}
function salvarCalendario(x) {
  if (!x || !x.encerramento) return;
  const ics = (s) => s.replace(/[-:]/g, "").slice(0, 13) + "00";
  const fim = ics(x.encerramento);
  const d0 = new Date(new Date(x.encerramento + ":00").getTime() - 3600000), z = (n) => String(n).padStart(2, "0");
  const ini = `${d0.getFullYear()}${z(d0.getMonth() + 1)}${z(d0.getDate())}T${z(d0.getHours())}${z(d0.getMinutes())}00`;
  const txt = (s) => String(s || "").replace(/[\\;,]/g, (c) => "\\" + c).replace(/\n/g, " ");
  const corpo = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Busca Licitacoes//PT", "BEGIN:VEVENT",
    "UID:" + x.id.replace(/\W/g, "") + "@busca-licitacoes", "DTSTAMP:" + new Date().toISOString().replace(/[-:]/g, "").slice(0, 15) + "Z",
    "DTSTART:" + ini, "DTEND:" + fim,
    "SUMMARY:" + txt("Prazo de proposta: " + x.modalidadeNome + " " + x.numero + " - " + x.orgao),
    "DESCRIPTION:" + txt(x.objeto + " | " + (x.linkPortal || x.linkPncp)),
    "BEGIN:VALARM", "TRIGGER:-PT2H", "ACTION:DISPLAY", "DESCRIPTION:Prazo de proposta encerra em 2 horas", "END:VALARM",
    "END:VEVENT", "END:VCALENDAR"].join("\r\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([corpo], {type: "text/calendar"}));
  a.download = "prazo-" + (x.numero || "licitacao").replace(/\W/g, "-") + ".ics";
  document.body.appendChild(a); a.click(); a.remove();
}
async function abrirDet(id) {
  ABERTOS[id] = !ABERTOS[id];
  desenhar();
  if (!ABERTOS[id] || DET[id]) return;
  try {
    const r = await fetch(`/api/detalhes?id=${encodeURIComponent(id)}`);
    const d = await r.json();
    DET[id] = r.ok ? d : {erro: d.erro || "Falha ao buscar no PNCP."};
  } catch (e) { DET[id] = {erro: "Falha ao buscar no PNCP. Tente de novo."}; }
  if (DET[id].erro || (DET[id].erros && DET[id].erros.length)) { const x = DET[id]; delete DET[id]; DET[id] = x; setTimeout(() => { if (DET[id] === x) delete DET[id]; }, 1000); }
  desenhar();
}

function desenhar() {
  desenharPortais(); desenharMods();
  if (!DADOS) return;
  const lista = filtrar(), nome = (UFS.find((u) => u[0] === DADOS.uf) || ["", DADOS.uf])[1];
  let h = `<div class="cab"><span><b>${lista.length.toLocaleString("pt-BR")}</b> licitaç${lista.length === 1 ? "ão encontrada" : "ões encontradas"} em <b>${esc(nome)}</b> com proposta em aberto${DIA ? ` · disputa ${MODO_DIA === "no" ? "no dia" : "a partir de"} <b>${DIA.split("-").reverse().join("/")}</b>` : ""}</span><span class="at">atualizado ${esc(DADOS.atualizado)}</span></div>`;
  if (DADOS.erros.length) h += `<div class="aviso">O PNCP não respondeu para: ${esc(DADOS.erros.join("; "))}. Tente buscar de novo em alguns minutos.</div>`;
  if (DADOS.cortadas.length) h += `<div class="aviso">Muitas licitações em ${esc(DADOS.cortadas.join(", "))}: mostrando as 1.500 primeiras de cada uma. Use a palavra-chave para achar o que procura.</div>`;
  if (!lista.length) h += `<div class="vazio"><b>Nenhuma licitação com esses filtros</b>Tente marcar mais portais ou modalidades, ou mudar a palavra-chave.</div>`;
  h += lista.slice(0, MOSTRAR).map(cartao).join("");
  if (lista.length > MOSTRAR) h += `<button type="button" class="mais" id="mais">Mostrar mais (${(lista.length - MOSTRAR).toLocaleString("pt-BR")} restantes)</button>`;
  $("#res").innerHTML = h;
  pedirNotas();
}

let pedido = 0;
async function buscar() {
  const uf = $("#uf").value, meu = ++pedido;
  PREF.uf = uf; salvar(); MOSTRAR = 40;
  $("#bt").disabled = true;
  $("#res").innerHTML = `<div class="carregando"><div class="spin"></div>Buscando licitações abertas de ${esc(uf)} no PNCP. A primeira busca de cada estado leva de 20 a 40 segundos; depois fica guardada por 30 minutos.</div>`;
  try {
    const r = await fetch(`/api/busca?uf=${uf}&mods=${PREF.mods.join(",")}`);
    const d = await r.json();
    if (meu !== pedido) return;
    if (!r.ok) throw new Error(d.erro || "Falha na busca.");
    DADOS = d; desenhar();
  } catch (e) {
    if (meu === pedido) $("#res").innerHTML = `<div class="erro"><b>Não deu para buscar.</b> ${esc(e.message)}</div>`;
  } finally { if (meu === pedido) $("#bt").disabled = false; }
}

$("#f").addEventListener("submit", (e) => { e.preventDefault(); if (!DADOS || DADOS.uf !== $("#uf").value) buscar(); else { MOSTRAR = 40; desenhar(); } });
$("#uf").addEventListener("change", buscar);
$("#q").addEventListener("input", () => { MOSTRAR = 40; if (DADOS) desenhar(); });
["#vmin", "#vmax"].forEach((s) => $(s).addEventListener("input", () => { if (DADOS) desenhar(); }));
function hojeMais(n) { const d = new Date(); d.setDate(d.getDate() + n); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; }
$("#dia").min = hojeMais(0);
$("#dia").addEventListener("change", () => { DIA = $("#dia").value; MOSTRAR = 40; desenhar(); });
$("#modo-dia").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  MODO_DIA = b.dataset.m; document.querySelectorAll("#modo-dia button").forEach((x) => x.setAttribute("aria-pressed", x === b)); desenhar();
});
$("#atalhos-dia").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  DIA = b.dataset.d === "" ? "" : hojeMais(+b.dataset.d); $("#dia").value = DIA; MOSTRAR = 40; desenhar();
});
$("#f-prazo").addEventListener("change", (e) => { PRAZO = e.target.value; desenhar(); });
$("#f-ordem").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  ORDEM = b.dataset.o; document.querySelectorAll("#f-ordem button").forEach((x) => x.setAttribute("aria-pressed", x === b)); desenhar();
});
$("#f-portal").addEventListener("change", (e) => {
  const p = e.target.dataset.portal; if (p == null) return;
  PREF.portais = e.target.checked ? [...new Set(PREF.portais.concat(p))] : PREF.portais.filter((x) => x !== p);
  salvar(); MOSTRAR = 40; desenhar();
});
$("#f-portal").addEventListener("click", (e) => {
  const s = e.target.dataset.sel; if (!s) return;
  PREF.portais = s === "meus" ? MEUS.slice() : []; salvar(); desenhar();
});
$("#f-mod").addEventListener("change", (e) => {
  const m = +e.target.dataset.mod; if (!m) return;
  PREF.mods = e.target.checked ? [...new Set(PREF.mods.concat(m))] : PREF.mods.filter((x) => x !== m);
  if (!PREF.mods.length) { PREF.mods = [m]; e.target.checked = true; }
  salvar(); buscar();
});
$("#res").addEventListener("click", (e) => {
  if (e.target.id === "mais") { MOSTRAR += 40; desenhar(); return; }
  const bc = e.target.closest("[data-copiar]");
  if (bc) {
    const t = bc.dataset.copiar, ok = () => { bc.textContent = "✔ Link copiado"; setTimeout(() => { bc.textContent = "🔗 Copiar link"; }, 2000); };
    if (navigator.clipboard) navigator.clipboard.writeText(t).then(ok, () => prompt("Copie o link:", t)); else prompt("Copie o link:", t);
    return;
  }
  const bk = e.target.closest("[data-cal]");
  if (bk) { salvarCalendario(DADOS.itens.find((i) => i.id === bk.dataset.cal)); return; }
  const bd = e.target.closest("[data-det]");
  if (bd) { abrirDet(bd.dataset.det); return; }
  if (e.target.classList.contains("obj")) e.target.classList.toggle("aberto");
});

// ===== página de uma licitação: /licitacao?id=... =====
function textoEncerra(dias) {
  if (dias == null) return "";
  if (dias < 0) return "Prazo encerrado";
  if (dias < 1 / 24) return "Encerra em minutos";
  if (dias < 1) return `Encerra em ${Math.ceil(dias * 24)} horas`;
  return textoPrazo(dias);
}
async function paginaLicitacao(id) {
  document.querySelector("#f").hidden = true;
  document.querySelector(".grade").hidden = true;
  const alvo = document.createElement("main");
  alvo.className = "pg"; alvo.id = "pg";
  document.querySelector(".grade").after(alvo);
  alvo.innerHTML = `<a class="voltar" href="/">← Nova busca</a><div class="carregando"><div class="spin"></div>Carregando a licitação no PNCP…</div>`;
  let x;
  try {
    const r = await fetch(`/api/licitacao?id=${encodeURIComponent(id)}`);
    x = await r.json();
    if (!r.ok) throw new Error(x.erro || "Falha ao buscar no PNCP.");
  } catch (e) {
    alvo.innerHTML = `<a class="voltar" href="/">← Nova busca</a><div class="erro"><b>Não deu para abrir a licitação.</b> ${esc(e.message)}</div>`;
    return;
  }
  DADOS = {itens: [x]};
  document.title = `${x.modalidadeNome} ${x.numero} · ${x.orgao}`;
  const dias = diasAte(x.encerramento), meu = MEUS.includes(x.portal);
  alvo.innerHTML = `<a class="voltar" href="/">← Nova busca</a>
    <div>
      <div class="tags">
        ${x.numero ? `<span class="tag num">${esc(x.numero)}</span>` : ""}
        <span class="tag mod">${esc(x.modalidadeNome)}</span>
        ${x.srp ? `<span class="tag srp">Registro de preços</span>` : ""}
        ${dias != null && dias >= 0 ? `<span class="tag prazo${dias <= 2 ? " urg" : ""}">${textoPrazo(dias)}</span>` : ""}
        <span class="tag">📍 ${esc(x.uf)}</span>
        ${x.uasg ? `<span class="tag">UASG ${esc(x.uasg)}</span>` : ""}
        ${seloPortal(x.portal)}
      </div>
      <h2>${esc(x.objeto)}</h2>
      <div class="org">${esc(x.orgao)} <small>· ${esc(x.unidade)}${x.municipio ? " · " + esc(x.municipio) + " - " + esc(x.uf) : ""}</small></div>
    </div>
    <div class="quad">
      <div><span>Valor estimado</span><b>${brl(x.valor)}</b></div>
      <div><span>Propostas até</span><b class="${dias != null && dias <= 2 ? "urg" : ""}">${dataHora(x.encerramento)}</b><small>${esc(textoEncerra(dias))}</small></div>
      <div><span>Abertura das propostas</span><b>${dataHora(x.abertura)}</b></div>
      <div><span>Publicado no PNCP</span><b>${dataSo(x.publicacao) || "—"}</b></div>
    </div>
    <div class="acoes">
      ${x.linkPortal ? `<a class="pri" href="${esc(x.linkPortal)}" target="_blank" rel="noopener">Abrir no ${esc(x.portal.length > 22 ? "portal" : x.portal)}</a>` : ""}
      ${x.linkPncp ? `<a href="${esc(x.linkPncp)}" target="_blank" rel="noopener">Ver no PNCP (oficial)</a>` : ""}
      ${x.cnpj ? htmlNota(x.cnpj) : ""}
      <button type="button" data-cal="${esc(x.id)}">📅 Salvar no calendário</button>
      <button type="button" data-copiar="${esc(location.href)}">🔗 Copiar link</button>
    </div>
    ${""}<section class="secao" id="sec-docs"><h3>Documentos do edital</h3><div class="carregando" style="padding:12px"><div class="spin"></div>Buscando documentos e itens no PNCP…</div></section>
    <section class="secao" id="sec-itens" hidden></section>
    <section class="secao"><h3>Objeto completo</h3><p class="txt">${esc(x.objeto)}</p>
      ${x.complemento && x.complemento !== x.objeto ? `<h3>Informação complementar</h3><p class="txt">${esc(x.complemento)}</p>` : ""}</section>
    <section class="secao"><h3>Dados da contratação</h3>${blocoDados(x).replace(/^[\s\S]*?<div><h4>Dados da contratação<\/h4>/, "<div>")}</section>`;
  pedirNotas();
  alvo.addEventListener("click", (e) => {
    const bc = e.target.closest("[data-copiar]"), bk = e.target.closest("[data-cal]");
    if (bk) salvarCalendario(x);
    if (bc) {
      const t = bc.dataset.copiar, ok = () => { bc.textContent = "✔ Link copiado"; setTimeout(() => { bc.textContent = "🔗 Copiar link"; }, 2000); };
      if (navigator.clipboard) navigator.clipboard.writeText(t).then(ok, () => prompt("Copie o link:", t)); else prompt("Copie o link:", t);
    }
  });
  let d;
  try {
    const r = await fetch(`/api/detalhes?id=${encodeURIComponent(id)}`);
    d = await r.json();
    if (!r.ok) throw new Error(d.erro || "Falha ao buscar no PNCP.");
  } catch (e) {
    alvo.querySelector("#sec-docs").innerHTML = `<h3>Documentos do edital</h3><div class="aviso">${esc(e.message)} Use o botão "Ver no PNCP (oficial)".</div>`;
    return;
  }
  const docs = d.arquivos.length
    ? `<div class="docs">${d.arquivos.map((a) => `<a class="doc" href="${esc(a.url)}" target="_blank" rel="noopener">${ICDOC}<span>${esc(a.titulo)}${a.tipo && a.tipo !== a.titulo ? " · " + esc(a.tipo) : ""}</span><small>${esc(dataSo(a.data))}</small></a>`).join("")}</div>`
    : `<p class="loc">Nenhum documento publicado no PNCP. Veja no portal da licitação.</p>`;
  alvo.querySelector("#sec-docs").innerHTML = `<h3>Documentos do edital (${d.arquivos.length})</h3>` +
    (d.erros && d.erros.length ? `<div class="aviso">O PNCP não respondeu para ${esc(d.erros.join("; "))}.</div>` : "") + docs;
  if (d.itens.length) {
    const sec = alvo.querySelector("#sec-itens"), tot = d.itens.reduce((s, i) => s + (i.vt || 0), 0);
    sec.hidden = false;
    sec.innerHTML = `<h3>Itens da contratação (${d.itens.length})</h3><div class="tw"><table><thead><tr><th>Item</th><th style="text-align:left">Descrição</th><th>Un.</th><th>Qtd</th><th>Valor unit.</th><th>Valor total</th></tr></thead><tbody>` +
      d.itens.map((i) => `<tr><td>${esc(i.n)}</td><td class="d">${esc(i.descricao)}${i.tipo ? `<span class="tipo">${esc(i.tipo)}</span>` : ""}${i.me && !/sem benef|nao se aplica|não se aplica/i.test(i.me) ? `<small>${esc(i.me)}</small>` : ""}</td><td>${esc(i.un)}</td><td>${qtd(i.qtd)}</td><td>${i.vu != null ? brl(i.vu) : "—"}</td><td>${i.vt != null ? brl(i.vt) : "—"}</td></tr>`).join("") +
      `</tbody>${tot ? `<tfoot><tr><td colspan="5">Total estimado dos itens</td><td>${brl(tot)}</td></tr></tfoot>` : ""}</table></div>`;
  }
}

const qs = new URLSearchParams(location.search);
if (location.pathname === "/licitacao" && qs.get("id")) {
  paginaLicitacao(qs.get("id"));
} else {
desenhar();
if (qs.get("uf")) $("#uf").value = qs.get("uf").toUpperCase();
if (qs.get("q")) $("#q").value = qs.get("q");
buscar();
}
</script>
</body>
</html>
"""


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
        if u.path == "/api/nota":
            q = urllib.parse.parse_qs(u.query)
            try:
                res = nota_orgao(q.get("cnpj", [""])[0])
                return self._send(200, json.dumps(res, ensure_ascii=False), "application/json; charset=utf-8")
            except Exception as e:
                return self._send(502, json.dumps({"erro": erro_txt(e)}, ensure_ascii=False), "application/json; charset=utf-8")
        if u.path == "/api/licitacao":
            q = urllib.parse.parse_qs(u.query)
            try:
                res = licitacao(q.get("id", [""])[0])
                return self._send(200, json.dumps(res, ensure_ascii=False), "application/json; charset=utf-8")
            except ValueError as e:
                return self._send(404, json.dumps({"erro": str(e)}, ensure_ascii=False), "application/json; charset=utf-8")
            except Exception as e:
                return self._send(502, json.dumps({"erro": erro_txt(e)}, ensure_ascii=False), "application/json; charset=utf-8")
        if u.path == "/api/detalhes":
            q = urllib.parse.parse_qs(u.query)
            try:
                res = detalhes(q.get("id", [""])[0])
                return self._send(200, json.dumps(res, ensure_ascii=False), "application/json; charset=utf-8")
            except ValueError as e:
                return self._send(400, json.dumps({"erro": str(e)}, ensure_ascii=False), "application/json; charset=utf-8")
            except Exception as e:
                return self._send(502, json.dumps({"erro": erro_txt(e)}, ensure_ascii=False), "application/json; charset=utf-8")
        if u.path == "/api/busca":
            q = urllib.parse.parse_qs(u.query)
            try:
                mods = [int(x) for x in re.findall(r"\d+", q.get("mods", [""])[0])]
                res = buscar(q.get("uf", [""])[0], mods)
                return self._send(200, json.dumps(res, ensure_ascii=False), "application/json; charset=utf-8")
            except ValueError as e:
                return self._send(400, json.dumps({"erro": str(e)}, ensure_ascii=False), "application/json; charset=utf-8")
            except Exception as e:
                return self._send(502, json.dumps({"erro": erro_txt(e)}, ensure_ascii=False), "application/json; charset=utf-8")
        if u.path == "/diagnostico2":
            try:
                txt = diagnostico_busca()
            except Exception as e:
                txt = "ERRO: " + erro_txt(e)
            return self._send(200, txt, "text/plain; charset=utf-8")
        if u.path in ("/", "/index.html", "/licitacao"):
            return self._send(200, PAGINA, "text/html; charset=utf-8")
        self._send(404, "não encontrado", "text/plain; charset=utf-8")


def main():
    na_nuvem = "PORT" in os.environ
    srv = ThreadingHTTPServer(("0.0.0.0" if na_nuvem else "127.0.0.1", PORT), Handler)
    print(f"Busca Licitações rodando na porta {PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
