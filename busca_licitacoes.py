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

# O servidor do Render usa o horário de Londres; as datas do PNCP são de Brasília.
os.environ["TZ"] = "America/Sao_Paulo"
try:
    time.tzset()
except AttributeError:  # Windows não tem tzset
    pass

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


# Conexões reaproveitadas com o PNCP: abrir conexão segura (https) a cada página gasta muito
# processamento, e o servidor gratuito do Render é bem fraco. Assim abre uma vez e reaproveita.
import http.client
import ssl
import threading
_conexoes, _conexoes_lock = {}, threading.Lock()
_SSL = ssl.create_default_context()


_estat = {"inicio": time.strftime("%d/%m %H:%M"), "pedidos": 0, "ok": 0, "segundos_ok": 0.0, "codigos": Counter(),
          "falhas": Counter(), "ultimos_erros": []}


def _anota(t0, codigo=None, erro=None):
    with _conexoes_lock:
        _estat["pedidos"] += 1
        if codigo == 200:
            _estat["ok"] += 1
            _estat["segundos_ok"] += time.time() - t0
        elif codigo is not None:
            _estat["codigos"][codigo] += 1
        if erro:
            _estat["falhas"][erro[:60]] += 1
            _estat["ultimos_erros"] = (_estat["ultimos_erros"] + [time.strftime("%H:%M:%S ") + erro[:120]])[-8:]


def diagnostico_velocidade():
    e = _estat
    media = e["segundos_ok"] / e["ok"] if e["ok"] else 0
    linhas = [f"DIAGNOSTICO VELOCIDADE (desde {e['inicio']})",
              f"pedidos ao PNCP: {e['pedidos']} | deram certo: {e['ok']} | tempo medio: {media:.1f}s",
              f"recusas do PNCP (codigo: vezes): {dict(e['codigos']) or 'nenhuma'}",
              f"falhas de conexao: {dict(e['falhas']) or 'nenhuma'}",
              f"estados guardados: {sorted({k.split('|')[0] for k in _cache if '|' in k and not k.startswith('lic')})}",
              "ultimos erros:"] + (e["ultimos_erros"] or ["nenhum"])
    return "\n".join(linhas)


def http_get_rapido(url, timeout=45):
    """Igual ao http_get, mas reaproveita a conexão. Erros viram urllib.error.HTTPError como antes."""
    u = urllib.parse.urlparse(url)
    host, caminho = u.netloc, u.path + ("?" + u.query if u.query else "")
    for tentativa in (1, 2):
        with _conexoes_lock:
            livres = _conexoes.setdefault(host, [])
            c = livres.pop() if livres else None
        nova = c is None
        if nova:
            proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
            if proxy:  # (só no ambiente de teste)
                pr = urllib.parse.urlparse(proxy)
                c = http.client.HTTPSConnection(pr.hostname, pr.port, timeout=timeout, context=_SSL)
                c.set_tunnel(host, 443)
            else:
                c = http.client.HTTPSConnection(host, timeout=timeout, context=_SSL)
        t0 = time.time()
        try:
            c.timeout = timeout
            if c.sock is not None:
                c.sock.settimeout(timeout)
            c.request("GET", caminho, headers={"User-Agent": UA, "Accept": "application/json", "Connection": "keep-alive"})
            r = c.getresponse()
            corpo = r.read()
        except Exception as e:
            c.close()
            if nova or tentativa == 2:
                _anota(t0, erro=type(e).__name__ + ": " + str(e))
                raise
            continue  # conexão velha que o PNCP fechou: tenta com uma nova
        _anota(t0, codigo=r.status)
        if r.getheader("Connection", "").lower() == "close":
            c.close()
        else:
            with _conexoes_lock:
                if len(_conexoes[host]) < 8:
                    _conexoes[host].append(c)
                else:
                    c.close()
        if r.status != 200:
            import io
            raise urllib.error.HTTPError(url, r.status, r.reason, r.headers, io.BytesIO(corpo))
        if not corpo.strip():
            return None
        return json.loads(corpo.decode("utf-8"))


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
    ("Licitações-e", ("licitacoes-e", "licitacoes e", "banco do brasil"), ("licitacoes-e", "bb.com.br")),
    ("BLL Compras", ("bll compras",), ("bllcompras.com",)),
    ("Licitanet", ("licitanet",), ("licitanet.com.br",)),
    ("Portal de Compras Públicas", ("ecustomize", "portal de compras publicas"), ("portaldecompraspublicas.com.br",)),
    ("BNC Compras", ("bolsa nacional de compras",), ("bnccompras.com",)),
    ("Compras RJ", ("compras publicas do estado do rio de janeiro",), ("compras.rj.gov.br",)),
    ("Banrisul (RS)", ("procergs",), ("pregaobanrisul.com.br", "compras.rs.gov.br")),
]


def sem_acento(s):
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()


# Categoria -> palavras (sem acento) que aparecem no objeto da licitação
CATEGORIAS = [
    ("Eletrodomésticos", ["eletrodomestic", "eletroportat", "liquidificador", "geladeira", "refrigerador", "freezer", "frigobar",
                          "micro-ondas", "microondas", "micro ondas", "fogao", "forno eletrico", "ventilador", "climatizador",
                          "ar condicionado", "ar-condicionado", "condicionador de ar", "bebedouro", "purificador de agua",
                          "cafeteira", "sanduicheira", "fritadeira", "air fryer", "airfryer", "batedeira", "espremedor",
                          "chaleira eletrica", "ferro de passar", "aspirador", "lavadora", "maquina de lavar", "secadora",
                          "televisor", "televisao", "smart tv", "umidificador", "exaustor", "coifa", "cooktop", "processador de alimentos"]),
    ("Material de escritório", ["material de escritorio", "material de expediente", "papel a4", "papel sulfite", "papel oficio",
                                "caneta", "lapis", "grampeador", "grampo", "clips", "envelope", "marca texto", "marca-texto",
                                "papelaria", "arquivo morto", "corretivo", "perfurador", "post-it", "bloco de notas", "pasta suspensa"]),
    ("Informática", ["informatica", "computador", "notebook", "desktop", "monitor", "impressora", "multifuncional", "toner",
                     "cartucho", "teclado", "mouse", "nobreak", "no-break", "servidor de rede", "switch", "roteador",
                     "tablet", "ssd", "pen drive", "pendrive", "projetor", "webcam", "licenca de software", "tecnologia da informacao"]),
    ("Limpeza e higiene", ["limpeza", "higiene", "detergente", "desinfetante", "sabao", "agua sanitaria", "papel higienico",
                           "papel toalha", "saco de lixo", "sacos de lixo", "vassoura", "rodo", "alcool", "sabonete", "limpador",
                           "conservacao e limpeza", "fralda", "absorvente", "lenco umedecido", "lencos umedecidos",
                           "creme dental", "escova dental", "escovas dentais", "shampoo", "xampu", "desodorante"]),
    ("Alimentos", ["genero alimenticio", "generos alimenticios", "alimento", "merenda", "alimentacao", "cafe ", "acucar",
                   "leite", "carne", "hortifruti", "frutas", "verduras", "cesta basica", "refeic", "lanche", "agua mineral", "pao "]),
    ("Copa e cozinha", ["copo descartavel", "copos descartaveis", "copa e cozinha", "utensilio", "panela", "talher", "prato",
                        "garrafa termica", "guardanapo", "filtro de papel", "pratos descartave", "prato descartave",
                        "talher descartave", "talheres descartave", "garfo descartave", "garfos descartave", "faca descartave",
                        "facas descartave", "colher descartave", "colheres descartave", "marmitex", "embalagem para alimento",
                        "embalagens para alimento", "material de copa"]),
    ("Móveis", ["mobiliario", "moveis", "cadeira", "mesa ", "mesas", "armario", "estante", "arquivo de aco", "sofa", "poltrona",
                "balcao", "gaveteiro", "longarina"]),
    ("Material de construção", ["material de construcao", "materiais de construcao", "material eletrico", "materiais eletricos",
                               "material hidraulico", "materiais hidraulicos", "tinta", "cimento", "ferramenta", "lampada",
                               "fio eletrico", "cabo eletrico", "tubo pvc", "tubos", "conexoes", "torneira", "fechadura",
                               "parafuso", "areia", "brita", "tijolo", "madeira", "telha"]),
    ("Veículos, peças e combustível", ["veiculo", "pneu", "pecas automotiv", "peca automotiv", "combustivel", "gasolina", "diesel",
                                      "oleo lubrificante", "motocicleta", "moto", "onibus", "ambulancia", "caminhao", "automotiv", "frota"]),
    ("Saúde e medicamentos", ["medicamento", "hospitalar", "odontolog", "laboratori", "luva", "mascara", "seringa", "curativo",
                             "equipamento medico", "material medico", "saude", "farmac", "insumos medic"]),
    ("Uniformes, calçados e EPI", ["uniforme", "fardamento", "camiseta", "camisa", "calcado", "bota", "epi ", "equipamento de protecao",
                                  "colete", "jaleco", "bone"]),
    ("Eventos, gráfica e brindes", ["grafica", "impressao de", "banner", "brinde", "evento", "buffet", "coffee break", "decoracao"]),
]
ASSISTENCIA = ["assistencia tecnica", "garantia on site", "garantia on-site", "garantia onsite", "rede autorizada", "instalacao"]


def _padrao(palavra):
    """A palavra tem que começar no início de uma palavra do texto (a checagem fica em _CAT_RE). Palavras curtas
    também têm que terminar ali (aceitando plural), para "rodo" não pegar "rodovia"."""
    p = re.escape(palavra.strip())
    if len(palavra.strip()) <= 6:
        return p + r"(?:s|es)?(?![a-z])"
    return p


_CAT_RE = [(nome, re.compile(r"(?<![a-z])(?:" + "|".join(_padrao(x) for x in palavras) + ")")) for nome, palavras in CATEGORIAS]


SERVICO = ["prestacao de servico", "prestacao de servicos", "servicos de", "servico de", "servico para", "servicos para", "contratacao de servico", "contratacao de servicos", "contratacao de empresa", "contratacao de empresas", "servicos continuos", "mao de obra",
           "obra", "obras", "reforma", "pavimentacao", "pavimento", "recapeamento", "engenharia", "execucao de", "terceirizacao",
           "terceirizado", "vigilancia", "consultoria", "assessoria", "locacao", "manutencao preventiva", "manutencao corretiva",
           "manutencao predial", "limpeza e conservacao", "transporte de", "custodia", "auditoria", "treinamento", "capacitacao",
           "credenciamento de", "selecao de pessoas", "agenciamento", "seguro", "publicidade", "buffet", "coffee break"]
PRODUTO = ["aquisicao", "fornecimento de material", "fornecimento de materiais", "fornecimento de produto", "compra de",
           "material", "materiais", "equipamento", "produto", "genero", "insumo", "medicamento"]
_SERV_RE = re.compile(r"(?<![a-z])(?:" + "|".join(re.escape(x) for x in SERVICO) + r")(?![a-z])")
_PROD_RE = re.compile(r"(?<![a-z])(?:" + "|".join(re.escape(x) for x in PRODUTO) + ")")


def tipo_de(texto):
    """'Produto' ou 'Serviço', pelas palavras do objeto. Na dúvida, fica como produto (para não esconder oportunidade)."""
    t = sem_acento(texto)
    if _SERV_RE.search(t):
        # "aquisição de peças... incluindo instalação" continua produto; "prestação de serviços... e fornecimento de peças" é serviço
        if _PROD_RE.search(t) and not re.search(r"(?<![a-z])(prestacao de servic|contratacao de (?:servic|empresa)|servicos? (?:de|para)|mao de obra|obras?|reforma|pavimenta|engenharia|locacao|terceiriza|vigilancia|consultoria|assessoria)", t):
            return "Produto"
        return "Serviço"
    return "Produto"


def categorias_de(texto):
    t = sem_acento(texto).replace("mao de obra", "mao-de-servico")
    cats = [nome for nome, rx in _CAT_RE if rx.search(t)]
    return cats or ["Outros"]


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
        "categorias": categorias_de(str(c.get("objetoCompra") or "")),
        "tipo": tipo_de(str(c.get("objetoCompra") or "")),
        "assistencia": ("Eletrodomésticos" in categorias_de(str(c.get("objetoCompra") or "")))
                       or any(p in sem_acento(str(c.get("objetoCompra") or "") + " " + str(c.get("informacaoComplementar") or "")) for p in ASSISTENCIA),
        "complemento": re.sub(r"\s+", " ", str(c.get("informacaoComplementar") or "")).strip()[:1500],
        "processo": str(c.get("processo") or ""),
        "amparo": (c.get("amparoLegal") or {}).get("nome") or "",
        "esfera": {"F": "Federal", "E": "Estadual", "M": "Municipal", "D": "Distrital"}.get(org.get("esferaId"), ""),
        "poder": {"E": "Executivo", "L": "Legislativo", "J": "Judiciário", "N": "Não se aplica"}.get(org.get("poderId"), ""),
        "situacao": c.get("situacaoCompraNome") or "",
        "linkPncp": pncp,
        "linkPortal": origem or (link_comprasgov(c) if portal == "Compras.gov.br" else ""),
    }


_PNCP_VAGAS = threading.BoundedSemaphore(4)  # no máximo 4 pedidos ao PNCP ao mesmo tempo (ele bloqueia quem pede demais)


def _pagina_proposta(uf, modalidade, pagina, tentativas=4):
    fim = (date.today() + timedelta(days=120)).strftime("%Y%m%d")
    url = (f"{PNCP}/contratacoes/proposta?dataFinal={fim}&codigoModalidadeContratacao={modalidade}"
           f"&uf={uf}&pagina={pagina}&tamanhoPagina=50")
    for t in range(1, tentativas + 1):
        try:
            with _PNCP_VAGAS:
                return http_get_rapido(url, timeout=45) or {}
        except urllib.error.HTTPError as e:
            if e.code == 204:
                return {}
            if (e.code < 500 and e.code != 429) or t == tentativas:
                raise  # antes, "muitos pedidos" (429) virava lista vazia sem aviso
            time.sleep((5 if e.code == 429 else 2) * t)
            continue
        except Exception:
            if t == tentativas:
                raise
        time.sleep(2 * t)
    return {}


MAX_PAGINAS = 100  # até 5.000 licitações por modalidade em cada estado
_cache, _lock = {}, threading.Lock()


def buscar_modalidade(uf, modalidade):
    chave = f"{uf}|{modalidade}"
    with _lock:
        if chave in _cache and time.time() - _cache[chave][0] < 7200:  # guarda 2 horas
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
        r.pop("complemento", None)  # a página da licitação busca o texto completo; a lista fica mais leve
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
    # documentos e itens hoje respondem em /api/pncp/v1 (em /api/consulta/v1 dão 403): tenta esse primeiro
    for base in (PNCP_API, PNCP):
        try:
            r = _get_tentando(base + caminho)
            if r is not None:
                return r
        except urllib.error.HTTPError as e:
            ultimo = e
            if e.code not in (301, 302, 307, 308, 400, 403, 404, 405):
                raise
    if ultimo is not None and ultimo.code not in (301, 302, 307, 308, 403, 404):
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
        if chave in _cache and time.time() - _cache[chave][0] < 7200:  # guarda 2 horas
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


# ================================================================ e-mail diário
# Configuração (no Render, em Environment):
#   RESEND_API_KEY  chave do serviço de e-mail Resend (grátis)
#   ALERTA_EMAIL    e-mail que recebe o resumo (o mesmo da conta do Resend)
#   ALERTA_CHAVE    uma senha qualquer, que o despertador (cron-job.org) usa para chamar o envio
#   ALERTA_UF       estado(s), separados por vírgula. Padrão: DF
#   ALERTA_PORTAIS  portal(is), separados por vírgula. Padrão: Compras.gov.br
#   ALERTA_PALAVRAS palavras-chave separadas por vírgula (vazio = todos os assuntos)
SITE = os.environ.get("SITE_URL", "https://busca-licitacoes-j9vl.onrender.com")
_envio = {"rodando": False, "ultimo": "", "dia": ""}  # "dia": data do último envio que deu certo


def _lista_env(nome, padrao=""):
    return [x.strip() for x in os.environ.get(nome, padrao).split(",") if x.strip()]


def _html_email(novas, urgentes, ufs, portais, palavras):
    from html import escape as e

    def linha(x):
        if x.get("valor") is None:
            valor = "valor não informado"
        elif not x["valor"]:
            valor = "valor sigiloso"
        else:
            valor = "R$ " + f"{x['valor']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        prazo = x.get("encerramento") or ""
        prazo = f"{prazo[8:10]}/{prazo[5:7]}/{prazo[0:4]} às {prazo[11:16]}" if len(prazo) >= 16 else "não informado"
        link = SITE + "/licitacao?id=" + urllib.parse.quote(x["id"], safe="")
        return (f'<tr><td style="padding:12px 14px;border-bottom:1px solid #e3e8f0">'
                f'<div style="font-size:12px;color:#5a6b80">{e(x["modalidadeNome"])} {e(x["numero"])} · UASG {e(x["uasg"])} · {e(x["portal"])} · {e(", ".join(x.get("categorias", [])))}'
                + '</div>'
                f'<div style="font-weight:700;color:#0c326f;margin:2px 0">{e(x["orgao"])}</div>'
                f'<div style="color:#14202e">{e(x["objeto"][:280])}</div>'
                f'<div style="font-size:13px;margin-top:4px"><b>{valor}</b> · propostas até <b style="color:#b3261e">{prazo}</b> · '
                f'<a href="{link}" style="color:#1351b4">Abrir</a></div></td></tr>')

    def bloco(titulo, lista, vazio):
        corpo = "".join(linha(x) for x in lista[:60]) or f'<tr><td style="padding:12px 14px;color:#5a6b80">{vazio}</td></tr>'
        extra = f'<tr><td style="padding:10px 14px;color:#5a6b80">… e mais {len(lista) - 60}. Veja todas no site.</td></tr>' if len(lista) > 60 else ""
        return (f'<h2 style="font-size:16px;margin:22px 0 8px;color:#0c326f">{titulo} ({len(lista)})</h2>'
                f'<table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #e3e8f0;border-radius:8px">{corpo}{extra}</table>')

    filtro = f'{", ".join(portais) or "todos os portais"} · {", ".join(ufs)}' + (f' · palavras: {", ".join(palavras)}' if palavras else "")
    return (f'<div style="font-family:Arial,sans-serif;background:#f3f5f9;padding:20px;color:#14202e">'
            f'<div style="max-width:720px;margin:auto">'
            f'<img src="{SITE}/logo-email.png?v=2" alt="SigaLicita" width="193" height="60" style="display:block;border:0;height:60px;width:auto;margin:0 0 6px -6px;font:700 22px Arial,sans-serif;color:#3b7bea">'
            f'<div style="font-size:17px;font-weight:700;color:#0c326f;margin:0 0 2px">Resumo do dia</div>'
            f'<div style="color:#5a6b80;font-size:13px">{e(filtro)}</div>'
            + bloco("🆕 Novas nas últimas 24 horas", novas, "Nenhuma licitação nova hoje.")
            + bloco("⚠ Encerram nos próximos 2 dias", urgentes, "Nenhuma encerrando nos próximos 2 dias.")
            + f'<p style="font-size:12px;color:#5a6b80;margin-top:18px">Abra o site para ver todas: <a href="{SITE}" style="color:#1351b4">{SITE}</a>. '
              f'Dados do PNCP. Confira sempre o edital no portal antes de participar.</p></div></div>')


def montar_resumo():
    ufs = _lista_env("ALERTA_UF", "DF")
    if "BR" in [u.upper() for u in ufs]:
        ufs = ["AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS", "MG", "PA", "PB", "PR",
               "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC", "SP", "SE", "TO"]
    portais = _lista_env("ALERTA_PORTAIS", "Compras.gov.br")
    palavras = [sem_acento(p) for p in _lista_env("ALERTA_PALAVRAS")]
    itens = []
    vistos = set()
    for uf in ufs:
        for x in buscar(uf, list(MODALIDADES_PADRAO))["itens"]:
            if x["id"] not in vistos:
                vistos.add(x["id"])
                itens.append(x)
    agora = time.time()
    ontem = time.strftime("%Y-%m-%dT%H:%M", time.localtime(agora - 86400))
    daqui2 = time.strftime("%Y-%m-%dT%H:%M", time.localtime(agora + 2 * 86400))
    agora_s = time.strftime("%Y-%m-%dT%H:%M", time.localtime(agora))
    hoje_br = time.strftime("%Y-%m-%d", time.gmtime(agora - 3 * 3600))  # data de hoje em Brasília

    cats = _lista_env("ALERTA_CATEGORIAS")
    so_produto = os.environ.get("ALERTA_TIPO", "produto").lower().startswith("prod")

    def passa(x):
        if (x.get("encerramento") or "9")[:10] <= hoje_br:
            return False  # encerra hoje: não dá mais tempo de participar
        if so_produto and x.get("tipo") == "Serviço":
            return False
        if portais and x["portal"] not in portais:
            return False
        if cats and not any(c in cats for c in x.get("categorias", [])):
            return False
        texto = sem_acento(x["objeto"] + " " + x["orgao"] + " " + x["unidade"])
        return not palavras or any(p in texto for p in palavras)

    sel = [x for x in itens if passa(x)]
    novas = sorted([x for x in sel if (x.get("publicacao") or "") >= ontem], key=lambda x: x.get("encerramento") or "9")
    urgentes = sorted([x for x in sel if agora_s <= (x.get("encerramento") or "") <= daqui2], key=lambda x: x.get("encerramento") or "9")
    return novas, urgentes, ufs, portais, _lista_env("ALERTA_PALAVRAS")


def enviar_email(assunto, html):
    chave = os.environ.get("RESEND_API_KEY", "")
    para = os.environ.get("ALERTA_EMAIL", "")
    if not chave or not para:
        raise RuntimeError("Faltam RESEND_API_KEY ou ALERTA_EMAIL nas configurações do Render.")
    corpo = json.dumps({"from": "SigaLicita <onboarding@resend.dev>", "to": [para],
                        "subject": assunto, "html": html}).encode("utf-8")
    req = urllib.request.Request("https://api.resend.com/emails", data=corpo, method="POST",
                                 headers={"Authorization": "Bearer " + chave, "Content-Type": "application/json",
                                          "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


def tarefa_email():
    _envio["rodando"] = True
    try:
        novas, urgentes, ufs, portais, palavras = montar_resumo()
        assunto = f"SigaLicita: {len(novas)} nova{'s' if len(novas) != 1 else ''} · {len(urgentes)} encerrando em 2 dias ({', '.join(ufs)})"
        enviar_email(assunto, _html_email(novas, urgentes, ufs, portais, palavras))
        _envio["ultimo"] = time.strftime("%d/%m/%Y %H:%M") + f" · enviado ({len(novas)} novas, {len(urgentes)} urgentes)"
        _envio["dia"] = time.strftime("%Y-%m-%d")
    except Exception as e:
        _envio["ultimo"] = time.strftime("%d/%m/%Y %H:%M") + " · ERRO: " + erro_txt(e)
    finally:
        _envio["rodando"] = False


PAGINA = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<script>try{if(localStorage.getItem("siga_tema")==="claro")document.documentElement.setAttribute("data-theme","light")}catch(e){}</script>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Busca Licitações</title>
<link rel="icon" type="image/png" sizes="64x64" href="data:image/png;base64,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">
<link rel="apple-touch-icon" href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAYAAAA9zQYyAABQJElEQVR42u2dd5hdVbnGf2vtffr0mslk0khCQgKhSpEiRRFBQEREUVQQriAdlWJDRfSqICIiINd6saDYr6IUkSItoYWShASSTMokk+kzp+691v1j7X3mTM3MnDMNsp5nUmbmnLPLu7/1fu/XBOO8tNYCEEIIlfO9IDAHmA0sBPb0vhqAYqAIKAcku9dUXApoA7qBLqARWA2s9b4agY1CiPQQmAgDi4BDgQ8ARwIBQIMWaG3+Kaze1yS2oZ0OZNECELYLWMB64B1CiM1aaymEUGICQTwXOABY7n0dANTvxsabcm0BngFeAF4EngSaPQAfDrwdeAcQ7X0+hPeVg6PkdtyWp1AdL+K2rkB1rSG0703YdSeAm1FYAQkcKoR4UmttCSFcexzAbAshHPOIgdZ6b+Bc4FTPKucu1/u7/9mI3ZiYFkv3+7f//3rv61Tve294lny+twMbECvXRQYkSOG/hU414+54FLf5X6iudeh4I9rpQgRLENqFTLuPEOXt4FbuAdkFBLJvlR2tdQg4ArgYOMHbTvyV8QA74GB2r2m3hjJCyvvS3r2fn/2BchwpbUBaSGnhJlHxTaj253G3P4jb/jw4PaBShnLIMCI8A5xuRMkSrBnHm/eXARvYBmzNfbjsAgEZIYQ2/9WnAud7QO7/9Ip+4N693pxLgpZoDVprpOVbbiGlbYOL27oS1fYcqm0lbutKdLoVYYVB2AbIVgyEAK1ApwGNXX8qIljhWXZLAn8BNvp0I29A576R1nov4HrgZM/yqt6TG4Qg7V5vUhLiGWdhgZAgvHuvUrjtL+A2PYBqfx4V34RONiGEDXYMESwH7frAMmjRng1UDiIyC7vhA571t2ygHfiJEEJpre28KYcPZq11MfAZ4Eog5v3YKSSd2b2mAZXuo0zIrDKhetbj7vg3bvMj6OQ2UBkDXBky1lZrg1HtekAW5sswaoSw0Jk4gbkfRdhFoBRIKYD7c5xBZ8yAzqEYrmeVbwOOynHy5JsNzL6hELu+Nggh3joAFqJ38/XOW8U3oVqfMZy4ZQWqey1CSJBBjxOHvNd4IM4yUbLvkSXlwgI3gSxZjD3zRH+/F54f9vUcvj42pzBXjtNanwncBNR5QLamu5OnPMqntS/RgCV9gzACD0l4z7T3HuYtzA0XQkxzsGuPTuDRCZGlGDq5FWf7v3BbHkd3r0fFG8FNIqwYIlDa+1qtQeh+4ogY1ufUKk1g1mmIYCVKOUpKWwK/FUK8kOO7jR7QHpilZ5mvzXlCpi29UMpsa2gDWikGWgl/Oa7Gtoa++KkMxNOCQMAiEgT/V/u8Qrveg2Is2vQAuAdGYfUGOpxuVLwR1boCZ/sDqM6XwU2iVQohbOPcWTGPRji9wB3N+QphrHPRQqy6kwCljXEmCXw3F4+jBnQ/mnETcHmOuZ/yYNY+xQNkTuzR/Lv3Ir+xLcXGpgw72jN09rhkXOjocWlud5hfF+TCU6sH3BOtFUJIUq0vs/OVe4iEw4iyYlKU0JqsIE4lMlxDVU0dtZVR82li8C1cezd+8oCec6GEzAGiBSqF2/I0bvtz6LZncVufQzudCCuSBbuwY55Ypz07p8eoBWiPuWrsmSchIzNAOS7StoHfAc97TMHt/0p7pGAGhNb6DuC8HK48pUPTrjJbv22JPkDMOJruhOK+pztZsaaH9i6XRFrT3J6htdOlO+GSTCtcBQFbkHY0xx9UMtSmCECx2EokfTcqkcLdCTg2YSeK1DFEoBhrSwk9sTJ2JqvYlllI7bxDmTN/T1wdRNoCWwiyrpByQEjDPSdDmchewARu6zO42x9EdawyykSqGSFDYEX7KhPeTtcXv2N9MIVxHkO12HPOMoZTSgsTnPmRF+uw+/PnXQLaA7PlvcG3PDA7HleesvulUmbHsjzu29HtsrXFgPX+FZ088Uo3rZ0GtOmMzv5+wBbYliAWkRRHrexDkExrouHhwaUIkBYVYDsI2yIadikSCikSoLpxnc04rS4VWlKqbQLrb6NpTTUbk0uZMe8QZszem05VRbSklpKI7V1/YzfGx2L3t8Yy69Tprtdwd/wLd+fj6FSLCXKgwQojglUGR0rRG+ilcHDQGqSNdroIzrnEPDS9ysajQohHvLwNZ7CX78pC+5G/z3nSnJqKYPadON9382nFAyu7eOn1BCvW9PDi60m64i5BWxCwBVIILCmIhXvlC//+KgXKJwDC8Geldg0QgeNxRoXSGheR9ZWFEIiAiSoF0WitqAhspTrYCDv+ROvGKNvS86mpX0p32b445Ucxe1Zdn/c3KorMH8j9lYnudajWFaj253BankH3bEDIAMiAsdp2xPy+VsZyDuNr5A9mCSqFjM3Drj/Fu6FCAmngWs/I6qHewh6Bzvxh4Bt9r8TUAbKrjLPm6/A7Oxx+9VAbT7zUzZpNSXZ2OETDkkhIUlFs91UxPGVj6MuT3xL9Ux10bsKDCZpmdBBklPJiRbVYj+pZRVvz75GReby25gDcGaeyYM/9wbKxhQBctBIIKcd8VGgXHd+I0/Qgbst/0PGNqPhmUGmEXYQIlnlWWPdSCf9cxpPfC5MRoZ0Edv1piHCtoTTGGbWAJUKIF7TWVr/tYXhAeybd1VovAW70rr6eSpzZ7EKGH/ckFCvXJrj7gVaeXdNDT0rhOIYm1JYHcLVGK3C1ZmotnQW9o8AhBCJCaYlCqE1E4xsQm/7G2jXzsGaeRvm8YymrmUlQjoGOaJNa4e74F+n1t6N73jDKhJtGyIBx6Cg2AFKOF+BgQu2XRiDcBLJoPnb9yTk2VGmQFnCz1voFIcSruVHqYQHtO4Fa6xLgLmDGVJHmdI6RkBIcR/P7Rzv4vyc6eGRVF+GAJGgLQgFBOGjSajPuVAPxkHYTP6fHdUETJhAyUt8esTWo1ut4Y9P/0tVwErrmvSzYY25fsA5HRbyf68Q20i99BZ1qAjsGwkYEgobK+I7heFvh4eAsBBqNXXcCMtqQe17Cw2AtcJvW+iQgqbUWI9KhvcDJZcBhnmmfdDDn6sCpjOa3/2rjD4+289KGBFpBZbGdVTVyZbrpugTKOweBQxCsMIsrG+nc8V3iO//AS28cQ+mij1BbP5egJQGF1kNIfl6SjwhVYM86jfT6HyIwr0GrKcIhBSiFCFURmHu2sc59H1Lbw+I7gMuFENfnKHCDA9rP+tdaLwM+5z2ycrKtstYGzC2dDvc93cXP7tvJluYMjquJRSQCgeNOcwTvkou7xDMhQtEIUZooTv2CxAt/5pWXjqd22VlU1C8mZPXq4gPAIgSIEIE9L0erFM7rd0GgeIo8+dpk2Tnt2LM+iQhVD+XYSO8HV2ut/yiEeMnH7HCUw/KcwBi9pQSTSi+EEPzl8Q5+/PedPPdagpKoJGALQkHhqQ9vXjD3uZtCoZUiQwg7oCnWSZbyK7Y+/Q9eq/wopYs/RkNdSa8kNwDYxsELLrkaNDhv3AV2MX1z8yflzIx6Eq7HnnVaLwAG7jZeEggx4BteqrLOpR4yB8i2h/T3YnKZJw3MrupNumrckeay7zfy2ds38+qmJFWldja3YtdS2pvVYiuj1iBIUcLM8iQNXbeQeubjrFpxP8mM8MDs9jPAnqOnXYJLrsKe/0m00z08/56QE5Jopxt71vuQ0XpP2ZDDuRvKw+jJHmatPhba4yKuV2lymfcL7mTQDVeZhKB4SvGHR9u55XfNtHU7xMIS6WnCu1eubXNJZyShSDEN6hW6t17O2h0nUrPvRdTMbEAK7Rk7kQNqc1uDS64G5eJs+IlHP9SkPJ6oJDI2G3vWqX1c5F0A2gIu0Vr/nxAi7Vtp2esHCg28C5MK2gf1E8eVDZhXvZHgUzc28uUfbyORVhSFJWgD9t1rMJ9Po5SLS4RY2GaxdS/xJ85i9ZP30JP2HUU1QFNBawJLr8We9wnIdNNbizHB8o5ysWqPQ8bmD0GVBiy/gOQdwPG5JyVzVA0JXDU5FENnKcY9D7Vx3rc28sTLXZQVWUjhpXXuxu0IlolQZihhdmkLNdu+xKaHr2XTlhbvVhuqkks/hNYEl1yDNe9j4HROPJfWCoJlBOadM5iyMZL1mVzHUGqthUc5jsKUlzORVEMpk3Oxs8Phih9s4cs/2UZPQlEas3fTizEbPZeUG6S4KMJ8dQ9d//kEb7z6JCm3fyBGZJ2V0F5fwJrzUY+/ToSV9qpbnG7sWachIqPuaOFj9AjgKB/HEpNTqoFPZB/ziTkdE3qU8MK6BB+7YQN/eqyNSFhi22I3mAvgOLpK48piFpW9RmT1f/HGk3ewsy3hFSDkWGrv36Fl1yGK9wQ3PQHUQ4DOQKjGrxUci4ToCxef8DAsJaC01jMwDUAmji8rcyR/+U8HF9y0iXVbUpQV2yilp0xQRAiT8GTldI4Y9gZl0y+nSrqLBu2SVhEqihW1zd9EtDyMzuXUOYpC+tVvoeObjCY8ntRDA8Iyykb9Kcjo3F1HO4dfh3sYVrYQQmutD8W04WK874bWoLTGkoJb7t3BHX/eiZRQFLFwJ9kq+zzez0x2lSblaLqTikR6F8emHXC6DVBEyEs9EDnAmiStV1gIN0WGAMX7XYc959jeChQvB1o7XaRfug5nyx9NTsd4Uw4hwE0hwjOx608zQB6bwuIfaD3wNiHEn/3AyiJPwhvXnA2lPUun4Es/3sov/tlKWZGVBc9kgNc8YOZ6ZlyN6+psCL28xKKhJkQsLFk6Nzz4ffa+KYKVyKpDId2JTjWjkk3ZZilCWCACvSD3LdF4g1zY4PYgAuWEll3nN2nxtGhjEVV8E+kXv4BqeQwRKMtJ2B9nZQMHq+ZoZMme+VhnP8cjCCwB/mx76sY+E+H8SQnxpOLqO7fwl8c7qCyxcSYQyCapSYCGZFqRcTQBSxAICIqikn0XRFk8J0xDTZD6qgClMUlJzCIakgQDYljfRJbtQ2i/W0ClIdOFdjxgxxtR3RvQbStRiUaTyeYmPPEp4m3vqvAasLQh04WIziW477ewyvbtpRfKBWnjtr9AetUX0J2vwkSB2QeDXUxg/rmec1iQHWEfrbW0MVXbB4wn3fDztjvjLlfetoUHVnRSWTpxKobhwgJXadq7HISAObUhGmqC7LNHmIP3KmLv+WGKI9bYr62wTN8IgGBF9kJaOZRExzfhtDyFal2J7l7XW9JkF4EVpk9ldV4nLCHdgShbTmjf7yBj83rzirUH5uZHSL34eUg3Q6Bk4sAsLHA6sBtON8c15rrDAbTjAKBOaK0PAx6ht99cwcEsBLR1OVx+62YeebGH8mKJ644/iAUmGOO4mkRKUVVq87YlMQ5ZGuPQvWLsUR8a1F/JrU7K7cexa7Drgd66GKImwo3j7nwCt/Up1M4ncDtfMdREhnrbBIwa3N5nud1Y1UcR3OcGRKiG/t09nS1/JPPy19AqafplTFCE0EBXgwwRPvSXyKKF+TqDuWqHBt5uA4fQG+oeFzCnMoZmPPpiNxVFFs44g9mSxhrHU4pISLLvwignHFzCwUtizKsLZtNQ/TYGUvQW0YoceXbMxmLIF+c4h1YUq/ZYrNpj0cntqK41ONv/ibvjEUjuMDfZCmcT80dklbUCN46sfx+hpdeBHe1tq+UdW+b1n5Be/S2EFZhQMJt85wA63Upg3unI2B6FArMPaBtYbnsOYcHphtaglEZKwRfu2sY/n+miqsQe14R7geHIXQmXSFBy1PJiznlPJQctiWHlXDfHNSDu38ZgQrwhkat8GDCJcC1WuBar+kh0ug1nw89wtv4V1fMGwooaPjwc8IQ0KotysOafR3DxZ3s/ww8IqwzptTfhvH6Xec+xKwtjJfWm+Ux4BnbD+73Pdwv45gDsZwOV42YpLcHXf9HEbx9uo6p0/MDs04uMq0kkFIfvHeMj76rguANK+mrf3o4xXMOY3N/PpSCIXWnRehCqMdwDI3obt2Q7EklEsJzAosuwZr0fd/PvyGz8JWQ6wIoOTkOEl3oJBJZcS2DuR8mGrbSJxulMB+lXvo67+V4vXZQCgFn0pVkjuUk4WNVHIUuW5kiHBV1VNlBWaAvtKqMz3/qHHfz4bzupLLHGzQG0JDguJNKK+qoA/3VyNe89rJRYWKJ0b4Gy2AWN8PVxkyA1kIKMzvrmvnFOT/ehttf+39cuMtqAXHQ51ox3k1l3G+72Bw0I7GgvDREWuEmwYwT3vh57xvG9fFkpkDYqsZX0i1ejWv4Ddgn5B4L9xKaMOQ4Z2jWotfcAWUUE5n+ykMpG/6erzMbUaRVsua7GsgS/e7iNW3/fTGnMYryUOUtCT1JRFLZ437EVfOrUKmaUB7K0YiSW2O9nZ0mBlXORk2lFd0LRk1R09rhEQ3JQJzLr6mQ60D0bTAOWQBFYMaNe5FohrYZ3FHOVAO93ZckSQvt/H3fbX8ms+xGq8xUDamGjnW5kuI7g8v/Gqjw4x/kzvS1U11rSL3wG1bnagDnvLd5shTrdTmDux9Ba4276pWf1h3lQpAWZTqz6DyOLF4wnrym3gZqCyYva0IxVbyS48Z4dBG05wOkvFMUA6Iwr9l0Q4Yozajl0aSx7DIKhaYVPPfwuAH4/u7SjePmNJK9sSLJ2c4qm1gwtHQ4tnQ6btmc4fJ8YP71m7iBdvMzWqVqeIrny04hgCTJUC8FKRLgKGZ1jOs+XLUOE6we+djALnfs9z3Gy6k7Cqjyc9Ov/g7Px55BpR5YsJbT82znBid7ifLf1GVLPXwmpHWAXFQbMaMjECcz/FIHFVyJkgLQQOBvvzrH+evBdKlBKYM6H+5zTOKy6glIOKaCzx+XqO7bQ1mWsmiowmi1pWnllHDjruAouPb2a8mIb1wvcDMVz+/TwEL1W+D8v9fCPZzpZvyXF9tYM21ozuG5vFyXbhnBQUBK1h5dMZdD0s1BJVM9G6F6P1g6oDCJYjojMQITrsSoPxqp7NzI6Owe07tCUJBfYwTKCi6/EKt8bZ+v/EVh8FTIys98WLnG2/Z3Uqi8gVMIEb/IFs5CgHLR2CSz+HME9zss6tcGlX0IjcTf+YqCerTXIADrTSmD2WcjiheMFZpHLoUOF4s0Cwdf/t4k1m5KUxayCRwH9SpayIpsrzqjhA+8o78PZh9s5pOcMph3NKxuSPPRsF/c93UFTi0M6Y/okBQOS0pidDYn77RMzjh5BaF57N1OCZfoj+c3r0Q66ZxOq63VUy5NkXv8RVvl+WHUnY1Xs35s6mU0UEkMoGcbZs2rfhVX7rhxrJ7yHTeBs+IWR5fD4bb7On+90CpvgsusIzHp/72d6jWFCy75MSmjcDT6oc3YelUKEqk1GnR/YGb8VLEjehs+b//5UJ3/9TwfFUQunwIqQZUFHt2L5HhG+/Ik69pkfyVKZocDsA1l6PtLfn+rgr0908OiL3STTRqO2pDB960SvFR+M3owqfDBAOxamL5zlbxUp3ObHcJoeQBYtwJp5oumyWbSgn3IgBuFaold26U3CRWuHzOqbTDmVNA9UQcDsJCBUSXCfG7Crj2JgEr6pvggt/RJpwNnwv72cWghwM1hVRyDLlmdVl/FcBRgaBEIKNjSluf7n23LZVsH2EksK2rsd3rFvMd84v57qMjtbezgUkIVHgRxX8+fHO7j33+28sD5OKq0ojloEAyZV1f/98U2E6y/pSRAhhBVBJ7aQWXsL7pY/YlUfhT3vY6YUKWuxrWGUBk+WcxKkX/oS7pY/GocRkb+aIWxwOpHFiwnucwOydO8hjkeYJuZaENzri6AFmY2/6G10bkVzcjbGX+0uiIWWAr55dxPNHQ7FEVnQ2j8hoL3H5X1HlPHVc2YSCZmwuTXEg+4nQQE8s7qHG+/Zwar1CVyliYQkoYBpSDP5SdceRbE8YCebcTb9Gmfr3wjMPRt77llmBsnQRMpUS6dbST53JbrlMQgUFSaDT1iQbkNWv53g3t9ARmeZpCppDzF2wwM1FsFlXwY0zsb/RbsJAnPOQpbsRQFyNsYf0D53/d3DbTz8XBdF4cKB2b9mPSnFOSdWctWHarGk6cMxGJhzG5pva8nwP3/bye8ebs9SC4FpDTbl+tv5LbhkwKSYqhSZ127G3flvAvPOxao91oSo+wDC9P9R3a+Tev4KVOfLCLu4QLKcQGfasepPNhUsgTLv+CzvvoghwNn7/eDSL0KgGNXxIoGFn6bvTJYpCmi/Ze+WnRl++KedSEsUdNcWwqSaXnhKNZeeXpMNegzWdDObZw3cv6KLm+7ZztrGJCVRy1NapkORrc46doQqcVueAOUgKw/ua6n9POa2laReuBoV31BAMANOD/acjxHc6xqE/yBpgRYCgcP69RvYY48Fw0t7wiK455XGmZSBsTojEwtojeno+csHWnl9W8rkNhcoGmhJMwri3BOruPT0Gi+/Rgx6TXwwpzKa//m/ndz+52ZcF8qKbJTWqOlWmigsSO7Eqj2e4LKvel3ydbZBDMLCbbqP9MtfRafbCgdm7YJysRdeQnDhRTkc3QOjm8R59TrcNStoKv0DM6qKhqAQOR6UDEwY1cjS3zEHUKTgtc0pfnl/K6WxwpVPWdJ03D/jHeVc/eHa3pFqYohdwuuudMUPGvnuPTuQwnQfdadQbeLI3V+JznQi608mtN/Nhrtmux0ZsGc2/ILUC59DpzsKpDFbpi+0tAksvc4Dsz9z0IuCpltJPXcF6Y2/Y2a4kW3P3W6auQ/52bl5HhNbXznmuheAm3+7g3hKIWVhtnTLEnT0uJx0WBlfOWdm9mYOdkkcz/l76Y04539nI/c/00VJkexVLZhmYBaYkv65HyO0/FuIQE6POu+JTq++icyrN3hKXaAwYHbjiGAFwX1vIjD7jN73VG4vT195IWr7P9CBUkLBADU9v+eNtavQwkINKQ1OTm98ORbrLAT885lOHnmxm2hYFqTHnBTQk1ActrSI6z9ZR8AWgymxWctsS3jutTgX3byZDU1pSqLWuBcNjBuYtQtuGnvRpYSWftFw15xuldpNkFp1Lc76H4IV8nTg/JUM7XQhovMIHXg7Vs3ROVUtJrHJbXuW1MpPodqehWAZQjukdZCqUDPp1+6kK+4ghchpiTD5a1SA1t6fPUnFXf/XgnJ1Qbi+EJBxoK7S5vpPzqQoYmUfnKFkuade7eGC7zbS3G5C7I6ahn08RrzdX4mz6deeLEdhZDmnE1m2P6GD7kCWLsvKgL5T52x/kPSzn0bHG01zdOX0auOBYipSD9C66TlUQaMOEwxo10uMf2BlF6vWx4mGrYJYZz9189qzZjC7NthHtegrExowr1wT55LvNdLV4xIKiunZ826U272pyFZ5gsejAU4XsuadhA+60+SUaC9Lz2uwntn0G9LPXY7OdIEM94k4mrxzqCp2aXr5l7guEzd+rpCA9vOEk2nN/Ss6ybgaWYDzkFIQTynOPamSdx5U4l2goRxRWNOY5IofNNKdUISDIhvtm25gHu12nz9f9qKHbhyr4YOE9r/FRPOyWXrG0qbX3Ub65S95PmqAwSKOEkVGhVkYepjXVr+CP6Fr2gEaAWs2Jfnn050F6T0nBCSSLgftGeW8E6u9kq2BvFlpjRRm0usl32ukud0hPIGWWYyoxlDsOs+5z3a/7+i3+3xus3ZBZQgsvITg3tebsW3ZdE/D09OvfA1nzU0IGfFeo4Y9jyA9iM0/J6OnDu2QI7ek5lb97B8tRtUowBOpNYSCks+cWUtxVHrzrwf+jkDQ0uly1R1b2NCUNuFvNf4gti0z/zvjaNLOrqoyXK8nnDJ5EIONhUB62/2xhA+6q992r3e53Y/tRCToNCYs/VUCCy/yRlz0lmipVAvp56/E2fBz0yd6BB39ldZEQgFKko+xcd1qhJAo5U4fQANs3J7m4ee6CAVk3tKYJaE74XLmMRXstzBqooBiaHDd/LvtPLc2QVF0fMEsvQm0jqtp6XSIpxQ1ZXa2EmbIZccQ0Vkgg+j0TnB66NvvToPbg9VwBqH9vz9wuxeS9Lof7nK7HzWY3QTYxQT3+67XFDFnMIOwUPFG0isvwN1+v5f6ObIbK9CkCVMithBq+SvJjBpB/7/xXyOKFPrpoXf9dSc9KWXq9VQ+oBEk0ooFs8J88qTKrIIihlA0fvlAK795qI3i6Pj08/CLbB0Xko7pqDR3Roj3HhbjgEVR9poTZkalPUQrMJPjYJXvT+jA29E9m3DbVnqTWZ9HOwkvhAz2gk8TWHhJTgNyL/XTTZBZ8x2cN37uzQukMJbZjSMiswnu+x2ssuW9yfVeXoXbvor081egE5u8qpbRfaZSLrFYlPWv3094j7MJV9cMMbRoCgHa5E8IWrtcVr2eMEnreVhnM/HVbHfnn1RFVak9aIK+3wLh6Vd7uPE3OwgH5bhE/kzSvyKV1lSW2hy1vJRj9i9m8WzTWWnkT2kQGZ0D0TlY1UegM53o+Ebclqdwm/6B3XA6dsMHYcB230rmpS/iNt3XNzk+byWjG1m6nOC+NyJjc/qBGdyWJ0k9d1lvRfkYPleiSboh9iheT9PW5ymveie2mOIW2lGm/9sjL3SzelOSaFjmdc2F19/uwD2jnPL2Uo9qiIG8WQh6Ei433rODnqRLLGwVtKGj/5FtXQ41ZTbvPbSET763inkzgn2Ow1W9jWh26RjmyGoiUIIo3RtZujeBuR83vTV6J6N62/1m0s9/BtW+clTb/a5F/S5k7XEE974eGarql8fs7QqZTnRyByJUkW2DMDY/SBMJWWx59T7m7/MuJruV8C4B7Y+EWPV6nERaUxwRZPIw0dorpP3IuyqwLNEnf7n/ffnVg22sXNNjckUKCGYpIZXWWBJOP6qcM44u54A9o30kwlzHcFRPa++Z9qZMypx+y56ldDteIv3clejEhjFt94NbZiDThT3nQwSXXONZ3n5J+d6v2dWH49QcibvzMUQeFeECRUYHWVL0NNubmplZVz0JGRwjBLSfhLS5OcP9K7oojeZXJygEpNOaZXuEs01g+oPZx8Ab21L8z992epa5cFZZCuhOKObUBvnC2XUcudw0WMy1xIVxbgYz6aZ8yWl+hPQLV0OmDaxYYYpYtUKrBIFFlxJccKEH4sFKnjwJz4pi178P1fJ0r9IyRkMlhEWIVlat+icz685iSCs1VVSOjU0pGnekCQREXruiFIKUozn7+Epsa/D30tqUTX33tzto6XCHLLMaK8XojCuOWl7MXVfN5sjlRVlakdtcpvBLZy+3s+UPpJ+7DJwOr3edm/8tVBmvAvs6ggsvxrQq1P0+e+ADYM84Dlm61CgheZy8RmALh7nWY6TSanJGhY8E0IZuaO59pINwQOYVlZNS0JN0OWhxlKOWFw96qV0vsPLEy9086ic+FYhWKmXaFpx3UhXfv6yB2TUhY/kFw1aM5796O39m1v+I9KovmB7SokBFrCqJsGMEl3+HwOwP05sHkuMgDkVPrBjWrNONbp7HsSgN4VAA1fkKr61dawoQJmkq6i7tn6tMbZ5t5+ezCC9A8c4DSyiKSG+L76emCIHjaP7wWAedcTU6/jrMQ+kqCAUE1587k899qJaQt9NYcrznb5hSKa1dUi9/jcya7xjrKe38NWZpg9ODiNQT3P9W7Lrje/t7eBRCdbyE2/7C4Jba4/uB+pMRkRm9rx0jj1YECbjbKdUvZb83pQDtg3fV+gQ9CTVAiRgtmJNpxeyaEEd5nFUgBuXOL29I8MCKLkqiMm9H0GeFmYzm6rNm8P6jyrOJ/+O+LXrOn850knruUtyNP/c69sv81QxhQbrdhM8PvB2r4iATHvc7egoLd+fjJJ/+BM76OxlQdZ67rAhW7XGmR14eFyWjJFWxBE77GjqSGjHVOLTf8ejPj3cQT+XHZX3rvGh2iD3qQyY3Qw5UHkDzs3+2ksqobLOXfKlGd8Ll/JMrOe3IMi+LT0wAx/Pq/hJbSK64ALXtvt6un/lmywkLnWlH1hxH6IAfeL08eotYERbO5t+Sfu4ydKYTt20FqnXl4O1rPZph1RyNzrPXvVaKYDDClg2raG/t8HYmNXUA7V/3xua0aVCeBwiUt+W/Y98iryORGHQ3WL81zSPPd3l8Pb8T8xs5nvz2ci4+rSYbWi8MmDVD5jt43ZNUx6ukVnwK1fZ0TousfMEs0JlO7IYzCe1/MyJUjV/E6vPl9LrbSL/0ZbSbQARK0IntONv+nuMZD2z5K4sXYZXt4zmHcozGQ5PRQeqC6yi1d04tp9DXine0OTS1OqZ6JI97oTTEIhbvOrBkUFnMZxb3PdVJIqXzbn3mg3nZvDDXnlVrFJVCgFi7OVzTb/Plf1/lbPdPkFp5AbprLSJQqCJWBW4Ce49PEdrnBoQV6dvNVGVIvfw1nLU3m7YHwgadQQSiuC1PolPNg9xu874iWIGsPATtJhjrVBKBJqNs6kta2bZxFY5mUtQOObgjaG7/yrVxGnekCQXFmNUGv3vR4tkhKkvtXB+779PtaB57qRvH0XnrwK42zRbPf291djjR2N4z1wr7iUbefD+nB9yUlwjiJSAJC2fLn0k/dwk62eSlfhaiiDUDQhBYfA2hxZ/J0bQ9KGU6Sb1wFc6Gn3q6Nr28WYZRXatRHS/33Q77qDBglS7Nu+Wu1gppBdix4QlcByZwwnZ22cOppttaMnR0O1SXBcae++zx5wP3jPVx/nIfHksKnn8jzsamNME8czakMM1pjti7iGP3Lzb1h2NRS3IjbCqN27YCZ+t96J71xgFTafNzGQC7CKvmGHRqO84bPzavtQtUke0mEIEygku/hFV3woDRbCqxjfSL16CaH0WESgd5gDRCWLjbH8CqeccglML8X5QsRoZr0YktObNdRu+EK20zJ7iKgDWuIy9HB2iZDUK4eYcxBaaqZN8FkezDIgbhzyvXxNm6M0NVnuPelIZISHLeSVXY9hh3Fq/ns0634TbdR2bDz9DxLaAzaOV42WSGj2qv9ZZqeSJbbWK2+zwdD6+qRUbnENr7BmTl2wZUtaiOl0mvusY0QQ+WDr4baA0yiNvyuNlRrNBAzxmFjM1DhKvR8Y150TKlJdWRFlLJBJFo8eRTDr/UqiepeHFdgkhQjrl9lsRY5/rqYG/mmu7Pd00+x/otqbyjgv5gz3cdUMLBe8WGzbEe1hMWErfpH6Se/jipVV8wlSNCgAwbTmzHTFNEO4YIFJnvCctr2SWzbXjzBnPJUkIH3mnA3KeqReI0P0Zy5QWozjVGQRlyN/CLbdtR3Wv70IxeHq0953CJuWs6v3uQyWR4+dX1Hp70JAPa+3tnh8PrTSmCgbFTACEFyYxiyZwwNWWBXMc6a02FgJZOh9UbU4SDY49GCi+AUlZkcc6JlWMUyLzRZ+tvJ/XcFajOV01xqgj0qtpaDf6Vw7lFboRuVBfPa3rudCErDyf8th8ji+aTjTb6VS1b/kj6uUsg3ZIzc2VX1CWN27piCOXQHKtVcZBnwVVekFIqQ9uO9WM4//FwCr3PjycVrX4uxVgB7SUjzakNEgmJga0JvPfd2pJmTWOSUGDszqclBF09LkcuL2LRrFDveL4RKxgGmOmXvkJmzY2GOthFZlwaeYZIR/wwach0Y808lfABt5medjpnaKaQZF7/MelVn/f6xo0wfC4E6DSq7fnBtXB/XnnxIm9Uc37yosClmE1TR7YDSGc0PUkXy8rv9DSamjLL06P1YIaBdZvTpDNqkDatoyMLtiXYf88o0hu8OeK30942vumXOI2/MkrBsK2uRu3+j+A2KFBp7PnnEdznGwg70gfMWjmkV3+LzKvfQEirz2ChkT5YKrHJ05otBjRkB2S4zlhoPXbPyTTVdakNN3lvracGoLuTymsEPtb4vlEwomGLytIAwzmfb2xLYdlizNzTn1Y7syrAfgui3nuP8Lj9bp4dL5Je/e3ebpmFrGIe7lj8IlYNwSXXElzyOQNYf26KAJ3uIP3i1TjrbodADPQot02tjf+f6UQltw79kFkhA+p8pDsEtqUot5tIu+RlpAoCaP/zX92YxJZjB5k/Kq84KqkpDwx7X7fsTBsA5kFtMo5mZlWQPRvCo8vV8Byt9NpbPesVYNxK8vuDSFholQIZIbj8v7GzQzPpw5lVx0s4jb+BYPnYOycJC+10o5M7hn5ghUREZuXHoTXYlkW8p41t2/0QuJ58QG9sSmHbYsDgypF+mbkmmpKolaUcgz2trgtbmjPYNtmc+NF+mYsoeNuSKFKaPJQRAdrbst1tf0ftfLS3wmPcluj7ILkJRLCK0IE/xK47gf5D5rO/WjQXWbLEWPKsZDjKL2mD0w3J5mGPT0Zn5UyqGv3naATSskklU3R2dk640mEPZUi2tzmk0nrMSUJSQCJtCl3LiwePEAKkHMX6rSkyaT3mim5TyKs5cM9R0g2/amnjrz2NV4yvVy5yVAen28wv2fcmM4xysHFnfq5FqAZZNB+n8xWv/cEYLpSwUalm3MRmrEEttMebQ1VoJ24a0Yzp4RYIkSaTipNKJSfcKRwylHPUvkU0VAcI2GOrUhEC0o5mRnmAosjQk49cF844usIbCzd2h1BKWFg/igl1PnfuWm3K+EVgwiQmnenEqj6C4N43ICN12fklQz4BMoBV9x4I1SDssVVo+9q2VbxoWPfJKl1OYI9zvd7Tagz3QiCkS1lVEeHKogkHtNBaT8PmcIVAlQPCxmn8rRlSOVagjNJ6ae1g151IcK/Pmx7QQ0662r0KaqGVIv+Il6dK7irXu1AV3VKKUVt5lW4BN2405/GustAuwooQ3OuLZh74aOb25d15NNcJFsPvdwV5sA2fnmiVwx4aHDBRxejWpPSQ8j4z3cbEFt1rU+0diI3uYyesG5Eo2I4xGXd16jT2nVi+kR3Tq5NNXifOCWBeQoByUPHNTHZDljfrkm/dUzdevZARr1RoggAmpOHru9fEUg5dwNl+u2IUhWqKNHLmInqdsVCFl68xAYD20v9EeMaYXqrzzeXVu+51XZDPydmQxFQB9EQezOS0YfV60AUrvGqTiZjOqRGBchPxGyXLFIWohxQT9DlTzUJr4LEXu9nWmvHC32O7do6rKSuyOHr/YgJeXV//a5VIKe57umt0yUSDGz6OO6CEkpgc1d21SvdGhuvQTs8gSTuFpRq4SazKt2fb64703IQw5XBrG5OExljRIwXEU4pl8yIs3yMyYI6Nf2/Wb03zzOoegrbIe5rLIXtFmVk19MycCQO0AO59pJ2/P9FBJDS27kV+pHBhfZAD94xRUWIN2gRaa/jm3dvoTqgxqx1KmzFvc2tD7L9nZGS5HF4OhyxbjgjXoTtfNon74ybLS7Sbwqo5wjtxtUvlIvc8fv1gG797uI2yYntMQ05tW7CjNcPnz57B8j0iptGPNRDRj73YzVd+uo2S2Ngq74UwHWuLwhbfv3QWM6uCJvQ9QWZ/yCtaU2abFADLSHhj+bKkGZAZT6mcTb7vCgUkM6sCpiXXGD/LtoyC3LgzzZAfNCiP1mCFsWYcX5jZf8M8PNqNI8v2RpbvP+qXd8cVzR0OwaAZkTGW+4CG4iLJjIohCpX9z0q4WPbY77n02FtRVOakPEziaGTfQNVVBFBq7AejMa0QuhMuO9oz3nsPBIxlQUN1EOWOnbNLL330Py91G4s9UmD67bDmnY2IzvNmpBRa+PGS57UiMPdsL2nfHZGu7F+uLTvTvLAuTmSsFT2e1SyJWINWDuUiesvODFYeM4CEt2NGgpKyYmvwz5pQQHt/z60L5R3Bs6Xp9rm9NTOs5ZxZHch2asoHNq9uTNLe7WLJ0eSfaLCiBBZegNZuduBl4fDsddOvOAC77j3ZYoKRvhTg2dcSdPR45zXGw1BKE4tIqr3c9MEMlVImy1Lm8Tl4bStKopLqUtsffTh5gPbPpLzYGq0jPsC6WJYpi2ryAK2HkOvm1IZwlB5zmFRpCAcljTvSrFzTA4wynK4Vdt2JBOZ81KRY7jI8PIrL66YQ4VoCe33BcPRRXFRzOTQPruw09ZZjfOh9B70kajGzOjDkIWQcxebmTF4Pjn/vo2HzAcrVEyrdDWkqApYgHBToPK200rCjzfWogRj04Vk8J+T1ix77Z9mWoK3L5fl1icE/a7jbLczItcDiK5E1x5q5I0LkF24WNugUWGECS7+KVbJXztSrkYFCa9i4PcOL6xNGJdJjt5pKQW1FgKKIoS39O78CNLc7dCfUgI5ho+WaQkA0lJ9BLBygvQOIhgWVpQEcd+wcSAMBG5paM9nez3pgKRszygPMrwuRdsYu3SmtiYYFj67qpqXTGfhZuwQ1CCtCaL/vYc35aE5nJGv06BEWZNoR4TpC+92MXXvMiHlzH9FBwD+f6aQnmd/INO11kvJ7owyFso3b06Y4Ih/rjHFC6yoDk6JDyyHwTE15gD3qg6QyeuyOodaEApL1W1Ps7HAH0A4fvFWlNosawiTTeUh3yjgiL72e4K//6RjjxqwRVpjQsusILv0yhKrA6ex9MoTs/fJbDuT+H0Cl0U43Vt0JptVt9ZHZxjWjeTgFpsjiD4+0Z8E9ZpApCNqS/RZGB8WzT2VWvZ7wipXz25GDAcnee0Q8gE2BmkJXaWJhyV5zIyTT7pjHZZiuo5K1jSmaWgY6hgIzAzFgCxbWh8jkOQHYVWar+9l9rezscPpsp6MBNWjsOWcRPuRuAvPO6W2N5cTRmW5Te6gzxoI7PWbqq0obXbtkCaEDfkBov5uRRQvpbRAzGkNgWv7e869W1jSmxhwLyAVsOCRYNi+SVYUGM2PPvhYn4+Q3HkUrCAYEhyyJ5QpJE7bsoYBoSagoskbZ32IIJ9/VvPRGguULIkM+/csXRKgut8lk9CjpwkBlZevONL/4RwuXn1FrpEcxWlAD2kVGG5BLrsWedw6q5SnclidQPW+gnbhxHmUAYRdDoAyrfF+sqreb2d1+JFCPHsx+VG3TjjT3/rudaDg/P0YIyLhw0OIYRRE5uPMuTbOfrS1OXn1YjL3ShIMWJUWTU7QweG872Us7imMWjsrPOgRs07vurHdWDACX9JydAxZFmTsjyKr1CWIRmZcDZNuCPzzazvuOLGPujFB2Eu7o3sfKAlKEZ2DVn4JVf4q5aakWdLoFrDAyXAsyNNBM5eFUCgG/eaiVxh1pyorsvORTKQWpjMt7Di7JGqtcC6yUuTavbkyyvS1DMI/BUP4sm4bqQMGGPRVE5fDls+ULIsyqCpDOY46z1mBbkpVre+hOuINv8hqiYckhS2Kj1JAHt3ChgKSpzeH79zZ7OSJjfM8sIHVv/2etEaFKZPEiM3xehnr7Q+f0xhvLvuYqY52ffrWHXz/UTnE0v/mMQpiGQfPrQuzjO4SDC0289HqCne0ZArYcs4H220kcsbw47/tYUEBLj0fPqQ0yszJAOpMf75AS2roVj7zYg2ZguqjfkuOEg0sJBkTe3ftdL5fgL//p4Ed/2ZkXhelVLqTXB7pfjzu/WCBP7dqneRubUnz+rq0kU/kPg7ekoCvucMjSGHWVgQH0UXvtH7oTiqdejRO0ZZ6DoQQZR3PIXtEx+C/jrEOTo3ZYBeion0or/vlUp7mg/c7UT1VdMjfMXnPDOAUQ47XWRMOS7/++mT880o6UFGyAZx+FowBevN/zr6PH5ao7trKxKe0FUgrwYEds3r4sZkLS/TIa/dvwxrYUz6zuIZrn+GnX1ZSX2MTCk1c3MuQn+4GJ4w4sJhyUY+6ZkXU8LMHLGxNsbs4g5UArrLzt9ux3V5DKqLzrDLW301gSbri7if+81I0lIeNOrSL3bGqlgK/9bBsr18YpLsAEMClNau7yPcIcs3+JmQrcz4/wL/EjL3aTSCtkHn3o/BbMBy+JMrs22EeWnRKA9g/myH2KiITz5EMawkFB444MT77c00f77EM7NBy2VxEHL4nRlch/iqzyttR4UvGVnzbx/GtxApYooKUuDJiTacU3727i/57spDgiC3J8hsIIPnh0OaHAEG3WPAXqH093msSnPCcnJNOK/RZGCAclGVdPLUD7K2BL9mwI55eA7/GrtKN5/OVu0s7AsW7Cm1pbErM45Ygy73WF2c5DAcHm5jSfumkTf3q8PfugTNKwU+NHeDvSjnaHy2/dzE//3kIkWBhHyjhnsGhWiBMOKe2jXOUCHuCZ1XE2bktjWfl9tuNqKopt5tSGBvM9pwagfYnnzGMrSKTyG77puprSmMX9K7p4ZUMCIQbSDul5xu89tJT9FkZIJN2CzED3Qd2TVFxzx1Zu/f0OtDcr0XH1hDovrtIIba7rKxsSXHDjJh5Y2UUsYixkIQ7FDGpS/NfJ1QR2MZbj7vtbSGZUXruhlGauzeLZYY7at3hQejNlLDTAgpkhSmJWAVI8NemM4u772xCDWA0/ThcJSS44pQY7T6+7P6gDliAUEHzv3mauun0LTa0ZbEtk9dPxtsr+gCQF/P2pTi64qZGXNyTMtS3Q59uWoLNHcewBxRy9vwGXGEwelEYefOrVOKFg/pRSCJhXFzQJbVpPTQtt5C5NfXWAo/crpqtH5fXk+QN9Hny2i9Wbkn100FzrorXprXfyYaV0JVTBGtEoL9W5OCr50+MdfOwbG/jff7aSTKvsw6U0BbOUWpsHxQeVJQXPr49z5W2NfOa2zbR2ukTDckwlVUNRjbSjKC+2uOCUKm+m+UCqKNA4LvzpsQ52djh5y3Um5UByxjEVnjM+eVW2IxpeHwlJDlgUNTcmz2tvhmK6/ORvLR6t0YNaM63hU6dUMasySCqjClZk6YOsKCJp3J7haz/fxtk3bOD+FV3ZYlvpbRWOq0dtObU2ltj1nCIpzfs1tzt8/edNfOo7jfzl8Q6CAYFtF3ZnkAKSKc2Hj6tg7/nRrGM4kEYK1m1O8venOimLWXlNHfN9n6oSm/0WRLLtl6csoKUXZTt87yIWzAoRT+eXjaU1hGzBw893sXJtPDsFq/+NUVrTUBPk8jOqcT2eW8jr5CoIhwSxiGTV6wmuuLWRi29p5PFVPWzdmUEIs31L6VlZNQIgeyC2pMDy1JXVm5L85G8tnHHd6/z8n630JFzKiuze/hcFA7OgO67Yb1GU895b5bUYHgSA3vf+528t9CTdvMHnV5OfdmQ5g265E7x2ORnRBCQ0c+uC7DUnxIamlMeTxg7oQEDS2ulw55+b+d4lDaZkXvcdKm9J48yccngZz69LcPcDrQWTtPp7+r5k9eDKLv7+ZAeLGsLstzDKvgsi7LcwysJZoeyxDwUAIfwaSsWza+M8vy7OC+sSPLs2TmfcJRaW2SoOp8BauBSCjKupLrP52rl1xMKDUwhXaywheOjZLv72ZAfRUP7BG59GHntAcdYXkWIKAzrXSr/viHIeerYr/9C0qymJ2fzr+W7+9kQnpx1VNqjz4mvTV55Zy6sbE7ywLkk0XHgd2b+p4aAgGrJp3JFm9aYk9/5b0FATpKLE5qjlRXz6fdWDPBSmqfvLG5Lc8rvtbG9z2bIzTXO7QzAgiIVkNsFoPNQUIXw9WXH5B+tYNCucdT4HaN4I2rtcvn/vjuzDmc8xmSohhxMPLWXOjOBoyiUnV+Xwu+kcsU8Ri+dEyDj5e7FKaYK24NY/7GBnh8NgI1Z81aMoLLnuE3WUF0tSGWNlxkWJ8PhvwBZUltjEIpLtbRmefKWHlWvjg958/1tNLWn+/UI3r25MkHE0VaU2xV6j90K1Cx5qy+/ocvjwsZW8/8gyXDV4N1ftxRF++WArL21I5p1j7e9w4aDk+INKCAVEXk3rJxTQuZr0ue+pJJHSyDzTXTUQtAVbWjJ873c7sll3g90wV8Hi2RFuvLCBcECQdtS4bms6xyEM2ILiqCQaGv5S2bL393x921V6XCmlJaG9W3HqEWVc9eHaISfnKkU2RfTn97V4YM4/tN6TdNl7foRjDyhBaT1p2vOYAO0bxYMWR1k2P0w8qfIOemivf8O9/27nj4+1Z7P8BrtxSsOhy2L896fqPcBMDFfLld529YD6vzcRgRrbEnT0uBz/tmK+ft5MggHRW+87CPi6k4qv/HQb7d0utsz/GP38nPceVkY4aHjPVGiJNypAO66mosTmzGPKyTj5n0I2gciCb/2yiVc3JrLO4FCW+tgDSvjqOTNxlCbj6knVPCfN8ZGC1k6HI/cp4pvn12cz88QQFEop+PavtvPs2ng2IpnvclxNQ3WAM44uy94fphOge5UHzbsPLmHp3DA9BQhNK68iubVL8dWfNWUrnIeiH77yccN5M4137+hJq46YjGVJaOt2OOaAYr71qVmUxKzhqYYU/Onxdn77cCuxAqlEtiXoSSjOf2/1mIdKTQlAG2MoqCi2Oe2osqwVKIR8Fg0Lnlkd5zu/3t5Heej/+dKThk49vIybPl1PJCRIZvQkjbWYuOXr2+3divceVsotFzdQVWYPnJ/uK0nK7HyrNyb41q+aCraTWdJIk/ssiHLcgcVT7jqN2rb51vPMo8tZPDtMOlMYz1YrKIlJfvNQK3f+Zadp9OgO7lT5wY5jDyjhe5c0UF5kmbnk8s0LZrQpADjruHJuvHAW0bAcUvP1C18bm9N89vYtdHSrvGoFB6o6mo++q4LSmJUNJk1bQPsXLBSUXP6BWhxHF0R79MPdwYDklnt38PN/tHjdlIb2spWCQ5fGuP3K2SycFabL6/rzZqLVUpo6vYyruei0ar567kwsy/gZg9E9P813y840l36vkdc2pwmHREFC7FIYZePgvWKcfFjplFE28ga037vj7XvHOOWIMjq6FXYBTswX+y0p+Pavt3PPv9qMM+gOA2oNy+ZF+MnVc3jfEaXEkwrH0QU5nskGspSmlW5tuc3NFzVw2ek1ZtzeEJzZ16B3tGX4zG1beGWDCUQVKl9EY1Spz35wBgF74AjnaQ1o4TkHZx9fQVWplR2jXAhQW5aJTn7z7ib++Fg7ljV0uNjn1FWlNv/9X7P44sfqKIlZtPc4yGlqrU0NpiaeUhy7fzE/vWYuxx1YnO0xMtg5Ke3r0g6fu30Lz76WIBqRBQOzZUGHp3kvmx8e8qGaloA2FsREhpbNi5g6QEcVDDx+BXTG1Xzlp9v41YOtWfoxFKf2qclH3lnBXVfN4eTDSomndLZf3lT3GXOPsSuuqK0I8JVP1HHbFbNpqAlmA1uDO4Cm+eLmHWkuvKmRJ17uoaiAlllKQSKlWNQQ4rLTawqeWDUlAO17vFrDuSdWs8/8CPGkLkiFiW9xbK/+7/pfNHHHn5t7OwjooRQY8zAsmR3mxgsb+O5Fs5hdEySZViS8vnlyijmO/k7nur2TDs48toL//fxcPnh0RTbfYqjjdlyj8Ly2OcWFNzey0tOaC5XvYj7faNkXnVZNWZGVrfaZisvO9w00EA4Irj93Jh/5+gZSGY1tFeYJ9r11Swi++9sddPa4fObMGV4O7uBW13cWpYQTDi7h8L2L+NWDrfz58XbWNKawLeEpBHpSrYzwLJ9SmtYuh6pSm6P3L+UTJ1Sw74Jor+WVYlCr7EcmbUvw8oYEl92ymc3NaYoKnJEoBHT2uJx5TDknHVqK405t/yRvQPvh6sVzwlx2eg1f/dk2glELt0Bo8R3FSEjyo7+20BlXfPZDtZRErWFB7X98cVRy/nurOOPocv74WAd/fLSNF19PEA5IbFtgyZzeMYzfVprlvl5FjKM0iYRLUdjig8eUc+rbyzh4r1g/TiyGpGT+7JRHX+zimju39la/FLhgIJXW7DU3wtVnmdmKU93Ztgtz4oZPn/6Och55sZuHnu2itCi/Xh79QQ1mEM2vH2pl3ZYUn//oDJbNi5iKFyEGADvXqikFZUUWH393BSceUsJTr/Tw1yc6WLEmTiKlyDga2xYELOF5737D8bElF/ngFfj1ipqMo0l7DXQCtqC+KsC7Dy7luP2LWTY/klUpfEd2KM7v9+nTGn74p2bu/EszjmOKFQptmf3g1rUfmUFRxBrQF+9NC2ghTK5tOAhfO7eON7am2NKS8WraCnewSkFJ1OK51xKc/+1NfObMmmylxHAX27fYrjJJ8CcdVsqJh5bS2JzmT4+189QrcXZ2ONkvIUwmYMAWBAOm+mRXTqXI8SmU0qQyGsdRpDKKoohFdblNRbHFgvoQ7zmklEP2ihHxMvi0NhU6w0U7tTaTvixLsL01w5d/so1/PddNOCgIBgtf5CuEGSdy9Vm1HLJXbMpTjexxa104yPmgeuKVHj59UyOu1kPmZeS7FTrKBBw+eEwZF5xSTW15ILtVy12N/mXg72xuzrB+a4rXt6ZYvzXFluYMW3em2dqSYWeHywlvK+F/rpozgM/6yfQPrOzkwps24biCGZU29ZUB6qoCzK0NMW9mkAX1YRbUh4iERB8e7HfcFbvYnfzPvX9lJzf9ZgfrNicpjtlmFynw9bUkdCUUJ7ytlG9fOBNbymGP8U0LaN/rti3BT//ewtd/0URJTI5LpyL/BnfHFQtmBTnnxCpOP7I8u8XDrhUNX34aTNvtTig6elw6ul0am9MEbcHR+xUPyfEbm9OsWB1nVnWQihKLsphFaZE1wKr53VBHIiPmWsWN29N8/94d/HNFJxkHoqHCV+5oTH/teFKzsCHIT6+ZS0WxzQTOzZx6gM61Wt+8u4k7/rKTimLbTLkahxOwpCCRNnf2yH2KuPT0GpbMCefos2JEN0P5nNlzxgp1A/3SK/84RnosfvV0T1Lx+0fauOMvO9nR5mQLCMajj4iUgmRaURaz+Pnn57KgPjQtePO4A1p7fwgBn/3hZu79dzsVJXbBi0NzKYhvVcuLbT50bDnvO6KMOTOCg4JkpMfv/+3NFNrla7MBB9G7PY/08/Qgx/ePpzv51YOtPLaqm2jIMnLoOCkxfs5I0Bb89Jq57D0/MrZG8W9GQGe5n4BMRvPpmzfxr2e7KCseP1D7N8V1TQLNnNoQ79iviI8dX5nthulv436+yGQv31HN3RHSGc19z3Ty24fbeOG1BMm0yrvx+Ujom6uMY3rLxQ0c43Vcmo4ZueMG6FwHbUebw8W3bOL51xIFF/4HVVy8UK1SpvnjiYeUcuoRZSyoD2V7F/vKgvQSU8QEgtg4pn1VDTM0NMHP/9nCmsYkrqsJByWWl2IwbkbAiyM4Llz38To+cHQ5rmtyN6bjGldA54J6c3Oai25u5JUNSa//8fiemBS90bR4ShEOCg5cHOOQJTHetiTaO+JskK2ffrRhNID3r2aWrjB0QlFLh8PjL/Xw9Ooenny5hze2pQgHJQFbZI9/PO+OX9aWyiiu/vAMPn5C5bAJULsB3V8J2JHmsz/cwoo1ccqKrHGlH/2piNYQT5ogysyqAPNmBFm2R5QDF0XZb1GEspg9pPMzlNQ32t9JZzTrt6ZYsSbOs2vjvNaYZMP2NN0JRXFUErRNNfZEhOT9QmOlNJ87s5az3105Kj/jLQ1o4+0bfbO5PcPnbt/CIy90Uz7OnHowRUQIY5FSXtlWKCAI2oLFc8IcuGeUxbPD1JYHKIlJiqMWxVHLNAwfxcPbFXfpTCi6elzau102NKVYtT7JijU9NLc7OF7gxZJm/LRPKyYqt0RK4Q3YFHztnDpOPaJsWklzUwLQ0Bt4ae92uPrOrfzj6U4qSuyCdd8cDc/2s9j8NgWO0jiOxnFNMKi2MkBteYAZFTYlMdOI8IPHlA+5+6zbkuQ3D7XR2qlo7sjQ1JqhqdWhs8fFtgS2ZfIg/IfKt+RaT2w7OEuSfZi+c+Es3nlg8YDgzW5Aj4F+JFKKz9+1hT882kFFiT352W85+RdgcrEdxzSL6UoojjughJ9dM3Sk8P5nOrngpk2mubpndQO2yIbDfY17MtOIfc0+FhJ89+IGDt+7KLtzvlmWPRnA8Rv83fTpBsqLbH56n2nEKOTkJY73Jq3rrCWzQwIpJMGApDQ2vNsfDAgqS20yLgihzdQ3zYRSql1Z5q64y4zKALdcMot9F0RxlIkMvpnWpJyOn9+hgS9+rI5rPjIj6zRNFWvhUxEjaeldSmc+eF2vhZjSk95ZNkfGNFUw+y2K8uOr57Dvgiiuq990YJ40QOfyNdeFT55YyXcvmkV1mU13Qk276NSU5ZN+SVeP4gNHl/Ojz85mwcxQttfdm3HZQBoITsoFxwj4roKj9y+mvjrIFbdtZs2mJCXjHB17K4BZK4gnXS49vZaLTqvuoza9SVdKAsmpwO+UgkUNIe78zGwOXhKlM+6YotDd2Bz9tptt0qP56rn1WTD7leFv4tUpgS6fBk72TVBaM7MywA+vmM0Jh5TSk1BDdtTcvYZWMpIpTVFEcuOFszjz2PJsVPZN3C3Nx25SAq1TxrII0xGoOGpx80UNnP1u04vamWLtpqbkHdUmYNIVd5ldG+SWS2Zz3IElXlX4W+YydEhg65TaLnMquj//kRlcc1YtYFIb5W5QD22ZLWjvcjhwcZQfXtHAQYuj2VYQb6G1SQJrpwLl6A9q7VVHf+zdlXzrU7OIhiQpxy/p2u0s9np/pkdGR4/LiYeW8sMrZjOvLjRt0z/zpByvSmCN940phZLc1rknHFzCLZc2UFVik0hrr4vSWxvUfhdQoQXdcZezjqvg5otnmY6girfabubnbq6XwDqmsJjglxsdsleMO66czR4zQ3206qnclmo8QOyH0c2YDkHa0Vx8Wg1fPWdmdvrBW6kBvA8T7+81EngD2AFYU81K91VAYPGcMHd9bg4H71VEV1xlO3S+FbS93PnZti29qKrg2o/WcfH7a3JqF99yYFYeoDcBb0hgM/Cc90N3yj6CXjL6jAqb2y5v4P1HltHV42Z/luvtj3di/EQqF/75CE/msS3ojrtUFNt858J6zjqu3OPL4q2qBPl0YyWwRQoh4jmAntIwsKQBdVFEcsP59VxwajU9SdUnl1fkVJdkwc30QHg2Iy+3aibnfCwp6Ewo5tUFuf3K2RybU/u3W9bkeSFE2s+2exITAg/0XsqpC2q/QvqKM2qpKrX59q93IDADM/0gwsAbLAZUZE8ZKiH8tFrR74j7nndXwuWARVFu+vQsZlYGpl2LgXFyK/z0jecApNZaAI8Ar9M7vHVqK1XZ1rmas4+v5BvenL5Uepg2r/2sXa/lnpzb0JdKiAFgzj1XIUy23LH7FXPnZ+YwszJglIy3Nphz79464N9aayEBSwjRBjw0iGGY0qCWnld/0mGl3HrZbKor7BH3qM4OVMhJZc0FWu6/R6Kk6H4Ux2/06NOH3O/1f7iG8xu0hnhK88Fjy/nuRbMo8QqMrd1gzsXqw0KITm8jyzqCd091ujHUTc/KelfMZum8MF1xd1Rl+FlwicH/PZIqaJHzPsKzvNmCU9H3eyNVdhzXtBe49P3VXPfxmYRDMtsze/fKXnbtYRfAlUII3/Y8BfzH+wU1rUDt3fxFDWFuv2I2x+xXTHvX9B3zJqWhT7YluP6TdVx4anW2cn2389dH3dDA4x52EUJo6TkmUgjhAt9lmqq6tmVmilSX2Xz3ogZOP6qczp7pN+bNkpBMKcqKLG69rIFTDy/L9mneDeZBLfR3hBCu1gbLsh+5/oeHdgk408+yGSsWDUu+ft5Mzju5imRKTZuAgyVNc8bZM4Lc+dk5HLasCNeX5XaDN3c5HkafBh7sgwHfVGutbSFEN3BnDpeeduEJXwKzLMHnzqzlsx+agdKQdvWUVgUsCZ1xxfIFUe68cjZL54ZxXNhdjTYsd75DCNHtYVflWmgA32zfAzzKFA6FjwTUvkpxznsq+cb5MymKWCRSasr1bPOTsDrjihMOLuHWyxqYXRvEVWb40u41KHe2gMeAezzMun0stG+lvb+7gc8yhcPgI1YuvHD5iYeUcttls2moDtIdV1MmR9jvX9eVUJx7YiXfvqCe6lI726N69xpypYHPeFglR9joW/UthFBaayGEeBq4zfu5ms5n7ofL918U4fYrZ7NsfoSOnslXQPzclHRa87kP1XL1h2cQCkjUbiVjV9ZZArcKIZ72sKoGcOj+yzPjX8aEE6elg9gf1ErB/Jkhfnj5bA7fx8vWmyRny28uLiXccN5Mzjupyrvu7K7K2bUj+CzwdV/VGHBtB27VWerRBpyHqTm0prul9otwa8ptbr20gVMOL6UnNfFFuJZleleXFVvcfNEsTj2ibJj8k92rH29uBc4XQrT2pxrDWmiPelhCiJXA1dNV8Ri4zZtQeSws+fYFszjnPVVm/JrrNT6fADB39bjMrwvxvUsaOGrf4rdaEWu+ysbVQoiVHjbViCx0DqhdL+DyI+C6HCutpzeoe9t0XfWhWr740TqkMBRgPIFlSVPEeuiyIm6/cjb7L4xmRxvvXkMuncObvySE+FFOEJBRAbqXTmtbCPEV4Fbv993pfpVkzqD7Dx1nkn5iEdPPotBatT/Ppb3H5ZTDS/nBZQ001ARNHvNuyzxSMH9HCPE1rbW9K4Mqh78ZQmP0aSGEuBj4ESb/1J3ullp4vNpVcMz+Jdx6aQO1lQHiSVUwS+0HebqTio+/u5IbL5yVnTGz2/nbJZi1xwpuFUJ81tebB+PNo7HQWeLtgfp84L+9DxLT3VH0qYCrNAcuNkW4i2eH6U6ovK2n9N4342qu+EANXzy7DoTYnS03MgdQeNj8hhDiYi9nX+8KzCMCdD9QW0KIq4Frc17vTH9QG2dx4awQd31uDkcuz5H1xNjeL5XWhAKC6z5ex3+dXGUaT+7WmHe1nBxMXi2EuFZrbQ2laIwZ0DlvqDxS/g3gTGBbDgWZ9rzaVVBVanPLJQ188OgKOuMupppkNEqGKWKtKrO58cIGPvCOcq+6ROwG8/AUw/WwtBX4kBDivz0wq5GCeVSA9kGdE038DXAc8C+PgjDdubUfVYyEJNefW8elp9eQSLloNYI2ZJ7z1xVXLGwIc8eVszlyeVG2T8ZuLA8LZOFh6EHgWCHErz2MuaMB86gBnQtsj368ApwMfBHoyeHWznQGtdaGyF18Wg3XfbwOpdllw0ghTBHr25bE+MlVc1g8O5xN/dy9hqQXPpB7gC8BpwghVnvYGpNhHPOMFU+ntrwEkeu11n8EvgGc5L2vyuehmVQFxAsjKaX54DEVREIWDz/flR0QNNhKpjXHH1TCV8+tpyQqTZf83c7fUE5fLvZ+D3zRM46+nzZmCpu3/fA80GySiNb6TEzI/JhBZBjBNNt9/bKnjU1pZlYHCPQLhPg/39ycoarUJhwUu9sLDKQVg937hzD5zPd4uJEjVTLGFdD9gO3TkRhwOHARcDym34e/MjmyzLS47Wo3dRiLFfajyv3v/T+AHwCPCiF6CgXkggM6B9i2EMLJ+f9S4BPAO4El/U7QzTkOMZ7HNd6g1m/Nuj89iCUmRyTwQfwqcD/wEyHEy0NhZUoCejAa4n0v5KkibwP2AQ4C6ncbszfl2oLpNfc8pu7vASFEKgcLBbXK4w7oQcDdJ6FEax0E5gANwCLva7H3/2KgCCifjg7lW4hStAHdmBk9jZg+42uA1zCdQDcKIdI593zUmvJY1v8Dm46pWk/K8U4AAAAASUVORK5CYII=">
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
/* versão clara (botão da lua no canto) */
:root[data-theme="light"]{
  --bg:#f3f6fb; --surf:#ffffff; --surf2:#eef2f8; --line:#d8e0eb; --line2:#c5d0df;
  --ink:#132033; --muted:#526179; --dim:#7a889e;
  --accent:#0a7fc0; --accent-ink:#ffffff; --accent-soft:#e2f1fb;
  --gold:#7a5300; --gold-soft:#f8ecd0;
  --good:#16803c; --good-bg:#e4f5ea; --warn:#8a4b00; --warn-bg:#fdf1dc; --bad:#c2302f; --bad-bg:#fde8e7;
  color-scheme:light;
}
:root[data-theme="light"] .busca{box-shadow:0 6px 20px rgba(20,40,80,.10)}
:root[data-theme="light"] .modo button[aria-pressed="true"],:root[data-theme="light"] .acoes button.salvar.on,:root[data-theme="light"] .salvar-todas:hover{color:#fff}
:root[data-theme="light"] .aba-op,:root[data-theme="light"] .salvar-todas,:root[data-theme="light"] .acoes a.pag{border-color:#b8913f}
:root[data-theme="light"] .tag.mod{color:#1d5fa8;border-color:#b5cbe6}
:root[data-theme="light"] .tag.srp{color:#6b3fb8;border-color:#d4c3ef}
:root[data-theme="light"] .tag.nos-itens{color:#0a6e9c;border-color:#a9d4ea;background:#e6f4fb}
:root[data-theme="light"] .card,:root[data-theme="light"] article{box-shadow:0 2px 10px rgba(20,40,80,.06)}
.tema{flex:none;width:40px;height:40px;border-radius:50%;border:1px solid var(--line2);background:transparent;color:var(--muted);display:inline-grid;place-items:center;cursor:pointer;padding:0}
.tema:hover{color:var(--ink);border-color:var(--accent)}
.tema svg{width:20px;height:20px}
.marca-clara{display:none!important}
:root[data-theme="light"] .marca:not(.marca-clara){display:none!important}
:root[data-theme="light"] .marca-clara{display:block!important}
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
.busca input{flex:1;min-width:0;border:0;background:transparent;color:var(--ink);font:500 16px var(--font);padding:11px 0}
.busca .q #limpa{flex:none;width:30px;height:30px;padding:0;border-radius:50%;background:transparent;color:var(--dim);font:400 24px/1 var(--font);cursor:pointer;display:flex;align-items:center;justify-content:center}
.busca .q #limpa:hover{background:var(--surf2);color:var(--ink)}
.busca .q #limpa[hidden]{display:none}
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
.ufs{display:flex;flex-wrap:wrap;gap:6px;padding:2px 0 4px}
.ufc{min-width:42px;padding:6px 8px;border-radius:8px;border:1px solid var(--line2);background:transparent;color:var(--ink);font:600 13px var(--font);cursor:pointer;position:relative}
.ufc:hover{border-color:var(--accent)}
.ufc[aria-pressed="true"],.ufc.uf-ok{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.ufc.br{padding:6px 12px}
.ufc.uf-carr[aria-pressed="true"]{opacity:.55}
.ufc.uf-erro{border-color:var(--bad);color:var(--bad)}
.ufc.uf-erro[aria-pressed="true"]{background:var(--bad);border-color:var(--bad);color:#2a0d0c}
.uf-info{font-size:11.5px;color:var(--muted);margin-top:6px;line-height:1.4}
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
.tag.mod{color:#9cc8ff;border-color:#2b4a74;background:transparent}
.tag.srp{color:#c9a7ff;border-color:#4b3a74;background:transparent}
.tag.prazo{color:var(--warn);border-color:#6e5317;background:transparent}
.tag.prazo.urg{color:var(--bad);border-color:#7a2f2f;background:transparent}
.tag.portal{color:var(--muted)}
.tag.portal.meu{color:var(--gold);border-color:#7a5a12;background:var(--gold-soft)}
.tag.cat{color:var(--muted);border-color:var(--line2);background:transparent}
.tag.assist{color:#ffb4a8;border-color:#7a2f2f;background:transparent;font-weight:700}
.selo-cg{display:inline-flex;align-items:center;gap:6px;font:800 12px var(--font);padding:3px 10px 3px 4px;border-radius:7px;background:#1351b4;color:#fff;border:1px solid #2a6bd1;white-space:nowrap;letter-spacing:.01em}
.selo-cg i{display:inline-grid;place-items:center;width:18px;height:18px;border-radius:5px;background:#ffcd07;color:#0c326f;font-style:normal}
.selo-cg i svg{display:block}
.selo-cg em{font-style:normal;color:#ffcd07}
.selo-p{display:inline-flex;align-items:center;font:800 12px var(--font);padding:3px 10px;border-radius:7px;color:#fff;white-space:nowrap;letter-spacing:.01em}
.selo-mg{background:#c8102e;border:1px solid #e23a52}
.selo-cld{background:#0e8ec9;border-color:#38b1e6}.selo-cld i{background:#fff;color:#0e8ec9}
.custo{display:inline-block;font:700 10.5px var(--font);padding:1px 7px;border-radius:999px;vertical-align:1px;white-space:nowrap}
.custo.gratis{color:#4ade80;background:rgba(74,222,128,.12);border:1px solid rgba(74,222,128,.35)}
.custo.pago{color:#fb923c;background:rgba(251,146,60,.12);border:1px solid rgba(251,146,60,.4)}
.selo-cbb{background:#fcfc30;border-color:#e6e600;color:#003da5}.selo-cbb i{background:#003da5;color:#fcfc30}
.selo-cpp{background:#e56a17;border-color:#f08a3c}.selo-cpp i{background:#fff;color:#e56a17}
.selo-csl{background:#1a8a43;border-color:#2fa85c}.selo-csl i{background:#fff;color:#1a8a43}
.selo-cmg{background:#c8102e;border-color:#e23a52}.selo-cmg i{background:#fff;color:#c8102e}.selo-cmg em{color:#fff}
.selo-go{background:#13873f;border:1px solid #2aa95a}
.selo-ld{background:#0e8ec9;border:1px solid #38b1e6}
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
.acoes a.pri{background:transparent;border-color:var(--accent);color:var(--accent)}
.acoes a.pag{color:var(--gold);border-color:#7a5a12}
.acoes button.salvar{color:var(--ink)}
.acoes button.salvar.on{background:var(--gold);border-color:var(--gold);color:#241a00}
.acoes button.remover{color:var(--bad);border-color:#7a2f2f}
.remover-todas{margin-left:auto;font:700 13px var(--font);padding:6px 14px;border-radius:999px;border:1px solid #7a2f2f;background:transparent;color:var(--bad);cursor:pointer;white-space:nowrap}
.remover-todas:hover{background:rgba(229,72,77,.12)}
.salvar-todas{margin-left:auto;font:700 13px var(--font);padding:6px 14px;border-radius:999px;border:1px solid #7a5a12;background:transparent;color:var(--gold);cursor:pointer;white-space:nowrap}
.salvar-todas:hover{background:var(--gold);color:#241a00}
.salvar-todas.feito{cursor:default;opacity:.8}
.salvar-todas.feito:hover{background:transparent;color:var(--gold)}
.ja-salva{display:inline-flex;align-items:center;font:700 13px var(--font);padding:6px 12px;border-radius:8px;color:var(--gold);background:var(--gold-soft);border:1px solid #7a5a12;cursor:default}
.conta{margin-left:auto;align-self:center;display:inline-flex;align-items:center;gap:10px;font-size:14px;color:var(--muted);white-space:nowrap}
.conta b{color:var(--ink);font-weight:700}
.conta a{color:var(--muted);text-decoration:none;border:1px solid var(--line2);border-radius:999px;padding:7px 14px;font-weight:600}
.conta a:hover{color:var(--bad);border-color:#7a2f2f}
.conta+.aba-op{margin-left:12px}
@media (max-width:560px){.conta{margin-left:0;width:100%;justify-content:flex-end}.conta+.aba-op{margin-left:auto}}
.aba-op{margin-left:auto;align-self:center;display:inline-flex;align-items:center;gap:8px;font:700 14px var(--font);padding:9px 16px;border-radius:999px;border:1px solid #7a5a12;background:transparent;color:var(--gold);cursor:pointer}
.aba-op b{display:inline-grid;place-items:center;min-width:22px;height:22px;padding:0 6px;border-radius:999px;border:1px solid #7a5a12;color:var(--gold);font-size:12px}
.aba-op[aria-pressed="true"]{background:transparent;border-color:var(--gold);color:var(--gold)}
.aba-op[aria-pressed="true"] b{background:transparent;border-color:var(--gold);color:var(--gold)}
.tag.nos-itens{color:#7dd3fc;border-color:#1e5a7a;background:#0c2a3d}
.tag.encerrada{color:var(--bad);border-color:#7a2f2f;background:var(--bad-bg)}
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
.marcar-item{display:block;margin:6px auto 0;width:17px;height:17px;accent-color:var(--gold);cursor:pointer}
tr.escolhido td{background:var(--gold-soft)}
.copiar{font:600 11px var(--font);padding:2px 8px;border-radius:6px;background:transparent;border:1px solid var(--line2);color:var(--accent);margin-left:6px;cursor:pointer;white-space:nowrap}
.copiar:hover{border-color:var(--accent)}
.copiar.ok{color:#4ade80;border-color:currentColor}
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
  .busca button{padding:12px 20px;flex:1 1 auto}
}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <img class="marca" src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAoMAAADICAYAAACTbusIAAAQAElEQVR4AexdB2AU1dY+d3Y3vRN670lodgV7VyC2Z6+gPp+9POW3AwIidhRsqIDP8nzwxJKA8MSuYEGlJgHp0lt6z879z9kUdpMtszOzm93NSe7ZO3Pvueee+83s7re3jQL8xwgwAowAI8AIMAKMACPQZhFgMthmLz03nBFgBNoeAtxiRoARYARaIsBksCUmnMIIMAKMACPACDACjECbQYDJYIReam4WI8AIMAKMACPACDACWhBgMqgFJdZhBBgBRoARYARCFwH2jBEwhACTQUPwcWFGgBFgBBgBRoARYATCGwEmg+F9/dj7toYAt5cRYAQYAUaAETAZASaDJgPK5hgBRoARYAQYAUaAETADgWDZYDIYLKS5HkaAEWAEGAFGgBFgBEIQASaDIXhR2CVGgBFoawhwexkBRoARaD0EmAy2HvZcMyPACDACjAAjwAgwAq2OAJPBIF8Cro4RYAQYAUaAEWAEGIFQQoDJYChdDfaFEWAEGAFGIJIQ4LYwAmGBAJPBsLhM7CQjwAgwAowAI8AIMAKBQYDJYGBwZattDQFuLyPACDACjAAjEKYIMBkM0wvHbjMCjAAjwAgwAoxA6yAQabUyGQyhKxqd3L1vYnof6Uni03qdG0Luhowrcal9RiWk956V2K73gUQv+FFe//595Yhj+jXHODdkGsOOMAKMACPACDACQUaAyWCQAefqTEAgLS0pIa3XFQnt+nyUkN673GKBXAHi7yBEO3fW+3YWcPcFNlj0RCysnBEPX0yJg1HHWppUpYQjgP8YAbcIcCIjwAgwApGPAJPByL/GEdHCuPSenRPa9b4T5YsEkXJAKMqHQsAlSALj3DXwiN4CHrk8Cr6dFgs/PhuPx9FwVN/DBHD3IemuGKcxAowAI8AIMAJtDgEmgw2XnKPQQyAhtftgJH+PJaT3WWEByy4hxAyUs4QAmztvT8qywJTrouC3l2Jh8eR47A2MgoHdLC1Uv1lTB79vUp3S5TdOJ3zICDACjAAjwAi0KQSUNtVabmyoI6AkpvY+OTG99/OJ6X02CottjRBisgA42p3jUVaAc460wPRboiDvtTj47yOxcPO5UdC1XQMBxPHf+nISo8Pk77mPavD8cFDt6vOHz/iIEWAE2gAC3ERGgBFwQoDJoBMYfNgaCPSLRvJ3QWK7PrMT0vvsAYv4DkD8EwD6orQIiTEAlwy3wpt3RUP+6/Hwr/tj4cpToiAt0elWdpBAIoCNxZFOQn3+svw6WLHxMDGUUv6vomjbH42aHDMCjAAjwAgwAm0NgfpvyLbWam5v6yKQ3CM1Ma33TYnten+akG4vAhCfgoCxSNnag5u/9CQB151uhX+Pi4E8JICv3hED2cfbID4GS0DjnxP5E5SOQrEj+3DeCx/XOlIaX+xSTmg85pgRYAQYAUaAEWiLCDAZbItXvRXaHNuuW9f4tF73JKT3/jrBatkPingLhLhAgMC+vpYOdU8HuG2kDT4bHwNrXomDZ2+KgdOHWcFmFQ3KRPBQJIojpTHdcdLspT5vxZ92+CHP3pSHvYJLKw9t/akpgQ8YAUaAEWAEwhoBdl4fAkwG9eHGpTQgEJ/S64iE9D4TkQCutIqoHYqiTBcgThNCNEzqczUyqKeAcZfY4KupsfDr9ASYcHU0HDfACqgP4CB9SPwoJgGBhVEECh55DA7d+txnF1TXHzS8Yq/g4w2HHDECjAAjwAgwAm0WAaXNtryVG55107ozh45ZN2Ho2HWfDBmb9yPGf/Q+683F3ty6/4aj//vhq/fse3fGP3fOmv5Y3tRpz85/cPwb//BWJth5Ce16noEE8EUcAt6qWJU/kKpNECCGufNDCAnHD1Rg4tVR8PMLcfDlk/Fw/yXRkNWjkSsi+XMUxFjQAb4Q+SOhUy3SoLtmqx2+XeM8VxC+5F5BLQCGqg77xQgwAowAI2AWAkwGzULSg53eV+f1HDIm/6mhN+YtG3Zj3i4kfdUYS5sUS4UiJgohLlQEjMD4CGGx9vNgxpF8XKf8hOw+X7e/pP/SLtdkfJZ5zxHvXjp+xCuvly8eKtfNO1cumX1D9dwZ43ZPefrZhf94+O3hjkJBeEls1+vChHZ93k9s16dYCMuXAuBeEKInuPmzWQHOGGaB526MgtUzE+DTx+Pg1pFR0LND462IxI/KOXr00BIdA8UkjhM/XhpsYYmn/+u6ghjschImc2AEGAFGgBFgBNo8Ao3fwCELRLg51uvK/F5Db8ifiqTv92Fj8yoSY2CrosiHBACRs85I+qIC0aZeSbvhpC5/RF3Wf0mn+4a9O/L5U15atm7eeerHb9x66OXnJy6eNm2a6Y+yS2jXMzMxvfd6EMonQsDVyNmS3LUtPgbgguMt8Nrt0bDu1Tj4YFwsXHtGFLRPRlSwgAQibSR4gkboFdCgIzb0Um+fegWXrnSaKwjy+7KiLd8ZMs2FGQFGgBFgBBiBCEFAiZB2tGozul32V+yQG/NnYe9fUXKc3CIs8mEhxJEgILaejgTfPYEV90raJc7puSz1pkELzr3riA8Wr/rwfPWV58evuOex6QMNe5SWlgRC+RJADAA3f2mJAFedaoF374+BvFfjYdZdsXDxCBskxSkN2kT+6gVdxbT6VzwwKZDtelPPLWjWKyjVifU5/MoIMAIhhgC7wwgwAq2AQOM3cytUHf5VZoxZmz1s7LqVaQml5QrIvyOdSQ7lVvVL2SnGDPrk6PEnvFfw6Rv/KJ745EtT9PqbIJKnCBCdnct3bSfglvNs8PGjMbBmZjy8+PdYOPtIK0RHiQa1eoImJcWU5iwNKiZH63eqsOT3w72CIOXysoPbvjK5GjbHCDACjAAjwAiELQJMBv28dFmXrY0aMjbvbewFLI1WlM9AiGFCgPDTTKuqx1hr4Kyey5PGHf32o6s/HKnOfH7Cbzc99JbX+YotHRanN6b17Sxg8aRY+O2leJh0bTQMz7SCxdIICRE/EtKuTxNCANBpwITqq6/r6fnVLrWoEia4JPAJI8AIMAKMACPQxhFgMqjxBki/YH/i0LFrP7QmKBWKgBuRaiRoLBrSan1Tdoixgz4+6rFj3/sTSWH+LeM+dvvkjxaNEDK9Me2EDAsc0adxBXBjamOMSLUSV87/yw6LVrj2CpYf2vJFo2ccMwKMACPACAQHAa4ltBFgMujj+vS+anPHITfm5XZO318khHIFdmp5Yj0+LIV2dof4Q4CkMGPCSc9unP3yQ9vum/j6MV49lrC2Mf/9r+tg/g/OT/agnrnG3CDHTUPQANM/dZ0rqEpeQRzkq8HVMQKMACPACIQBAkwGPVykrhfvaDdkzLqFCTGVuxGkUSgYPChHUHJKdBlcMWBRj4ePnv3rG9Mf2ZZ9Z06Gu+apqvqCc/rdb1RDzi91DUnUG9hwGOyooeote1T47GeXXsFfyw9t9bqPY7Bdjcz6uFWMACPACDAC4YZAmyA4/l0UKYaMyZuZllqyT1HESEWIBnrhn5Vw1463VcG1Gbk9ZpzxfP4zz0z95fy7FkU7t6micNvn2An3UmMaHsM/ZlQ5EcLGnGDH9Zfr+Y+rgXxqrN2u8griRiw4ZgQYAUaAEWAEnBHQTQadjUTK8dCxBZcOHZtfrChwBwKDIVJapr8d7eMOwR1DPzz22ZNmVjz91NMznC2VHdx8rwT5VmOaiqPDoUAIdxxQ4aNlh3sF0a3fkLwuavSTY0aAEWAEGAFGgBE4jAATHsQi4/r8AUPH5q0FUOdjP2AiJnFohkDv5B3KnUe+f+cP715R+eyzk65uzC47sOUWkOrsxvNQIIS0r6Bzr6AK8olG/zhmBBgBXQhwIUaAEYhgBNo8GRx6w/rro61yLZLAQSgRfKnNadqR7fNjrsv4/L3nn3ny/QaLsvTg1pullI3n0DqEEPv/0KEd2Cs4/4fGuYsA2HP5R8WBLTmYxYERYAQYAUaAEWAE3CDQhsmgFEPH5s8Hxf4O4mJD4UAIaJBEW7m4deh/rv7ojdt3v/DCC7FYRJYd3HK9lOoHeOwIjYRwye+HiZkjI0AvjT2B0z+rAbt6uBLBK4gPg8FHjAAjwAgwAoyAGwTaJBkces2qbkNvLNguhLyUewPd3BUak87r+UOn0zp/XTpt2rRRAB1jVSl6yEZWhjZU7Ky7cXoVbN/vxM4w3fwgQQgBewtV+Pc3h8knupKHvZafml8fW2QEGAFGIDIQ4FYwAoRAmyODWWPWnQxRtk0CZDcCgMUYAgNTt1nO7/JRTsce6ZstijiJrDUSwtgogH/dHwM92gf2NkPSR9XCi5+49griGPF4zJAoHBgBRoARYAQYAUbAAwKB/Zb2UGlrJQ8bu+5aqyK+wU4kpCmt5UVk1Zu33Q4XTTwkKioqO1DLhBAUQVoCQM6EWDhzmNVxHrgX2dQr+H6zXsGyQ5sXBK7ecLPM/jICjAAjwAgwAu4RaDNkcMjYvPuQNvwLqUrItbmu6iBUHlgHFftXub9KDakrNtbBsvw6+Cvgw64NFfqIfsyrg1ETK+FAiXTR7NdZgSVT4mBwz8A/rKWxV3BGTg3UHt5NBkA6VhC7OubiJZ8wAowAI8AIMAIRioCfzQo5YuSn/5rUh43NexpJ4PPYaYWRpiIBUbLXlkPFvt+hcMM82LviOdjx7b2wZeGlsP1/N8DuZQ/CwdUzvdb73IJauOTJKjj2vgrodWMpnP5wOfz95Up4bkEN0EKNQ2Wq1/JmZs77vhYue6oKKl2f+AZH91Pg80lx0D09GLeWdPQKHiyV8N7Xh+cKYjs3lh3aMh9jDowAI8AIMAKMACPgA4FgfGP7cCGw2UPGrpstAca1FhEs+etL2Pfbc/DXl7fAts+vgD0/jYfCgn9B+a5voKZ4I0h7lS4AqmoE5P8lIecXu4MM3vBCFWTdWgHH3FvmIIhzl9ZCaWVgyOHUedVAj5+jBSLOzmcfZ4EFj8ZCUpxwTg7YcWOv4MycaqhyejSytKuPY6V42fGVAyMQ2Qhw6xgBRoARMIxARJPBoWPXfaIIMVYIEIaR8sNAVdGfsH/Vq7B10RVw4I8XoWznN1BbvssPC/pVdxwAB0F8aG41DL2jHO56vQqW49CyfouuJW97pQpe/syJeTVk3zbSBrPuioFoW5CgRiYohADqFZzzRbNewcKt8xrc4ogRYAQYAUaAEWAEfCAQsWRw6E0FbwohLvTRflOzS7YvxaHfu2HXd/dB6bZFoNaVm2dfh6VK7D2kDZgvxqHlEQ+Uw1tLaqCyWl+HWUmFhAsmVcDHy12IFwjkftPGRMOEq6PxGE90+KmnSGMrXl1Y49IriH2hE9AeRvjKgRFgBBgBRoARYAR8IhCRZHDImLwHQVVv8tl6ExTsdRVQuHEBbPvfGDiwcjoO/W42war5JjbvkfDYuzVw9D3l8MxH1XCoVDtfoqd6nDe+An7Z4Fom2gbw3gMxMOYsPDDfZS8WpYN4FpdLmPOFcy+l3FB+YPMHXgpyFiPACDACYYEASGt2lwAAEABJREFUO8kIBBOBiCODQ8eupY2kn8Ieq4B2U9FeesVbFsFfX9wEhXmzwV6F47PBvHI66zpUBvDCx7UOUvjSpzVQWye9WqKtY855rAKITDorpsQDfPp4MLaOca61/hhHiB0Hry2qgYpqx6HjRZXwpOOAXxgBRoARYAQYAUZAMwIRRQazblx7khCWDwNNBCsPrXcMBx9c8yqotaWawQ4lRRpCfmp+DZz0fxXwxR+uQ7+Nfn69un7rGCKQjWkU9+ogYMnkODiij3lbx+wvlrDiTzuZ9yEShBBAvYKzFh/uFUSCuL384Jb3fBQOwWx2iRFgBBgBRoARaF0EIoYMDr1uYwcrKEsBpHkMpdm1qasqgn2/vwC7f7gfaku2NMvVfiossRDX+URIyxwD6UfcAx2PnwBdTnkROp/4tFcjr90RDUunxMKH/xcDL98aDY9fFQVXnWqBlDivxbxmbtsn4brnq+CaZyth6157k+57X9fC1c9Wtdg6ZlhvxbF1TM8O5t06W/eqcD4OQ/9taiX8sM49MW1yDOo7fN9cUuvSK4hkcCLquI5jYwIHRoARYAQYAUYgZBAIUUfM+0Zv7Qbaan5BmhAdKDcq9q+Ev766Bcp2fKWrCmtcR0jqnQ2dTpgMvc7/ADod+zCk9L8UknqcDfEdj4WYlP5gjUnzajs1XoHBvSxw2lArXH6SDe4YFQUv/j0W8l6Ph8/Gx8Cdo20wsCui4NWK+8wvV9nhlAcrYcGyWnjig2p44O1qQILlonzuURbH0HBqgr46XIw1nFBvIM1H3HFQQjV29F3zXJVnQtjgUHmVhFmLD29wiMnbyw9tfrfBJEeMACPACDACjAAj4AcCEUEGh43Jm4n0pKcf7dasSnMDDxW8D7uXPw6yrkJ7OezBiknNgtTMG6Dbaa9Aj7PehvQh/4C4DkeCUGya7WhRVBQBxw2wwmNXRsO3T8fDby/FwuRro+CUwQrYrKD5rwY75WjrmNcWIStrVuqGM60w974YiIlCpJvl6T1d/FsdXDylAgrLZJOJekJYCT8XHO6lxN5ezEedhqrfRCJY4nQphJSTUAG9x1cOjEBoIMBeMAKMACMQNggoYeOpB0czx+YdJQXc7iHbULJaWw57lj8GRRv+jdQOyYhGa7GdToDuZ7wBXU5+BlL7XwZRSQHhqR696drOAn8/LwrmPRQHv02PcwwlC6HNfyEaGFeTdQkTr46Cp8fGgBDN85qU/D544/MaGPNiJdTahcOuhMP+VdcKuPKZCsej9+oNC4zqpapGwhufO5FVKbeVHtryNipwYAQYAUaAEWAEGAEdCIQ3GbxMWrCj6ivkKMQUdDTfc5Hain2w49t7oPLAKs9KlOMkUUm9kQA+D52PewyiEro45bTeYYcUBWgo+dtp8TA8w7/LHWUF7A2MhVtHRpnWAOppfXBOFUx4n4Z5nS4bckFVyu2NFdECF5rH2HzD7Lf/VwOFTts3SiGnNpbhmBFgBBgBRoARYAT8R8A/duC//YCWGJqYtwQrSEYxNVQX/Qk7v7sX6ir2aLJriWkP7Y+8H7qe+jLEpA7UVCbYSgO6KvDxY3HwwbgYTfMKU+LB8Wi5845GRmiSs9W1EnsDq+CdL11HdJEgYueufLD84JaeWNXnKI7gIITPVcFPDUPG1Cv4ysLDvYJSwo6yA1tnOZT5hRFgBBiBICDAVTACkYhA2JLBwTfmnyNAnGn2RanYuwJ2/fB/oNaU+DRNq4JTM653DAkndj/dMdzps1ArK5wxzApfTY2DZ2+MgiQPq5C7tROweFIcHNPfvIXZNC/wkicrYcnvznMBATFT6nCE+PLSg1ufAfwrPbD5IiR5Tat0aB/Bq5+thKnzquGWGVVwyHUnH+4VRMw4MAKMACPACDACRhAIWzKoSGn6kybK9/wKe36eCFI93PvkCVxrfDfodtpMSB1wOShW84ZRPdVnZrrFIpCECSitbGmVto5ZPDkOenU079b464AKtGL4t42uO78kxgLMuLe3MvDSzZ86eVJTdlAdhb2FXzSmESGk5yH/7w9nIim3lB3c/FqjjvkxW2QEGAFGgBFgBNoGAuZ94wcRr6Fj8icIAe3MrLKqcAPsW6Gtoymm3RDodsrzYIvvaKYLQbGFJAsmvl/lduuYM4ZaHFvHpCcJ03xZudkO5z1eAbSfobPRzqkCPsfex0uP3qtc2e+pH53zALZWlR2UF6CvS13TD5/VSXnV4TM+YgQYAUaAEWAEDCDQxouGHRnsd5eMFop81MzrVlOyDfbQ1jEaegQTup0BnUdMAcUWb6YLQbFVUycdG0y//rnrnD2qnLaOee8Bc7eO+XJVHVw4uRIOug7twqAeCiyZHAv9OtffftdkLDzm/kmvHEt+HBYihFvOxiHj20HKHBxKLpUgK/H4XyDVzMqDW38+rMtHjAAjwAgwAowAI6AXgfpvY72lW6FcbFnePKzWtI366ir3w65lj4Ba57REFStoHpCUOPYM7HDUP3GI1by5dM3rCdR5Ubl0ELOlK52HWutre+wKm2PrGEUxr0dw7tJauPa5KsdG0vW11L+ePMgCORNioUOKUp+Ar8lRpWJ4+3W0GAjPXAMNBZce3HJB6cHNSWUHtsSVHtxyQ+nBrQWuWnzGCGhGgBUZAUaAEWAEmiFw+Bu5WUYonmZevbmnAJFtlm+qvQZ2Lx8Pak2xV5O0SXSn4x5x7BnoVTFEM7ftU+Gcxyrgj02uc/asyGln3RUDd2ab9+AWHNqFSR9Uw0Nzq4EItDMkV55ihQ8fjIG4aOGc7Dg+p9eylIefeHWs44RfGAFGgBFgBBgBRiBoCIQVGbRFV30oBAhN6GhQOrhmFtSW/eVTs9MJUyC+8wifeqGosGqL3UEEt++XLu7R4o15D8XCBcdbXdKNnNDWMbTi91U3TzB56FIbTL8lBiweeh8tQhXHd1o100j9XJYRYAQYAUaAEWAE/EcgbMhg5k15tAfd8f430X2J8t0/Qen2xe4znVI7HPUAxKYPckoJn8Mv/qiDCyZVQrHTo9vI+8bFGyMysWuQEkyQkgoJtHVMzi+uw9DU+/jKbdFw70W+ex9P7/5z3D8efe8KE9xhE4wAI9DGEODmMgKMgH4EwoYMWlV406xewdqKvbDv9+d9opbc9yJI6HaaT71QVHjj8xq4/oXKFnP2mi/eMMN32jrm/PEV0HzrmPgYAOp9/NuJNk3VWBUVTu3yK28Xowmt0FKSUt6K8h+UX1D2oFSibED5H8pMlH6h5TF7E4kIlHw24KSi3My3inIzvkLZglJVmJu5FeOvizCd8iOx3d7aVJQ78NGi3IyPinIyf0cpLMzNKC7MyVjpSMM8b2U5r+0gEBZksM8tm5KRCJq2wfS+354FaXezyZ7TdY9tfwSkZd3olBIeh/ilC/e/Vf+4NyldR9TdLd4w2qq12+q3jtm0x3UYmnofF02MBX97H8/v9UPKI0/M9HKtjXrM5c1EAO+3YSjL0eZrKJej0KrwjhjjTwHoj/HZKHeg/IF6D6OExWcO+sshjBCQX/RJLsoZ+KYqlG/R7ZsAxOkovVBohjKOKgn6VX8T5SMJekN+OjARIvyveOHA8wqRCAMoUwDEJSDgSBCQIkAkCSGGAaVhHukU5Qzgz1xo239h8cGcUFc9QwCY4mvpjm+gutD7YlRbQjfoeOxjIIQpVWq+w+zVRV51D5S6LgBprlxVU791zPvftNw6xtvijeZ2tJ7T1jHZT1S22DqGHn1HW8cM7Ob/MHSUpVYMSt80V6sPrNd6CCCxuxBrX4lyAoqvkIAKU1GaNhPHYw6MgGEEyj7t3bGoOmotCOVmED4+tB354pYiRVlbsjTD1L1qDTfERAPFuRnjpVQ+FwBIhL0bdugIy9KinIyHvWu2Ui5XGxQEgst29DZJFZfqLepcTq2rgEPr3nJOanFM+wd2Hj4FFCt1bLTINjXBXl0Mpdu+gD0/T4bNuZfArh/GebV/52vV0O2GMrh8WgXM/qIGdh86TA4PlkrH/EB3W8c8+Dfvize8Vuoh89/f1jq2jqmscVWg3kfqEezgtHWMq4bvs1O7/dr1sgfnJfvWZI3WQgCJYGes2/ubCRXchDOw7EQ36ZzECOhCoNYSM1+A6OZPYSGgh1oF7/tTJlx0aShcSpjgt79CTCpelKnlh53fprlA6CMQ8mRw8Ni8K4QiY82AsnD9B2D31vsmLNDp+IlgjU03ozqPNoo358DO78fBtiXXwP5VL0HF3p8B1GasykPpOjvAd2tVeOSdGjjy7go489Fyx1YuZz1aAau3HiaHVLxx8cZ9F/tevEH6WmXqvGq4783qFlvHdE4Tjq1jEmKFVlNu9VKjS8SgxF1vu83kxFBBgB4HqfeN8ggSwhObNYRPGQG/EShamPEQftqc7HdBRwFxLg6R3us4jJAXGi63K8o8ED56SN2316qq8GFbGEJ33/y2nRryZFAB1Xt3mcbrV1d1CIo3f+ZVO7nvJRCTlulVR2+mlCqUbF8K274YCwfXvoFD1fl6TbmUW7dNAm3lsvuQ65w9fxdvuBj1cFJbJ+H2V6vg5c9qW2hI/ClKPjQnpC0UNSYMSy8YqVGV1YKMAF7rwVglzcHCSFewYanbUDgwAoYQwI+dO4wYEBIMlTdSdyDKFlfZrhIgqNdel3kB0LPUIi7RVZgLhTUCwSeDfsElBf7AOcKvIh6UizZ+BICEzEM2CGsspPa/zFO2ofTyPT/Bjm/uggMrp4O9cr8hW1oK61284c12WaXE4elKWLCs5XxEKocXiiKY/om2Hk6HspeXk7r9HnP3uDm0AMGLFme1EgJm/GLq00q+c7URgoD8uleM8HN4uGXTZW85D/yf3NzSUEikIB6GPzNxfGlASDSGnQgqAiFNBodcX0C/2gy/Ue21ZVC69XOvwKb2vwIUW5xXHX8zVXs17F3xNOz9ZQrUlm7zt7hXfeydAZLmSkYWbzS31Xi+65AKIydWwvIC/JhoTPQQL/ndDht3+9bzULwpOc5aJXp3+OvNpgQ+cEEAr31/lNNRLkO5F+UplKkoD6PcgXItylkoSS4FzTkxg8j5nNhujqtspbURCFT9ZeWxxrcrEsJSktS3d6B8DLZd7D4xoy1mvL+D3XSuzyACIU0GhWL/u8H2OYrT8LD0MidPiUqGpD4XOHTNeqmt2Ae7vn8Aynd9b5ZJFzvUE0finDg8Q4FFE2Ohg4HFG8726Hj9Djuc+3glbNipneC9/Jk5vYNHtc8fTj6wYKe2lP2Q2D2O8gMKzQnYgLh8hTIP5UWUh1BoNeBUjGeivIvyBUox6m9E+RTlQZQkTDMadhk1gOUD30WOlXAwF4GinIG3FuUOnKZHaNWvmd4k2A5tMWxPSjWpttKM+9mwKyYZ2G3UjpRir1EbXD78EFBC2WUhlEFG/cP3OpRsyfVqJi3jOlAsUV51/Mms3L8Kdn57D9SUGP+s0lrvJSOsjg2ejS7ecK7vu7V1jh7B/Yp+/9MAABAASURBVMXEPQ7n0EKRw2eNR4djGkouKnctczhX+9HQDhtsN9/zQUftJSJPU0r5BMpabNmfKJNQ9Cy86Ivl6NfONIx3oL03UIwM9f6EdoyGVUYNcPnWQEBcD6A8qEus0R3M9Ficu7cch0eMfsj+KbJ3NXtGk5leBteWBPU3ozUqQl3hy0ZJzoCMotxMqUtyMgp82ef84CMQsmRwyJi8kSCMz+Wo3L8S1JoS8PRnje0AiT1pX1xPGv6lVx3Kgz0/PwFqbal/BZ208YsaP+McvUEYyyZxUnE5vO8iG7x6ewzYrMIl3cjJRz/WwpVPV0F5lasV6n38dlocHDfA861DK55zfm65yMTVku+zKKVWdG2/n3q6fCtHmAbeAzej7MBmjUcx/KMIbTQG2mz3FjxZg/ZfRvF7pT72SBMxPYA2jIQ/jBTmsoyAAwEhVjpivS8CIuo+tILyi14omspZ67T92GsqwAeRgIDnb/TWbp0CN5jhQtlfX3s1Q08ZEcLwtERHHXWV+2HPL1PA25C0Q7HZC9I9wC9laOxLwy9bEAJQhIsA/jn0ZL2mIgBe/Hs0PHipuVvHPLegBu54rRrU+mqw1vqQfZzF0fuYFCdg0rXe6/zvj+4XmtRb0v7aI3HXedq1w18Tr+9wFPplTvMluwawRXTT34X2V2F9Z2Dsb5jubwEn/e14/A4KB0bAIALySTSg78NGQg3+fp6K5SMmJI7Ox1EEuURvg/C7aGHyeZs26i3P5cIXgZAlg0LCcUZhVe01UL77B49mFFsixHfRM+rW0iQtFtnz8yTw1gvpXAq/gJH8SQcJpHQhBCgo4OVPCOEgh6QSY5OOPf2uOtVGp6aIHdkfbR1DZLC5wTtH2+DNu2Obeh+P6GOBI3qL5mpN5z+vV4EWnjQl6DzISNvcZoaJ8Z64HGGiSaZHYxysQKsP6fnBYz1U6DZZCEFfwt5XZbktCdTXfBmWL3SfzamMgHYEUkYX/CaEer/2Eoc1pVBvSxhVsOZwSmQcJSt1+Dkit/rdGgkbU2yFV/hdjgtEBAKhSwYBuhtFuHz3MpBqrUcz8Z2ObyJXHpU0Zuz//QXNcwTxS7/eqgRH/QIE+PPXIUWB3IlxcMpgqz/FvOrS1jE0LEzz/ZwVBbpGvY+PXdmyJ/Cco7zXP/97fT/YnevPardZeXjqOxH72KjGtuI9cT4e/wuFeuwwCmqgOt9EH271s9ZrUN+fzcFpePlSIYTxoSysmAMjQAgkj1r/shTwTzw+hKIlHMKPtTtSR6+frUU53HTEyI0lwlp7NvY2aP6xhu/9HPyyPMcxDzPcGsz+moLAYTJoijlzjGSO+fME5Ef0BWXIYMVe7985cR0Ndz46/CvfvRx7IH90HGt5wS9DaBQt+s46fTsJWDI5Fgb3NAxPk9l9Rapjocj36+xNaXQQGwXw3gMx4Kn38ewjvJPBL/4wTgZp3mA7644p5E+kCn4Q0xMU5mP7WjJuTAxSoBvqFfTlLK314T1ciHIz6tOcxgUYe5qcS70vd6LuAJSFqMeBETAVgdRR+S8m29VeIOU0/I3tdh8vKWE75ZNe8uj8V011IMSM0VBvSnb+SIsiTwOQ36BUt3RRYpr82iLg1NTsggtSLlhvdDFOyyo4JWwQCEkyaFXqbjQDwaoDaz2bEVaI7Wh8NE5KOxwqeA+M/iXGANCcvJdvjYYvkOytmhkHf82NhzWvxMOXT8bCrDuj4eHLbLAY87qkmXfZaE9Ad1vHtEsEyJkQC2cO80z4hvS2QMcU/I3tofErt6hQWSM95GpP7pm0e7R27fDSRPJFF5N6BONDwHPy5W30KckfX5Dg5aH8DSUZyw1BIYI4GeMLUZIxfSjKK3jMIYQQiDRXxIXrS1OyCx5OHZ3fS4mR6YpQL8JPp/FIAi+h89Ts/J6UT3qR1nZP7UkcWfAtDqWfjhIjRN0JQsq78P19v1BgOKWhnJE4Kv87T+U5ve0gQB/+IddaoapZRp2iff7s1Z5HDWLTh4FiMd4RU/rXV4Y2lE6IAbj3Qhv8PiPOMSfv8pNsUE+yFMf8vPbJAgZhL+AFJ9jgngujITFW4I88JFj4CQcG/5bl2+H88RWwuxDtOdmi3sfFk+M09T6ecyR1KDkVdjqkVcUrN7n2Njplaz6Mt1WkalYOP0Waq9crhNzugb68hKIrIOlbi/I2yniUz1A89Rbqss+FGAEtCCSdVXAwadT6T7EHcDKSwI/pXEu5SNZJHvXnz8nZBTOxF/CF5JH5vGI4ki+2jraFJBlEtkOT2nU053AR2uLl8FnLo7hOx7ZM9DOFegWLNnzoZ6l6dey+h7FnWWH58/Hw0GXRSPK0XgoBOMaMAg1/rkSuIdEpcn/42c91jsfLlVa65h/dT4HPJ8VB93Rt/viaN/jLBtW1Ah1nXeP3xukoFi5FHgxBR2/A3gPD78EQbBe7xAgwAowAI+AGAW3f+G4KBjRJiBSj9muKNnk1Ed95hNd8LZkl25ZAXYW+zdovOsEKT1wTBdTzp6WuljoCk/QRQXpCyC0zqoB67tBIUzj3KAsseDQWaOuYpkQfB6cOsYDNc+cgrNlmvGewY/xBAadJqw9Xwi4bCdeV6HQoki6BftFQL0bhFRBTekzfuRgPDi/PW3qLbWiHcg7K8S1zOSVcEChd0rdDSU7G8JLcASeXfpY1qHxxZueA+s7GWyAgF/VLIvzLPh84tEUmJzgQCD0yeJlEaiEM75dSW77L0UB3L7bEXmCNSXOX5VdaxW7fPe34Qd7C5qhjLfDK7bEQZTMKP31nk7Sowm2Cqkq4d1YVTJ3X8nFxt420wtz7YiDapt0eVRJlFXD6ULxkdOJGNu023jOYaKuAO0e8fqYb8+GeNMqEBtCTPB5FO1ej3Idi1gpJr1tM4H19CsrPOuV59NNwwLqJLE3A+GOUlSjUz02P6VuMxmlTbUySO/HlB5RHULpgutuAeaSjpz1L3RrUkYg+3IbyAQr5sR9N0MbeSzD+CdMoHMSX31FeQjkS090GzHsdhWzokbD7sizKyVimTzJNX8xUnJN5fVHOwDfRnyUo+YW5mWX22qi9qhDLVLB8Z1fk2to62FXkeHpHxpai3IyPUB4p+izzKLcXU0ci1n8r1q0Pk9yBlzWvsmThwAub28O2fNBcT+u5BOjR3J7LeW7GI1ptedIrzBk4tjAn4x2H3dyMfcWqrVhF/Ovsyqoiwj4n40BRbsavRTmZz9DTVDzayc14uUjn/YX3Qij+0PfUVDDKRjwa1psxNObPY4QAobd8Y7masp2Nhy3i6OS+LdL8TVBrK6DywGqfxYRwbcoxOAw789YYn+X8VvAxh7CiWsK1z1XBh9+1XOE7bUw0TLg6BoRw9VWrDzTH0ZPupt3SU5Zf6V0SKkb6VSA8lI3MFaQWLhBCHIEyFeXfKNNRbsIMWhnldkUl5mkNPZFQeCOr9GuKluPrkQFanXCnh371RaHVoNTGiahzEcowlBiU5oEI4ImY+CQKEcOlWPZ0PG4eSEdPWwjr5rY0n6MvySiPo9AHFrXpKixMfqRj3DwQ5kdi4t0oRAqJ8NLG4XjqEmjONdnQIwkulsLhRIjh+OGlQ6TxuUKIT/GizBOQgCEBzCyUAt4BodwMQpyDkiEA4lHFQxD4/heXAIgn8Zv4t8KczO9LcgdeAEb/BPTCunXgIYZLULpCsz9Vhc4t7QHdh800tZ0KIWLByzXDbwxdnw8yp0scXodxhbkZO4RQZmM91wPVA6J9C8+EaAcgjgEB41RhyS/MyViFpO/v0OxPAAwGsqFDVIsIq/dSyJFBu1J3TLProeu0zkvPoCU6WZdN50Lle38FkC2JlbMOfsA7n4IiJLx2RwzERuMt5pIT2JMDJRKyn6iEr1a3HLK97EQLjDnLZsiB9ETPxWsQol0HW9bruYT7nLio8rDrsXDfEpdUI3tpVqOl21FaBPwQ/B0Tx6IYDbTljVEbppXH91MqCvVI0BMSbkPDXr5oMdd9oB7mr9DOU+6zg5eKPkzC2uhpLBQTacVTv8Jg1KZHCuagLfxywzMOQUOgJDfjhsLczHVSheXgIIBgaHqTEHCSCsqnhTmZa4sX9uepAaD9ryh34KNFImk3COUZAaIFofVlSQgxFEnfrKLczAU0pOxLv3XyA1tryJFBi0Xo+lXgDFNN2W48VVHcByXa0HvWYbRyH33fOg49vuAN5pJ37ek26N4+QJAL4VJX4wltHXPe4xWwbrt7PDbtkY2quuP2yd7bZEYdqdElPXU7GLoFuxlwbasQwuOEVcyj5zAuM2CfiuohKFTOdEGy0weNLkehnjOMDIeH0OZylH4NllrOnWjIMDvCOmNRctDu4yhJKEYDbb1EjxU816ghLu8bAbmkY3xRbsYHKoi5AoB6YX0X8kNDCBgkpfUH7KmaKleAsV/qftQbjqpyXlZUERI4AGWKAGHGe+niIrttbenCzFPCEQ8jPnv/FjdiWW9ZKTvqLdpYztuWMqRjjU6lyJBUl2z2Wh4/7Fvk33JegN/XNFRM0lDzzwV2GDm+HHYcdCV85BsJqa3dplJkSNKThNfye4tc6/eq7CHToqjuhgA9aId+MuJPvTqeJ1v6bkKJbxXwstGmhtIAnTRpBVgJsToBqyBiOxBjMwPZXWqmQV+2sC30+fYd6hGBw6hl0JlCvSGL0b7u4Tud9bapYkW5GUcX16atAhBm/SgBD39W7Kl6uHhPxvLCj3sZ773wUEk4Jxfl9kgtjpP0XrrYzHYgGe9ul/Ctt7mEZtYXKrZChgwmpvY5KSG912llu5f1prl4RqTq0DqQXv7rqg855vsZqYMWqHirozn9GdRTQL8uRr77NdwyeBc3atHWMZdNq4TiisaU+ph8BuJuKHRcXSdhye+18GN+nW7ZfkD1graEVVvsum03+rV5y44Euj9I6lsS9q9Gx861EKPnESX6oNQrU7B8qwYkNzRv8Ut0gkgURqYHmhs503SrbgxiW2jUg1adHeMm26ykuQYNNf/oMmgucooXLsykBVr0qCnjk841wyKOBmvsDyVLM9ppLtIGFCsXZuJIUdwKbGrAhtNVsNCUFKxCZ5C1UmfJVikWMmQQFFgkQPl6/4qpx+1e9ggYkcL8fyHfER6lMG+OIfvkG9irPdp31CzA5e/iE6wu5wABOsV6X1lYA7fMqAKar9e8FuHG6xteqIa/PVmlW+5+3TsWby6u02270a9n565IFHh/kCQk9O0AYf4nhMg32IQkJBefebOBdWxA+cSA0K9ub1UENA/bR4+5+y9WEocSyHAHGo9CCXSgoeFeAa5kKNo3MtdTYHkOzRAgIigkvAAgoiHIfwKHjdVK+LHs096B+kEU5BYZr65awucAgqaOQMD+BBwJINwtNgNNf8IWVu+l0CGDmtANHyX84HBxdlCPAPcKNtS2r0iFyf92nv4UVvdjQyvaTESLB4w0NhsJ068o1ONkxE6oln0DHYuI6QF4jZ7CtkTqdcKgne11AAAQAElEQVSmRW4oXdQvS0hJ189YI42UFmJgnSXmUymhzX+gF+ZkTEQoM1E4mIgAk0ETwXQ21fw9mxQfrPewaz0CT1VpfF6gc9v42DQEdphgiYYcaZsRWoxggrnQMIHkiVYLnxga3hjzAttC80PvN2aFS7cGAnJZt1i73boAWqFHEFr+HV+8KCMUn1jU0tMApRQv7ttPCGF4H8IAuRfWZpkMBuDy4Yc/CCRhzqY7JDdLcM408Tg5rmU9AoSJNbApHwj4k220Z7Cxrng8mIT33T6UiSi0mACTwjOg/zQcNjU8vXfr9WxMDfDqMayBg+kIFB9KnAHYK2e6Yb0GJUxoawsbnKGStVHv4jm/lxAEswOTQbMRRXtCCJASD5zCvuJmCU55Zh4WVwSnHjN9bsO2sMfB1Na3R2sTUHYgofoRZRxKOBJDmsMXESsoEf8z8HqYsrkx2uEQRASKFvXrCyBvDGKVGqoSMSpYWnfIWoOXgVChR/qBANoBIBDmDdiMjKJMBgN0HZEOuljeXxycodr9zUgnkVIhuGfQ5WKEyIkQYj66QiviMDI9jECLz6BsQUKyCOVqlGAsksAqDYcLDVsIHQMtHu8VOq6xJ14RUK23AYjQ+/AUMLr804EhswcoBOnPDnBlkKpqk9UwGQzQZZfNPkLWbpMBqsnV7J+7mpPO4NTr6gWf+YFAoOcA0ZDK+ejP+yi7kRC+hoI9HngWggF9o018aUWsmd7Rg8rXoMFKFN1BZ0Gz9xMsQj9WoxxC4RBABPCT82rD5iXg9ZJfS5AzsJfxQwAwuosAmgBrrVW5hw6CIYoCu3Goa7mrwB9665ZSVrraki628atzg3vbgh476T5LX2ohSPgDfTmor3hklQodMqjCSAnq6e2PeWhZ5xFTwYikZlwL0st/asb1hux3Gj4ZrVPfn5da8JPE+Vb5elWd82nAjhf/1rIeL15iOyS0P2qcITzSj7jXYcdTPYm9RhmyT/dCx+MmIp1WT5d4j5SVbdoXMACDbFgI8RVW+SZKMAI92/ZWrGgjfiD/B+VIPA61cJVJDtGG2+ehrXaIcVeUoSi0Rc1gTJuMEvCA+NKj74w8ZabRx2I8oOHKntiGVJRhKLTvHNmm9ELM52AiAoU5Ay4SIDobMymfT8nOT00ZXXBG6uiCuzG+KmV0fhZ+WF6J90apIdtSXm6ovB+Fk0at/zQlu2CEsyhg102UBcB2Z1stjkcXtJgvXJKbMUII6OGH2+5VkZxj/VdZ7VWd8Fqk4fU5CutPtyi1HSgdC7XZH1khQwZLCzf/UHZg6zeJXU7cEJs+FIxIdGoGCC//9AQSz/Z91x3X/kiwRCd7qYFqx9vKKfy2yQ6Bns+HHzDw1SrsTHeqlw7JG28S32m4IbxtcR28YhGd3NeQfbpWcR2O3kH3Bwm1KZJECHELtmc+SjADfZnQKuRP8L4JpXmFZgwR5yKQwxHXJSguH+54vg5lPOZno7jk4bnZgTA2arMADZyAPs9BcVlwhOc7UeZgPk0JIPKLhxzMQUAhkm3AlHwUyd8D7gwgAfmPBeBcAFntLl9bmuhVlNs/sPvsaXMkKFr4rWb4R6KUsE5a5ZHJo/M/TLhwi8ujPBNHbtxP6dIij8bPQ+p5D0q7QqmSkCGDjaBIUHY1HuuNiex5K1tXbfyHdHTKQG9VAH5IN8sX8Obi2mZp5p7O+74WSpoNhLX0w7VOW3xXUKzGppLVVXnH02LC4/+EIva7eh5xZ/RLe3ErtIrI12r8ALy+Fep2V+UQd4l+pP0H7/lslDJvZTCfCCNtKFvlTc9g3lEGy69HPzNRiBB6NEX5KIRbnkclzvCMgJsc/PF8sptkbUkS/kQi2KJ3y7lwUnbBcinFPOc0f4+FtIz0t0y46uP1MLQICwfq8lKz8wennl+w1RsGlJ+aXTBMgmxzP65CjwzaxUZvF0tLni2+i1c1e3WR13wtmQldT/Gphl+wLjovf1YDOw6oLmlmnZRWqvCEy2bT+LtT4lvARwXx3U71oeE72xeetgTv18N3DQB2KVx6RbSUCScd/DKn8X3qrXoa/XbeNRxPAx5o+PgdvF9fCHhNXirA+umJI140fGbRr5L7fGo1KCDm1ANAj+xrSDE9MjjMCJrb0uB50OaRNdQXkZH8dGAiCNC9ml0oqrbHG1rEiyBd58v5da4ATRWAtvCHX2XGvkSkvMMfnCyKpOk0/hQJe92QI4N2sP9iFFVhsYE1jrYqc2/JXlPkPsOP1PiO+ENFeH/EHH7ZuFikx8Pd/moVVFb7JmkuBTWc3DurGg41m4XSvH53ZhK66P8B3GjPOxlUwBbfCQz/SXWVQRshXxyvVx3KQ+gobbbcGu29DwkZPfUDXWiVYHRO0JOI324/PX8S9TejmBoQR/psNXLj02MEP/fHKWz7UtSnRQoYcdCLQJFV7au3LJKWsiRr0dtayqeOzPsjpdlcPH/Ok0cVPKGlnnDXQUzx1gYjP6zmY2/fN/7gkDRyPW3N9S9/yoS7Ln1ghVQb8t7JyjODKtlw+NNTw2qKDHc+gmKLc8yD81RHY3rztvyyQYW73zB3ZOq5BTWw8Fd7Y5WOGL+MHLG3F8IoKrG7NxVNeTXFnvG0Yi+tEBZNdrwpqTLqZ2/5kZSHn3y03QwNMY7BdgWbFN6C985crLc1QjcDldIbYIa/5RFrmlgRiPma9MYycuO/529bGvTfaYg50ouAKvQ/P1rA7+LcveV6q+ZyLREoX5RFPTvee15aFgOA+kQhQdfnmdUCtFk8tJW/kCODAAK/i8AwW4pK9jy3trZ8F9SW7wGjf3Gdfe9/KdxUkvOLHe56vQpqao0NGRNQT/6nCogMNq8Gv+SaJ7U4j+s0vEWavwmqvRoqD9CuHe5LRqfo/pHdZJDamde7nxlbMjTZDPUDvH4qyjsoR6Cvf0P5DiVY4QbEnIhosOprrMfIQhZaTFHTaMjPeL2f+lrUe2tR8qKzzkuet6zfvWVyngYEhEU/GZSSVn5rqIRVtCJgh9qeWnXd6alWqWsubV11VbB/iLtzP2hpIUgGAekg+DvUA83/YtK8Tz8q3/1T8yJ+nyf1PNfrcLQ3g/N/qINLn6qCfUX6CGFJhQo3v1wFM3Joqpm3mtznCWsspA4wvtixcu9vANKzDzFpWe4d8CNVgiiDiUIfUBD+f0gIF6DQ5E4adrwLW7QExfAPJrThLTyKhDDYnw+65gU1NGJLQ6wnCsQPDSO9nDV4vQv0NATL0bZLgV4lrce1sCmDN70B8iGMz0EKG6SC46gEi/73koQaWhSix9PUi7cWgZRtZg9CvO/1wBTYMlJIz+OOGquOTfdOBiv2Gp6aCAKHP2nPQo0utVCjIeMRD1TAix9XQ0WVbJHvLqHOjn3eS2th+P0VLYaGSR+/wCnyKSn9L3cMdftU9KFQ7gNHM8igALnDhxttIhu/6PeizEQ5DxtMQye0/cVCPA5E6IdGb0IJZuhgoDLdZBDxNP7LsKXjRNxbpmpL0d2WBvNGyzeYaZsRfhLr76EWknsGTb5tsBtA93xBKcAYlxBik8nNCVlzIUkGQRFu5of5h6FijQNboufe/qqDa0GtNT61I7HbqViP/h+SZdi/8/RHtTD4jnLs6auEeT/UwpotdtiLPYa1dRL2F0tYt80On/1U65hrOPTOcnhobjUcbLZYpBEd/GJrPPQYK1HJkNyHdhTxqKIpg4hnxZ6fPeoKSwxEJenHpsmwUPQOmTWZiLQDvM4lKLT33GhsG5GoezE2/gsHjTiFS52Og3FYYaCSVL1l8T424SZtUbuRDxfdK1kbvEhuiDnSgQD++NR/7aRg7HVg7q2IClL39RAAtFOCN/M+8qTR96IP+6GTHZJkUNQopkzc9N4rpULFPnOm16RljTV8RSuqAXJ/scPdr1fD2Y9XwrA7K6D7mHIYgiTxzEcr4ZaZ1TDv+7oWK4b1VJyacQ0oFmN7C1K91UUbkFB7YKWoENNuMAhh/BazS9ka++9hC8IjICncj/ISyvHoMfUarsDYjJBhhhE/bNAQpx/qLqqeJwm7qLk98T6M4LaIz0Qjk5I7IkFN8FmDZwWj8xU9Ww5mTivVpUqheyQCyYeRqQ6t1OLQrlZRDE0b6yS/7hWju4USjE961115cAsa/6YOgL+r383YIiXonQze5FEcbf/SdNbywFuvVkttzynxHY+BpN4XeFZoloMf9NAozbL8Om20IUFqLhebPgySehJf0FzEo2L5nl885lFGHOJCsRHB+0Ba6tL+bcRGWyqLhJCeunEstvkWFKOhB95junvcdFRuhEAZIUAZOnz1VWSnLwUf+bSa3IdKy2y8XsMw1YLCQScCAlTd1w4/iY1MD9DpcWQXs4DQfT0ImZKyqKEU+yslCzMGgBAWf8uFq35IkkECE99UhjcZjut4FCi2eDLnVmi+G354us3zN7Hd4Jshtj0t/PRdEr+wgYQ0qX4SOtYqpE8CIIDs4Cto+bMl9oSOxz6KZcy57N7nXSqQ0PU0LW750ilf/W4n3cMEvoxHaj7eF29i20ahGA3HGDXgR3kjZDAB3xNX+FGXs+plzicmHf9l0I7etlxjsN42X1wIRfe1EyCZDJp8B9ktiiEyCMKi61F2dlVcZ3JTQtqcOawgEE0U8LVRswJJfZyXTZVlXQWU7fRrL0qPLgkcDu14zCMQleR5nmLzwkIgjWsQ/CJr6i109PRhlxjpO6ermObIwwwhqCweaAyWmDTofMIkJMdxGkt4V6s6lA+1JZ7nqRMxtkQZGelqqj8i5wvidT0H5WcD4vVxV4SeEGIRxt+jGAn0mDMj5f0pa/RRlBP9qYx0EX9aUu97jyhS9kMQ+92o7nmZPWb6CDehb34NUaF+V7R5JwoHAwjgl+I23cWFaFeSk6Fpzy65rFtscW7GuXqlcFHWkbr9NFrQKrC/Rq8R/8oln5d3CEvofi9JkLdV5Ayg9waa0RZKF/Vrj5r/RGkzAe/70GyrXdqmm+FZYlfakcOzpUN5c0Gquu8zF8OKLQ46HT8RLDH+PyUIvzwctihGmgcgBNCfEAIPGwQT8MhxjoeaA20jQ0TQGuu/X54qObDmdU9ZjvR4DY/rcyj6eJGq5T8+VMI1m1YdHofO65WLsayW8KMWJS86VV7yzM7aadBgBhIif4fHJxus01vxvd4yfeRFY/5EFH/CFFSOReFgAIGE6GrdPYNUrQriMYp9SfHBxLskiMV6RVHVVuu5stSA58niPhouBST5UGmRLaWReYMiugaUyS2Mekmwq9Yp+NVrTs+Jl3pCKStkyWDenP55KkC1UbBoEYMl2vOCIHvVQSjenGO0mqby1th06HrKCxCdGohpSE3VaD6gp4x0PeVFv3osfRkv27UMaoq9rLjHHtmELvRENV+WvOfjB4AsrIjzzjq9mwjl3D8MOjcAiY+WlYtG30O6t2bwt31CCOoFNkKgqMrJiMu5dOBLUO9fqDMAJVDB6Aq1a9HH+7U4QPhpzQAAEABJREFU16A3Rosu63hHQJy9uRg/e+he9K7oKVfAyMLcgad7yqZ0uahfEgi4j471ihTCr8cV6q3HXbn4C9fr7sXHDo3OZQuz/BpOF0L+6s4PzWlCjC3OybxDi369nvD3R6UW0yGtE7JkkFBTQBheFYlfMJDUK5vMeZTCDf8GM7aZaazAij2DXU58ChJ89Eo26gcqjkkfisQUiWCC/j07m/smpR0OrXurebLLeUKPs0CxmtFBoe7cMb87PS7MxX4knOB9SQukdK9aRAwUlMdQfAWjc/4KfFVgcj4NbRsxSdvsLERy9CJKlDtDmH4iCpHcQPesLHBXv59pz6GvS1E6uyuH6d1R6JnEz7nL5zSdCCig6fnCnqwj4VmEw8Uev3iKVet/saxfhAj1mwKS1Yrk8vyvmhJa4YB80FttnZTvVizpS49s1GZCinnaFD1rSQEzi3IzFhcvznK73Uzpkr4dKJ/0PFuJ3Bz6QjHYusAVr1XVV82wntz3AiQnnheS0NzBwg2G7zUXV4Vigw5Hj4O0wbeAhODDnNznYuh8wmSgoWsXxwyelGxdAnWVXnYAEQqk9dc7993VOVWISF9FvMy1xX6fPYBEwOMvWMyjeYWj/LZ6uEAlklbDC7kOm9N0ZMYbkVYA0r6LRYjBKpSPUN5A+RqFCPgP6ElflIAGxI6eiWrG4qcz0dFd6Pt6lM9RXkMhwkuP0aPrQ/mowsE0BGoq5wBIA1MkRAx+fn1WmJuRW5iTMbFk4cAL6+cGDry2KDcTRwXE2cZ8lf8Vl4PdmA2DpY1tsH1WTW3UdsRnR1FO5vKinIxlTkKfWy7OpWTn/wfJZ5lLoq4Tca6skwfxGuThdclBeQl9+JTO7bVROCohNI0q6Ko6xAsFn6X4AUje3EEfSADDE/qolyqpj/etX0q2fIYk54Af3mlTTcF6u506HWi4WlsJY1q2xB7QafgUaDf4JhAKfScas+dcWq2rgsL17zsntThO6Ho6WOOoc6ZFll8J+MaXew508Gueh18VhIayGWSXSM73SA4eR7ka5WaUp1BoEvzDBptpeBGXv/ULIWhPSd1DUM3qo+5p2lbiEkwn0kzL2/2aSI7ljIb/GTXgVJ6GtGlfqFsxbSQKnWMU5KCzujop7inKHThNrxQvHHi3zqr9LuZ4FBnAJ2DwD3sIR+E9PUGVyifSMT9QeRdNatt2AhU9BasQz3rKC1q6hA1G60J8uoKAE0CI4U0CItOdXSHA6KiBs9lMIcRolLvRByIHbut0LhDpxyFNBgl8KcH7ZnakpEGS+14I9EQMT6pSrYVD+fQ+9aShPz06uQ90OXEadDxuAljjzRuydfbIEt0O0o+4F7qdNhPi2h/hnGXacdGmBaDW0LoHTyYFpA40p1cQa9h24LP2uicpY/lwCJ+hk0a2U8HijnASvk5CIaZOW8o8hMc9UIyG14wa0Fk+UI/Y0+mOoWILDJWOrMI3ASgP6hVVFVg+eIBIkLOCV5s/NcmvE0fnr/WnRIB0zdmKQ6tzQpoxaqC1tjanF/Jk0C7lQ0gIpdErY7ElQMoA70SlbMeX4GsjZSN+xHc6Fnqc+Tq0G3I7RKea80PEltQbUjNvgO5nzoKkHmfhjyvTLqlLU6uL/oSiP72/FxN7nge2eJM24JfKSy4OROCJEELFZoUqWchH/3LRv9YIr7RGpYGoEzF8D+1uRuEQZgikjl7/NRJCo6vxTW+1AtKUnTaMOmaxyC+M2vCnfMqogo9AAs319acY62pEIDDMQWPlWtRwqPh71CtBMRxS+l4MNh+LKfatmAZEfAxX5sVAcu+R0PXkZ6Hnue9D+rB7ILbj8fhjOcpLCacsYYWY9GHQbvA/oMfZc6H7aTMgtf9loFijIVB/tRV7YfdPEwC8bMGjRCVBWtYNZrlQt3puRkh84JnVIC92nsE82kcLo5AKs1rLGyRQq7Dup1EiJdyGDZEoHMIMgWipUg9CKL0/lyaNXk8jCgFA0j+TieevXwYgg/pDRxH2G7FOfi/5d6k0aYc8GaRWYPfJfIqNilCs0P4I79NOpFrjID51lebPH2zuvyU6GZJ6ng2dj38c+oxeAF1O8j4NpP2R/4Q+2Z9AlxFPQnKfbKBtbJrbNPtcra2A3csfx+Fh73w8LWssWLD31Yz6VQkh92vcjHa5s4HEh+b2/cNdXium5aFfrU3Gx2P7/0QJ+4BY0rzBd8O+IW2wAXHZG3YKCdeFBAGRcp+wCiKnIXElhMB+OgH0YzZo/iSN3oCdQ2J20CpsQxUp4dDWQ7aUe9BPwwtJ0AbEpGVBfNdT6dCjqDUlSIAew44wA4vJPFr3nOFtP0Qq5SufdMwU2kZmzy+ToK7c+3z+6NQMSOpxtilV05SAOgl/N8VYmBgRQtA2E0H7gPMBy17Mz0Zp1YCY0NY7tOijdVdMmocCPc2AsDXPIlsKCgLJ2fmLkAwGlfS0aJiUKljkVQ1P42iR3VoJyR0LZiMlDOp9nazU0nvJjLnWrQVbSNYbFmRw16wuFdg7uMQsBNMH3wK+iFVt2Q7Y++sUoPegWfWGm539K2dA1UHv85SFJdqxhY55bZMF+XOzIqJHyE9MaCjR+6RMPw3qUC/DMhcgEQvq0A/W6TagHzRB3fumlm5L+p1IcyP1bs4ttdSGbTmIenehBDpswQpWoOgNmtqj13i4lksetf4R9P1nlOAHCTWgyKtSRq5v1X0F3TVcHAO1QlHH4Bdl0H60iZEbSxQhg9FhQE9W0H/NZW2ovJfcXboWaWFBBslr7KWiL0s6NCw0PNvxGNp1Q3i1Vbl/JRxc+6ZXnUjNLNr4CZT9RXvZem9h+yPuBVtcR+9KfuTaJa029KNAhKgiWahBoSGg1uqBoA/za9CHX0IMUnpKw6cB9CkPbV+F4v3DABU8BM3lEFua7kKkQvVgy2gyrb6/GI0Y2ahdc3uwnjYThAAVe6TOwW/3QN6LLfGUUGSxyHNSRq1v7R+KLX1rSEGivFgA0L6eEKy/pFEFuVLAP5GEBuS9JKUsRcI5EnuEK3S3SdgQFt2lg14wbMjg2jlD/lIlLDMLoZh2gyA14zqf5kq25EDhn/QZ7lM1YhTKdn4Ph/Le8tmehJ7nQkLXk33qaVWQqty+bm6mec8G1FpxCOkJIR5Ed2iYdivGwQr06K2zse6QmJju3Gj0iTa+vgjTvE+oRQUdgRYGXIh1UI+ojuL+F8G6nsJS16KYPQdFBYCxaH8V2uYQAASoRyp1dP5F+A1/B5IFI4Rbk3cS5DKQ6lGJIwu+1VSgFZWSswtmgpS34JAxTe8Iiiepo/JflKD+zfRrIaWqgLgSCafhfRSDAoRJlSgm2QmKmXJb9FVS4u1mUm0p/S9zrMz1Za4w/x3Y98d0kCp1nvjSDu/8QwUfwN4VT/tshC2hO6QP/odPPa0KdF3rLHCjVv1I1sMv9Fxs32CU51EC+eFKq4Kop2oo1hn0DaaxbZoD+vd/qHwnSh2KGeE3NHIS2t2IcVAD1kmbjZ+LlZq1Sm032spGux9hzCHACCSPzn/VKsRxWA0NI2JkbpBSItGU41JGFZyUcsF6GvY3t4IAWUvJLnhTKHWngJRB8zk1e8MnCqinYZ1eHoulvcGI/S4p5Fn180S1l4sEzbAig5tn9d2OXcOm7W2EH57Q8diHgZ7a4eti0pDprh8fBHtN0DoRPLkUkHTVXgN7f30KijZ8AAJ/+nqrxBKdCp2HTwLFonE7HG/GmvLk+ry3B33ZdNrGD4QQ5SgPIAz9UWhOw68YmxVoTiANRxMJfArrUc0yHEg76CftP3gE1kF79+ntWSMC/ATaOgYlH201BjNv5kabHmOs+zvMHITyBIqRLzIaPhyG9hahHQ5BQoA2fU7ulJ8JgraekfTUHOM/UqQsQFLziE2t7p0yuuA5Iczr+AgSLJA86s+fk0cX9HXgIuGnYNSbnL3hF1lXNZCwQ8R0L2aRAO+lxNRk0f6SwfA71OoIKzJI4InatEswrkUxJVhsCdBlxFSwapj3Vl1YADu/vRtqSv8ype5QMVJXdRB2ff8AlO/2vaOLQnidOA2sse1Nc19ib6+qRo02zWAEGRJCbEeZhkI9EQOwaUQMP8aYeoMw0hz2o+brKGegrb4oD6Jsw/OwCujzOhSa39EXHaee00KMtQSafnA9lk1GmailgEYd3SQA/diHQr70wrpozpWWYSki7vQlSz8UOmH5K1Do2qIJDsFEgBZPpIxaPw+J2/k2u9pTSHgcyciffvkg5e9IYqYpIE9MyS7IRHkq4cItzQiNXxZbXVkgiXXgkp0/PErauwmp0rSI17Cdy1EK0ME92AOHvZ94ZFJIvXhrEWGXXCl6gJC3NtTj3bqUqgT5I143HHEoT0sdnX+dOHuzt0dsebcX5rlKuPm/+t1O5VKF6WDinyU6BehxcZboNJ9W6yr3wc7v7oOK/St96oaDQnXxJtjxDRLcEuos8u4xPc6v84inwJbQ1buin7n4i+zzte/0D8iQi5+uhLS6EOJPFCKGl2BMj3o5Bh2mxQ80Xj8OjyejPIdCQ783Y0wE+1iMu6B+B5TbUEwZDkY7n6DoDTQnEt3SH7DiXSgPoNCbth9aOgPlGhR6FN8zGNPCk4sxpl4zVBO0StrtXn9SSiKWqKorGB7qRedoXuRLGA9ED+jNNQLjy1CoDTRng34A0OKiE1DHgjIc5XkUt6QB009B0Ru8zsvGL9wRKaPzRWtIanbBMMSkRdDvS0GHFsZ0JsRfuH4XDi1OScnOH5AcXZ2iCDlQATsOmaqXEdnAewx7o+VEIdR78PwGzMf3QHka4nk0ysNJowu84u6PWymj1z+kFxMkRKZ+t9I+jcnZ699Hf27Hdo5AycTjzngt4zB2fx9l59P71p8mN+mKy/NqUkYVvEH1KDEyXYq6IwXAaJDqbUj8JkmA+xQpL7Ba1GGoY0kdXXASXrdXUkZvd/ujMmV0wRke/fTxPkgdmfdHk2NhcBB2ZJAwXT036/9UCTTcQ6emCPV0dT7xKVCQGPoyKO1VsGf5Y1C4cYEv1ZDOL93xLfZ03gPenzdc3wQigp2GPwnRyb3rE8x7rS1MSbzUPHPha8lfz4UQv6F8iDILBYeVxHiMx6HQ0O/bGC9EWYHiby+iv660qj62bxPK1ygfoDyNQr2e0zEmwrpag3MDNOh4UtnvKUNPOvpMJHc5xv9FoTY8hDH9AJiHsf5tLvQ4w2V0IUC9S0mjCjYkjd7wfUr2+v8S2UDyMzF5VMETyaPWv4zn/8L8XE8ERFelXKgFAklnFRxMHfXnyuTR+QvxOryOxG8Ckd2k7IKchPPXa/lcaGEzkhPCkgw6LkgdmLqYhGxGYY9X15OfxyFQbT8YC/Nmw87vx0GgH19HvpkpNWU7YfcvU2D/79oWaDqGhk96BmLTBprphsMWkvoHd7zY3dQhA4dhfmEEtCNgpGdwj/ZqWJMRYAQYgSYEQuogbMngmnezFmH3r++N8PyEm0miFJoAABAASURBVPbM63rKC2BL7KmpZHVhPtCw8Z5fn4KaMu9P6tBkMIBKdVWFsG/ly/DXV7dB5R6aduS7MktMO+h6yovYI9jHt7KfGjh0UrBmTtaLfhZj9QhGAO+J9igX6RRakKEHnVF6CjWUYTLYAARHjAAjEL4IhC0ZJMhXlWWeD9Lc4WKyS3MIu578LESnZtKpJqnY/aODZO1f9QoQ6dJUKEhK9tpyOJj3DmxfehOUbf8fCKA56L4rt8V3RSI4HWzxnX0r+60ha6uqrOZtUuh3/VwgRBGwol8f65R/Yzm/ApLO47DAeSh6g28yqNcyl2MEGAFGIEgIhDUZhPnCXltnPUeC0MZu/ABVscZB5xFPQlLv0ZpLCbBD6bbP4a8vb4aD+e+CvU7/5uWaK/WiqNprHPMa/0ISWLxxPoBa40XbNSuu43HQ9dQXwRqT6pphwhmtHpaquH7DvwceMMEcm4ggBIQQNL9xr84mDUFyRysX/Sk+xR9lN7o8j88NKJzECDAC4YVAeJNBxDrv3QE/Y+9gQIYaaR+99CG3QodjHgJ6Bi9W5y005Ul7NRT/+R/YvuQ62PPTJCjZugjqKoPDe+zVxVCyfSnsWTHNUT/Na1Rr/dgbUVghbdDfodPx44EIcVOjTD0QuavnZn1oqkk2FkkI0BNR9LbnLSSEPod9UacLCvVAnq23Iix3QAjBe2MiEBwYAUYgvBEIezJI8K+ek/kASJlHx4GQhC4nQbdTXwZbQje/zBMprNj3CxxY/Sps/2IM/PX1HXBw3RyoPLAWpKp7ezIXH/ALDaoK18Oh9R/Ajm/vg62Lr4EDK6dDxa4fQK0rd9H1dWKJSYMuJz0DKX0v9KWqP1+K/Xi9LtBvgEu2AQSMrPSLRnxy8X3xHcqdKCejOCa8YnwCynUoz6EOEc6LMDYSco0U5rLhjgD7zwhEDgIRQQbpctTGyuPxQ94/9kMFNQrtrdf9jNchZeDVGku0VKst3QbFmz6C3csegi25F8G2/42FnT/8n6MX7+C62VCy7X8tCzmllO/+CWju397fnoWdPz4E25feDFtysmHX9/dDEZLBmuI/QQinAn4cJvYcCd1Ofw1iUo3ssuGrQllXUx1N+975UuT8to0A9dgZRYDmo85AI/Skj0342SDxeDnKv1DuR0lBMRpyjBrg8owAI8AIhAICSig4YYYPea8OLqtVxAkA0pwuNw9OpSEZ7H7mmxCTpnfh4mHD9qr9UH0oz9GLV7xpATjm9R3ObnFUum2RQ6d857dQfXAt1FUYn7seldQLup4yHdoPux0stvgWdZqVQPMEVVVcmP9Bn7B76oVZGLAdbQgIIYjA/aRNu9W06IdnbqvVzhUzAowAI2AiAhFDBgmT/Lez1tZJZbQMwIISst8otLq2y0lPQ/ujHtC0SXVjuVCKhSXOMTewKw5/R6fQAxwC5x0RQRDwzzVzsxYFrha2HGEI0HOIQ7lJHyNp1b4iK5Rbwr4xAoyAMwJt8jiiyCBdwXVzMpeoUt7pICCUEEBJ7HYa9DjzLSRVt4A1tkMAazLPtBKVAikDrobuZ7/tmBsoRDBuATlj9eys6ea1gi1FOgJItN7DNobqSt1D6BsNNWPEgRFgBBiB8EcgGEwg6CitnZP1mhDwTDAIoWKNQVJ1AXTHoeP2R42DqGQjDzOAgP1Z47tAu6G3Q4+z50BaxtVgjUoMWF3OhpGY/3f1nEH3OKfxMSOgEYFrUc+PpfCo7U/Qr3u7EGKf/uJckhFgBBiB0EIgIskgQbxqdtZDEuQsOg6GCMUCid1OhW6nvgRdT50JtNDEltQ7GFV7rIMIYHLfv0Hnk56D7me8Acm9RoJisXnUNzsDyfiiNXMGXWa2XbbXNhBAwrURW/ogSigFekbwf0LJIfaFEWAEGAGjCEQsGSRgkIjcKqV4DkmJpPNgSXRyL3AsNDltBnRrGEaO7XQCCGvgFmg42qZEQWz7oyAtcwwS0hk4hD0L2g0aC7FpGYBfrA6VIL4sXD0na1QQ6+OqIhABvG9fxWY9gRLU9zDW5y7sx8TbUDgwAowAIxBRCCgR1Ro3jVk9J3OcCvL/gk0IG12Jiu/kGEbufNxj0Hvkf7CX7nkkazdAfJfTcEi5HwhLTKOqfzESP1tiL4jrfCLQHMDOI6ZCn9ELoPPwSZDS/1KITm6dXknCWarq29gzq/3RLf61nLXbGAJCiInY5H+gBHSnALTvLazFzDPQF5oviIccQhcB9owRYAT8RSDiySABsnbOoOfsALfIAK8yprp8SWzaQCRrl0HHYx7AIeXp0HvUf6HHOe9A5xFPQ7uhd3otnjrgKtR7CrqfNdtB/LqfPhM6Hfsw0BzA2PShXssGI9NBBEFOXj138M3BqI/raDsIIAl7E1tLPzA2YxzsMAvrH4JChDDYdXN9jAAjwAgEHIE2QQYJxXVzst4SEi4ECcgLKSV0xBrTDmLTB0Fc+2FenYpOy0S9IWCLC72Vy0QE0fk71swZNAHjoASupG0hgGRsCbZ4MAo9QSQY27oUYl1XYL3UK4mHHBgBRoARiEwE2gwZpMu3ak5mbp2wHC0BQnGFIrkYloJEsEZVIXv1nKzXwrIB7HTYIIDErBJlHDqchULbFQXiod8lDbYHYV3z8JgDI8AItD4C7EEAEWhTZJBwXDd74KrdpemdpZR/IomhJBYDCCCxPlBRY+u79p2shQbMcFFGwC8EkKRtQrkPpT0WvBWFnlhSgbGRsAIL0+rlfmiXbO/Gcw6MACPACEQ8Am2ODNIV3T+/Q9nqnlkZAgQ/TooA0SlIpn+zWVN6bny//w6dJrgYI9ASAT9TkLi9gTIcJR6L0jDyNRg/g/IBylco+SiNCz9o6Hcrnv+OQu//FzAmMjkQyx+L8gwKrRrGZA6MACPACLQNBNokGXRc2olCxWHjbFXKSUhqsIPLkcovmhEQ81f3zDzut1ldjPbGaK6RFRkBXwggkVuH8gHKgyjXoJyJkoXSDoVCGr70RjkaJRvlfhQikxt82eZ8RoARYAQiFYHWJIMhgemaOYMm1NXJK5AQhtzCkpAAyMkJxAgcApZHV83OvByQUAP/MQKMACPACDACjEBYI9DmySBdvbx3B82XIM5UJZRITCDCgxGHZggIIWullJevnj1warMsPmUEGAFGwAcCnM0IMAKhigCTwYYrs2ZO5rdCKMcByB1CNCRy5IxAsSqVs9fMHTTfOZGPGQFGgBFgBBgBRiC8EWAy6HT9Vs/OWF+nymNxLHSlo4cQ8yQdYNwWA7Wdmi+RIEtQjifCrAUH1mEEGAFGgBFgBBiB8EGAyWCza5U3d/Aeqy31RJCwtLGDEIdG8bSZYoSeOghgPQME6iEVUq4kgkxEOUKbzM1iBBgBRoAR0I8Al4wABJgMurmItEJ2dVnmeThk/L6DENEL6jmIEsaRGBr4n6NpjuYiE8b2LiViTATZkcEvjAAjwAgwAowAIxBxCDAZ9HRJ5wv7qtmDrgUpJpAKciNHTxkga0KSRBElh71gc3BUvL4ZTW2kUwmziRATMaZTFkYAGAJGgBFgBBiBiESAyaCPy7pqTuYkFSSSQnBsPUO9Zg7Bco3Dx0QO8TRsAhFAcpbiRgJIMaU1yPhVc7JuAiTEDeccMQKMACPACDACjECEIuCODEZoU/U3a83sQe+rAs5D8lTqbEUIAQITMALqKiRSSIJJIRfIr0Yhn8nBxpiO60XWEvFdNTtrcv05vzICjAAjwAgwAoxApCPAZFDjFV4zO2upsMvh2Bu4x20RZFYOUoiZRLowahp+peNgC/rpqJ98IaH6yT8SOm4uRHRV2joGiW/zPD5nBBiBSEaA28YIMAJtHQEmg37cAaveGbQOLFHHYi/gOk/FiGyRUH5jLJFpYWjoPXQcUbap4qgDXzCgexKEECjgIuDhj7aOIaLLW8d4AIiTGQFGgBFgBBiBCEaAyaCfF3f1W/13VAkFewjhW61FkZeBIGV8EQJf8Jh67jByELd6AodcUVKKd3HWJXU6pxJkVggBGABfKamFuE2QsI62jnEQXbcKnMgIMAKMACPACDACkYwAk0EdV3f97IxSmy3zbKRv7+so7igihKiPibrhIQZoSHKke3ohnUbdxtiTrq90JJLfErHlrWN8IcX5jAAjwAiEHQLsMCOgGQEmg5qhclX8bZaoXeXYegamuOaEy5l8nwgtEdtw8Zj9ZAQYAUaAEWAEGAHzEWAyaBDTVXOyHgdV3oTjvY6tZwyaC1bxyURkidAGq0KuJ4AIsGlGgBFgBBgBRsAAAoqBsly0AYFVcwfNpq1ncNi4oiEpNCMJdiKuq2ZnjQ9NB9krRoARYAQYAUaAEfCGQCDymAyahCptPVOryhOllO63njGpHv1mZAURViKu+m1wSUaAEWAEGAFGgBGINASYDJp4RfPmDl5JW89IgPUmmjVsiggqEVUirIaNsQFGgBEIEgJcDSPACDACwUGAyaDJONPWM2XW6ONppa7JpnWZcxBTS9SxDqKqywIXYgQYAUaAEWAEGIFIRoDJYACu7uZZfYtppS6AnK/FfKB0iJASMSWCGqg62C4jwAgwAowAI8AIhDcCTAYDdP1ope6q2YMuBwnPBKgKH2blfCKkREx9KHI2I8AIMAKMQPAQ4JoYgZBDgMlggC/JqjlZD2IP3e04XKsGuKom81LKp4iIEiFtSuQDRoARYAQYAUaAEWAE3CDAZNANKGYnrZ6T9ZoEORpJYZXZtp3tOQinKm9aPWfQI87pfNxKCHC1jAAjwAgwAoxAGCDAZDBIF2nN7EGfq0I5GYeN9wemSllBhJO3jgkMumyVEWAEGAFGgBHwhkA45zEZDOLVWzs7Y0WNAsfiMO4mU6uVsL9+65hBn5tql40xAowAI8AIMAKMQMQjwGQwyJc4/+2sbXUKHIdDusvNqBrtrCeCyVvHmIEm22AEtCDAOowAI8AIRBYCTAZb4XrmvT3oUEW87XTsIfzUSPVIBJfT1jFEMI3Y4bKMACPACDACjAAj0HYRYDLo5doHMmvjjP7Vq+dkXQwSXtRXj5xPhJK3jtGHHpdiBBgBRoARYAQYgXoEmAzW49BKr0KumpP1TynhdhTs6NPmhpTiOdo6hgilthKsxQgwAowAI+ADAc5mBNosAkwGQ+DSYw8hbT1zMQ4bV3t1R4AqkTiunpM5zqseZzICjAAjwAgwAowAI6ARASaDGoEKtNqaOYM+Ldnz8xXe6inf8+t4Io7edDhPAwKswggwAowAI8AIMAJNCDAZbIKi9Q8Orn5jrTcvSjZ/tsJbPucxAowAI8AIMAKMgCsCfOYbASaDvjFiDUaAEWAEGAFGgBFgBCIWASaDEXtpuWGMQFtDgNvLCDACjAAjoAcBJoN6UOMyjAAjwAgwAowAI8AIRAgCYUkGIwR7bgYjwAgwAowAI8AIMAKtjgCTwVa/BIcdqC7+a1Ppgc3Ck5Qf2rrksDYfMQKMACPQJhDgRjICjECAEWAkALzOAAABUklEQVQyGGCA2TwjwAgwAowAI8AIMAKhjACTwVC+Om3NN24vI8AIMAKMACPACAQdASaDQYecK2QEGAFGgBFgBBgBRiB0EGAyGDrXgj1hBBgBRoARYAQYAUYg6AgwGQw65FwhI9DWEOD2MgKMACPACIQyAkwGQ/nqsG+MACPACDACjAAjwAgEGAFTyWCAfWXzjAAjwAgwAowAI8AIMAImI8Bk0GRA2RwjwAgwAm0EAW4mI8AIRAgCTAYj5EJyMxgBRoARYAQYAUaAEdCDAJNBPai1tTLcXkaAEWAEGAFGgBGIWASYDEbspeWGMQKMACPACDAC/iPAJdoeAkwG29415xYzAowAI8AIMAKMACPQhACTwSYo+IARaGsIcHsZAUaAEWAEGAEAJoN8FzACjAAjwAgwAowAIxDpCHhpH5NBL+BwFiPACDACjAAjwAgwApGOwP8DAAD//7nCR6EAAAAGSURBVAMAmJdJgPrJezwAAAAASUVORK5CYII=" alt="SigaLicita" width="170" height="64">
    <img class="marca marca-clara" src="data:image/png;base64,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" alt="SigaLicita">
    <div class="top-t"><h1>Busca Licitações</h1><p>Licitações com proposta em aberto no PNCP, com filtro por portal</p></div>
    <!--CONTA-->
    <button type="button" class="aba-op" id="aba-op" aria-pressed="false" title="Licitações que você salvou para analisar depois">⭐ Oportunidades <b id="n-salvas">0</b></button>
    <button type="button" class="tema" id="tema" title="Versão clara / escura" aria-label="Trocar entre versão clara e escura"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg></button>
  </div>

  <form class="busca" id="f" autocomplete="off">
    <label class="q"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
      <input id="q" placeholder="O que sua empresa vende? Ex.: material de escritório" aria-label="Palavra-chave">
      <button type="button" id="limpa" title="Apagar o texto" aria-label="Apagar o texto" hidden>&times;</button></label>
    <select id="uf" aria-label="Estado"></select>
    <button id="bt" type="submit">Buscar</button>
  </form>

  <div class="grade">
    <aside id="filtros">
      <details open id="box-uf"><summary>Estado</summary>
        <div id="f-ufs"></div>
      </details>
      <details open><summary>Tipo</summary>
        <div class="data-box"><div class="modo" id="f-tipo">
          <button type="button" data-t="Produto" aria-pressed="true">Só produtos</button>
          <button type="button" data-t="" aria-pressed="false">Tudo</button>
          <button type="button" data-t="Serviço" aria-pressed="false">Só serviços</button>
        </div></div>
      </details>
      <details open><summary>Portal</summary>
        <div id="f-portal"></div>
      </details>
      <details open><summary>Categoria</summary>
        <div id="f-cat"></div>
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
// ===== Mudança de endereço: o endereço antigo leva as Oportunidades e preferências para sigalicita.com.br =====
// (cada endereço tem a sua "gaveta" no navegador; assim nada se perde)
(function () {
  const NOVO = "sigalicita.com.br";
  if (location.hostname.endsWith(".onrender.com")) {
    let pacote = "";
    try {
      const d = {s: localStorage.getItem("busca_salvas") || "", p: localStorage.getItem("busca_pref") || ""};
      if (d.s || d.p) pacote = "#importar=" + encodeURIComponent(JSON.stringify(d));
    } catch (e) {}
    location.replace("https://" + NOVO + location.pathname + location.search + pacote);
    throw new Error("Indo para o endereço novo: " + NOVO);  // para aqui; a página nova carrega em seguida
  }
  if (location.hash.startsWith("#importar=")) {
    try {
      const d = JSON.parse(decodeURIComponent(location.hash.slice(10)));
      if (d.s) {
        const antigas = JSON.parse(d.s) || {}, atuais = JSON.parse(localStorage.getItem("busca_salvas") || "{}") || {};
        localStorage.setItem("busca_salvas", JSON.stringify(Object.assign(antigas, atuais)));  // junta as duas, sem perder nenhuma
      }
      if (d.p && !localStorage.getItem("busca_pref")) localStorage.setItem("busca_pref", d.p);
    } catch (e) {}
    history.replaceState(null, "", location.pathname + location.search);
  }
})();
// ===== Busca direto do navegador no PNCP =====
// O PNCP recusa a maioria dos pedidos que vêm do Render (muitos sites usam o mesmo endereço de lá).
// Por isso o próprio navegador da usuária busca no PNCP, com a internet dela. O servidor fica de reserva.
const CFG = __CONFIG_PNCP__;
const PNCP_URL = "https://pncp.gov.br/api/consulta/v1/contratacoes/proposta";
function semAc(s) { return String(s || "").normalize("NFKD").replace(/[^\x00-\x7f]/g, "").toLowerCase(); }
function escRe(s) { return s.replace(/[.*+?^${}()|[\]\\\/-]/g, "\\$&"); }
function padraoP(w) { w = w.trim(); const p = escRe(w); return w.length <= 6 ? p + "(?:s|es)?(?![a-z])" : p; }
const CAT_RE = CFG.categorias.map(([n, ws]) => [n, new RegExp("(?<![a-z])(?:" + ws.map(padraoP).join("|") + ")")]);
const SERV_RE = new RegExp("(?<![a-z])(?:" + CFG.servico.map(escRe).join("|") + ")(?![a-z])");
const PROD_RE = new RegExp("(?<![a-z])(?:" + CFG.produto.map(escRe).join("|") + ")");
const FORTE_RE = /(?<![a-z])(prestacao de servic|contratacao de (?:servic|empresa)|servicos? (?:de|para)|mao de obra|obras?|reforma|pavimenta|engenharia|locacao|terceiriza|vigilancia|consultoria|assessoria)/;
function categoriasDe(t) { t = semAc(t).replaceAll("mao de obra", "mao-de-servico"); const c = CAT_RE.filter(([, rx]) => rx.test(t)).map(([n]) => n); return c.length ? c : ["Outros"]; }
function tipoDe(t) { t = semAc(t); if (SERV_RE.test(t)) return PROD_RE.test(t) && !FORTE_RE.test(t) ? "Produto" : "Serviço"; return "Produto"; }
function portalDe(c) {
  const u = semAc(c.usuarioNome), links = ["linkSistemaOrigem", "linkProcessoEletronico"].map((k) => String(c[k] || "").toLowerCase()).join(" ");
  for (const [nome, nomes, doms] of CFG.portais) if (nomes.some((n) => u.includes(n)) || doms.some((d) => links.includes(d))) return nome;
  return String(c.usuarioNome || "Não informado").trim().slice(0, 60);
}
function linkCompras(c) {
  const uasg = String((c.unidadeOrgao || {}).codigoUnidade || "").replace(/\D/g, ""), num = String(c.numeroCompra || "").replace(/\D/g, "");
  const mod = {6: "05", 8: "06"}[c.modalidadeId];
  if (uasg.length !== 6 || !num || !mod || !c.anoCompra) return "";
  return "https://cnetmobile.estaleiro.serpro.gov.br/comprasnet-web/public/compras/acompanhamento-compra?compra=" + uasg + mod + String(parseInt(num, 10)).padStart(5, "0") + c.anoCompra;
}
function resumirP(c) {
  const org = c.orgaoEntidade || {}, und = c.unidadeOrgao || {}, controle = String(c.numeroControlePNCP || "");
  const m = controle.match(/^(\d{14})-\d+-(\d+)\/(\d{4})$/), portal = portalDe(c), obj = String(c.objetoCompra || "");
  let origem = c.linkSistemaOrigem || ""; if (origem && !String(origem).startsWith("http")) origem = "https://" + origem;
  const cats = categoriasDe(obj), sa = semAc(obj + " " + String(c.informacaoComplementar || ""));
  return {
    id: controle, numero: `${c.numeroCompra || ""}/${c.anoCompra || ""}`.replace(/^\/+|\/+$/g, ""),
    modalidade: c.modalidadeId, modalidadeNome: c.modalidadeNome || CFG.modalidades[c.modalidadeId] || "",
    disputa: c.modoDisputaNome || "", srp: !!c.srp, orgao: org.razaoSocial || "", cnpj: String(org.cnpj || "").replace(/\D/g, ""),
    unidade: und.nomeUnidade || "", uasg: String(und.codigoUnidade || ""), municipio: und.municipioNome || "", uf: und.ufSigla || "",
    objeto: obj.replace(/\s+/g, " ").trim(), valor: c.valorTotalEstimado ?? null,
    abertura: String(c.dataAberturaProposta || "").slice(0, 16), encerramento: String(c.dataEncerramentoProposta || "").slice(0, 16),
    publicacao: String(c.dataPublicacaoPncp || "").slice(0, 16), portal, categorias: cats, tipo: tipoDe(obj),
    assistencia: cats.includes("Eletrodomésticos") || CFG.assistencia.some((x) => sa.includes(x)),
    processo: String(c.processo || ""), amparo: (c.amparoLegal || {}).nome || "",
    esfera: {F: "Federal", E: "Estadual", M: "Municipal", D: "Distrital"}[org.esferaId] || "",
    poder: {E: "Executivo", L: "Legislativo", J: "Judiciário", N: "Não se aplica"}[org.poderId] || "",
    situacao: c.situacaoCompraNome || "",
    linkPncp: m ? `https://pncp.gov.br/app/editais/${m[1]}/${m[3]}/${parseInt(m[2], 10)}` : "",
    linkPortal: origem || (portal === "Compras.gov.br" ? linkCompras(c) : ""),
  };
}
// no máximo 4 pedidos ao mesmo tempo, para o PNCP não recusar
let VAGAS = 4; const ESPERA = [];
const PRIORIDADE = new Set();  // estados que ela marcou: passam na frente
async function vaga(prio) { if (VAGAS > 0) { VAGAS--; return; } await new Promise((ok) => prio ? ESPERA.unshift(ok) : ESPERA.push(ok)); }
function soltaVaga() { const prox = ESPERA.shift(); if (prox) prox(); else VAGAS++; }
const dormir = (ms) => new Promise((ok) => setTimeout(ok, ms));
let PAUSA_ATE = 0;  // quando o PNCP pede para esperar, todos os pedidos esperam juntos
function doisd(n) { return String(n).padStart(2, "0"); }
async function paginaP(uf, mod, pg) {
  const fim = new Date(); fim.setDate(fim.getDate() + 120);
  const url = `${PNCP_URL}?dataFinal=${fim.getFullYear()}${doisd(fim.getMonth() + 1)}${doisd(fim.getDate())}&codigoModalidadeContratacao=${mod}&uf=${uf}&pagina=${pg}&tamanhoPagina=50`;
  for (let t = 1; t <= 5; t++) {
    let r;
    if (PAUSA_ATE > Date.now()) await dormir(PAUSA_ATE - Date.now());
    await vaga(PRIORIDADE.has(uf));
    try { r = await fetch(url, {headers: {Accept: "application/json"}}); }
    catch (e) {
      // o PNCP às vezes recusa (limite de consultas) sem deixar o navegador ler a resposta: espera e tenta de novo
      soltaVaga(); if (t >= 4) throw new Error("sem conexão com o PNCP");
      PAUSA_ATE = Math.max(PAUSA_ATE, Date.now() + 4000 * t); continue;
    }
    try {
      if (r.status === 204) return {};
      if (r.ok) { const txt = await r.text(); return txt.trim() ? JSON.parse(txt) : {}; }
    } finally { soltaVaga(); }
    if ((r.status < 500 && r.status !== 429) || t === 5) throw new Error("PNCP respondeu " + r.status);
    if (r.status === 429) PAUSA_ATE = Math.max(PAUSA_ATE, Date.now() + 5000 * t);
    await dormir((r.status === 429 ? 5000 : 2000) * t);
  }
  return {};
}
const CACHE_P = {};
async function modalidadeP(uf, mod) {
  const k = uf + "|" + mod;
  if (CACHE_P[k] && Date.now() - CACHE_P[k].quando < 2 * 3600 * 1000) return CACHE_P[k].res;
  const p1 = await paginaP(uf, mod, 1);
  let itens = (p1.data || []).slice();
  const tot = p1.totalPaginas || 1, pags = Math.min(100, tot), outras = [];  // até 5.000 por modalidade em cada estado
  for (let pg = 2; pg <= pags; pg++) outras.push(paginaP(uf, mod, pg));
  const rs = await Promise.allSettled(outras);
  // páginas que o PNCP recusou: espera um pouco e tenta de novo, uma de cada vez
  let faltaram = 0;
  for (let i = 0; i < rs.length; i++) {
    if (rs[i].status === "fulfilled") { itens = itens.concat(rs[i].value.data || []); continue; }
    faltaram++;  // já tentou 5 vezes: mostra as que vieram e avisa que faltou um pedaço
  }
  const d = new Date(), agora = `${d.getFullYear()}-${doisd(d.getMonth() + 1)}-${doisd(d.getDate())}T${doisd(d.getHours())}:${doisd(d.getMinutes())}`;
  const lista = itens.map(resumirP).filter((x) => !(x.encerramento && x.encerramento < agora));
  const res = {itens: lista, cortado: tot > 100, faltaram};
  if (!faltaram) CACHE_P[k] = {quando: Date.now(), res};
  return res;
}
async function buscarEstadoDireto(uf, mods) {
  const itens = [], erros = [], cortadas = [], vistos = new Set();
  const rs = await Promise.allSettled(mods.map((m) => modalidadeP(uf, m)));
  rs.forEach((r, i) => {
    const nome = CFG.modalidades[mods[i]] || mods[i];
    if (r.status === "rejected") { erros.push(msgPncp(nome + ": " + r.reason.message)); return; }
    r.value.itens.forEach((x) => { if (!vistos.has(x.id)) { vistos.add(x.id); itens.push(x); } });
    if (r.value.faltaram) erros.push(`${nome}: faltou ${r.value.faltaram === 1 ? "1 página" : r.value.faltaram + " páginas"} (o PNCP pediu para esperar)`);
    if (r.value.cortado) cortadas.push(nome);
  });
  const d = new Date();
  return {uf, itens, erros, cortadas, atualizado: `${doisd(d.getDate())}/${doisd(d.getMonth() + 1)}/${d.getFullYear()} ${doisd(d.getHours())}:${doisd(d.getMinutes())}`};
}
// deixa a mensagem de erro do PNCP curta e sem códigos de página
function msgPncp(t) {
  t = String(t || "");
  const nome = (t.match(/^([^:]{2,40}):/) || [])[1];
  if (/429|Limite de requisi/i.test(t)) return (nome ? nome + ": " : "") + "o PNCP pediu para esperar (muitas consultas ao mesmo tempo)";
  return t.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim().slice(0, 160);
}
// tenta direto; se o navegador não conseguir falar com o PNCP, usa o servidor como antes
async function buscarEstado(uf, mods) {
  try {
    const d = await buscarEstadoDireto(uf, mods);
    if (d.itens.length || !d.erros.some((e) => e.includes("sem conexão"))) return d;
  } catch (e) {}
  const r = await fetch(`/api/busca?uf=${uf}&mods=${mods.join(",")}`), d = await r.json();
  if (!r.ok) throw new Error(msgPncp(d.erro || "Falha na busca."));
  d.erros = (d.erros || []).map(msgPncp);
  return d;
}
const $ = (s) => document.querySelector(s);
document.addEventListener("click", async (ev) => {
  const bt = ev.target.closest(".copiar");
  if (!bt) return;
  const texto = bt.dataset.t || "";
  try { await navigator.clipboard.writeText(texto); }
  catch (e) { const t = document.createElement("textarea"); t.value = texto; document.body.appendChild(t); t.select(); document.execCommand("copy"); t.remove(); }
  if (!bt.dataset.rot) bt.dataset.rot = bt.textContent;
  bt.textContent = "✓ Copiado"; bt.classList.add("ok");
  clearTimeout(bt._tm); bt._tm = setTimeout(() => { bt.textContent = bt.dataset.rot; bt.classList.remove("ok"); }, 1500);
});
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const brl = (v) => v == null ? "não informado" : Number(v) === 0 ? "Sigiloso" : "R$ " + Number(v).toLocaleString("pt-BR", {minimumFractionDigits: 2, maximumFractionDigits: 2});
const semAcento = (s) => String(s || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
// raiz da palavra: tira o plural/singular do fim, para "fraldas" achar "fralda" e "cartão" achar "cartões"
function raiz(w) {
  if (w.length < 4) return w;
  let m;
  if ((m = w.match(/^(.{3,})(?:oes|aes|aos|ao)$/))) return m[1];         // cartões, cartão -> cart
  if ((m = w.match(/^(.{3,})eis$/))) return m[1] + "e";                   // papéis -> pape (acha papel)
  if ((m = w.match(/^(.{3,}[aeiou])is$/))) return m[1];                   // hospitais -> hospita
  if ((m = w.match(/^(.{2,}[rzs])es$/))) return m[1];                   // colheres -> colher, luzes -> luz
  if ((m = w.match(/^(.{3,}[aeiou])l$/))) return m[1];                    // papel -> pape, hospital -> hospita
  if ((m = w.match(/^(.{3,})s$/))) return m[1];                           // fraldas -> fralda, geriátricas -> geriatrica
  return w;
}
const CONSULTA = "https://score.sigalicita.com.br/?cnpj=";
const MEUS = ["Compras.gov.br", "ComprasMG", "SISLOG", "Licitar Digital", "Portal de Compras Públicas", "Licitações-e"];
const UFS = [["AC","Acre"],["AL","Alagoas"],["AP","Amapá"],["AM","Amazonas"],["BA","Bahia"],["CE","Ceará"],["DF","Distrito Federal"],["ES","Espírito Santo"],["GO","Goiás"],["MA","Maranhão"],["MT","Mato Grosso"],["MS","Mato Grosso do Sul"],["MG","Minas Gerais"],["PA","Pará"],["PB","Paraíba"],["PR","Paraná"],["PE","Pernambuco"],["PI","Piauí"],["RJ","Rio de Janeiro"],["RN","Rio Grande do Norte"],["RS","Rio Grande do Sul"],["RO","Rondônia"],["RR","Roraima"],["SC","Santa Catarina"],["SP","São Paulo"],["SE","Sergipe"],["TO","Tocantins"]];
const MODS = [[6,"Pregão Eletrônico"],[8,"Dispensa"],[4,"Concorrência Eletrônica"],[7,"Pregão Presencial"],[5,"Concorrência Presencial"],[12,"Credenciamento"],[9,"Inexigibilidade"],[1,"Leilão Eletrônico"]];
const PRAZOS = [["", "Qualquer prazo"], ["2", "Encerra em até 2 dias"], ["7", "Encerra em até 7 dias"], ["30", "Encerra em até 30 dias"]];

let DADOS = null, ORDEM = "encerra", MOSTRAR = 40, PRAZO = "", DIA = "", MODO_DIA = "no";
let PREF = { uf: "DF", portais: ["Compras.gov.br"], mods: [6, 8, 4, 7, 5, 12], cats: [], tipo: "Produto", ufs: [] };
try { Object.assign(PREF, JSON.parse(localStorage.getItem("busca_pref") || "{}")); } catch (e) {}
if (!Array.isArray(PREF.cats)) PREF.cats = [];
if (typeof PREF.tipo !== "string") PREF.tipo = "Produto";
PREF.cats = PREF.cats.filter((k) => k !== "Serviços" && k !== "Construção e manutenção");
function salvar() { try { localStorage.setItem("busca_pref", JSON.stringify(PREF)); } catch (e) {} }

$("#uf").innerHTML = `<option value="BR">BRASIL — todos os estados</option>` + UFS.map(([s, n]) => `<option value="${s}">${s} — ${n}</option>`).join("");
$("#uf").value = "DF";  // abre sempre no DF; Brasil só se ela escolher
$("#f-prazo").innerHTML = PRAZOS.map(([v, t], i) => `<label class="opc"><input type="radio" name="prazo" value="${v}"${i === 0 ? " checked" : ""}><span>${t}</span></label>`).join("");

function diasAte(s) {
  if (!s) return null;
  const d = new Date(s.length === 16 ? s + ":00" : s);
  if (isNaN(d)) return null;
  return (d - new Date()) / 86400000;
}
function textoPrazo(dias) {
  if (dias == null) return "";
  // conta pelos dias do calendário: encerra amanhã às 03h é "amanhã", não "hoje"
  const fim = new Date(Date.now() + dias * 86400000), hoje = new Date();
  const d0 = new Date(hoje.getFullYear(), hoje.getMonth(), hoje.getDate());
  const d1 = new Date(fim.getFullYear(), fim.getMonth(), fim.getDate());
  const n = Math.round((d1 - d0) / 86400000);
  if (n <= 0) return "Encerra hoje";
  if (n === 1) return "Encerra amanhã";
  return `Faltam ${n} dias`;
}
function dataHora(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(s || "");
  return m ? `${m[3]}/${m[2]}/${m[1]} às ${m[4]}:${m[5]}` : "não informado";
}
function numero(v) { const n = parseFloat(String(v || "").replace(/\./g, "").replace(",", ".")); return isNaN(n) ? null : n; }

function contar(lista, campo) { const c = {}; lista.forEach((x) => { c[x[campo]] = (c[x[campo]] || 0) + 1; }); return c; }

function filtrar() {
  if (!DADOS) return [];
  const q = semAcento($("#q").value).trim().split(/\s+/).filter(Boolean).map(raiz);
  const nosItens = NOS_ITENS[chaveItens()] || null;
  const vmin = numero($("#vmin").value), vmax = numero($("#vmax").value);
  const lim = PRAZO ? parseFloat(PRAZO) : null;
  const hoje = agoraLocal().slice(0, 10);  // as que encerram hoje não dão mais tempo de participar
  return DADOS.itens.filter((x) =>
    (!x.encerramento || x.encerramento.slice(0, 10) > hoje) &&
    (!PREF.portais.length || PREF.portais.includes(x.portal)) &&
    (!PREF.tipo || (x.tipo || "Produto") === PREF.tipo) &&
    (DADOS.uf !== "BR" || !UFSEL.length || UFSEL.includes(x.uf)) &&
    (!PREF.cats.length || (x.categorias || []).some((c) => PREF.cats.includes(c))) &&
    (!q.length || q.every((p) => semAcento(x.objeto + " " + x.orgao + " " + x.unidade + " " + x.municipio).includes(p)) || (nosItens && nosItens.has(x.id))) &&
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

// portais grátis e pagos para o fornecedor (os que o site não conhece ficam sem etiqueta)
const CUSTO_PORTAL = {"Compras.gov.br": "gratis", "ComprasMG": "gratis", "SISLOG": "gratis", "Compras RJ": "gratis", "Banrisul (RS)": "gratis", "Licitações-e": "gratis",
  "Licitar Digital": "pago", "Portal de Compras Públicas": "pago", "BLL Compras": "pago", "Licitanet": "pago", "BNC Compras": "pago"};
function etiquetaCusto(p) {
  const c = CUSTO_PORTAL[p];
  return c ? `<span class="custo ${c}" title="${c === "pago" ? "Portal cobra do fornecedor para participar" : "Portal gratuito para o fornecedor"}">${c === "pago" ? "Pago" : "Grátis"}</span>` : "";
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
function desenharCats() {
  const c = {};
  if (DADOS) DADOS.itens.filter((x) => (!PREF.portais.length || PREF.portais.includes(x.portal)) && (!PREF.tipo || (x.tipo || "Produto") === PREF.tipo))
    .forEach((x) => (x.categorias || []).forEach((k) => { c[k] = (c[k] || 0) + 1; }));
  const nomes = Object.keys(c).sort((a, b) => (a === "Outros") - (b === "Outros") || c[b] - c[a]);
  PREF.cats.forEach((k) => { if (!nomes.includes(k)) nomes.push(k); });
  const todas = nomes.length && nomes.every((k) => PREF.cats.includes(k));
  $("#f-cat").innerHTML = (nomes.length ? `<label class="opc" style="border-bottom:1px solid var(--line);padding-bottom:7px;margin-bottom:3px"><input type="checkbox" data-todascat="1"${todas ? " checked" : ""}><span><b>Todas</b></span></label>` : "") + (nomes.length ? nomes.map((k) => `<label class="opc"><input type="checkbox" data-cat="${esc(k)}"${PREF.cats.includes(k) ? " checked" : ""}><span>${k === "Eletrodomésticos" ? "🔧 " : ""}${esc(k)}</span><span class="n">${c[k] || 0}</span></label>`).join("") : `<p class="loc">As categorias aparecem depois da busca.</p>`) +
    (PREF.cats.length ? `<button type="button" class="mini-bt" data-limpacat="1">Mostrar todas as categorias</button>` : "");
}
let UFSEL = [];
function desenharUfs() {
  const atual = $("#uf").value;
  const c = {}, st = (DADOS && DADOS.status) || {};
  if (DADOS && DADOS.uf === "BR") filtrar().forEach((x) => { c[x.uf] = (c[x.uf] || 0) + 1; });
  const falhos = UFS.map((u) => u[0]).filter((u) => st[u] === "erro");
  $("#f-ufs").innerHTML = `<div class="ufs">` +
    `<button type="button" class="ufc br" data-uf="BR" aria-pressed="${atual === "BR"}" title="Todos os estados">Brasil</button>` +
    UFS.map(([u, n]) => {
      const sel = atual === u, s = DADOS && DADOS.uf === "BR" ? st[u] : null;
      const cls = s === "erro" ? " uf-erro" : c[u] ? " uf-ok" : "";
      const dica = n + (s === "erro" ? ": o PNCP não respondeu" : c[u] ? `: ${c[u]} licitaç${c[u] === 1 ? "ão" : "ões"} com a sua busca` : s === "ok" ? ": nenhuma licitação com a sua busca" : DADOS && DADOS.uf === "BR" ? ": ainda buscando..." : "");
      return `<button type="button" class="ufc${cls}" data-uf="${u}" aria-pressed="${sel}" title="${esc(dica)}">${u}</button>`;
    }).join("") + `</div>` +
    `<div class="uf-info">${falhos.length ? `<b style="color:var(--bad)">${falhos.join(", ")}: o PNCP não respondeu.</b> Clique em Buscar para tentar de novo.` : "Clique num estado, ou em Brasil para todos."}</div>`;
}
function desenharMods() {
  const c = DADOS ? contar(DADOS.itens, "modalidade") : {};
  $("#f-mod").innerHTML = MODS.map(([id, nome]) => `<label class="opc"><input type="checkbox" data-mod="${id}"${PREF.mods.includes(id) ? " checked" : ""}><span>${nome}</span><span class="n">${DADOS && PREF.mods.includes(id) ? (c[id] || 0) : ""}</span></label>`).join("");
}

function cartao(x, emSalvas) {
  const dias = diasAte(x.encerramento), meu = MEUS.includes(x.portal);
  const encerrada = x.encerramento && x.encerramento < agoraLocal();
  const urg = dias != null && dias <= 2;
  return `<article class="card${meu ? " meu" : ""}">
    <div class="tags">
      ${x.numero || x.uasg ? `<span class="tag num">${esc(x.numero || "")}${x.numero && x.uasg ? " · " : ""}${x.uasg ? "UASG " + esc(x.uasg) : ""}</span>` : ""}
      <span class="tag mod">${esc(x.modalidadeNome)}</span>
      ${x.srp ? `<span class="tag srp">Registro de preços</span>` : ""}
      ${encerrada ? `<span class="tag encerrada">Encerrada</span>` : dias != null ? `<span class="tag prazo${urg ? " urg" : ""}">${textoPrazo(dias)}</span>` : ""}
      <span class="tag">📍 ${esc(x.uf)}</span>
      
      ${seloPortal(x.portal)}
      ${(x.categorias || []).filter((k) => k !== "Outros").map((k) => `<span class="tag cat">${esc(k)}</span>`).join("")}
      ${x.tipo === "Serviço" ? `<span class="tag">Serviço</span>` : ""}
      ${!emSalvas && achadaSoNosItens(x) ? `<span class="tag nos-itens" title="O produto que você procurou está na lista de itens desta licitação">🔎 nos itens</span>` : ""}
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
      ${x.linkPortal ? `<a class="pri" href="${esc(x.linkPortal)}" target="_blank" rel="noopener">${x.portal === "Compras.gov.br" ? "ComprasGov" : esc(x.portal.length > 28 ? "Portal" : x.portal)}</a>` : ""}
      ${x.linkPncp ? `<a href="${esc(x.linkPncp)}" target="_blank" rel="noopener">Ver no PNCP</a>` : ""}
      ${x.cnpj ? htmlNota(x.cnpj) : ""}
      ${emSalvas ? `<button type="button" class="remover" data-salvar="${esc(x.id)}">🗑 Remover</button>`
        : SALVAS[x.id] ? `<span class="ja-salva" title="Esta licitação está nas suas Oportunidades">⭐ Salva</span>` : ""}
    </div>
    ${ABERTOS[x.id] ? `<div class="det">${painelDet(DET[x.id], x)}</div>` : ""}
  </article>`;
}

const ABERTOS = {}, DET = {};
// ===== Oportunidades: licitações salvas (ficam guardadas neste navegador) =====
let SALVAS = {}, VER_SALVAS = false;
try { SALVAS = JSON.parse(localStorage.getItem("busca_salvas") || "{}") || {}; } catch (e) { SALVAS = {}; }
function agoraLocal() { const d = new Date(), z = (n) => String(n).padStart(2, "0"); return `${d.getFullYear()}-${z(d.getMonth() + 1)}-${z(d.getDate())}T${z(d.getHours())}:${z(d.getMinutes())}`; }
function guardarSalvas() {
  try { localStorage.setItem("busca_salvas", JSON.stringify(SALVAS)); } catch (e) {}
  $("#n-salvas").textContent = Object.keys(SALVAS).length;
}
function alternarSalva(id) {
  if (SALVAS[id]) delete SALVAS[id];
  else {
    const x = DADOS && DADOS.itens.find((i) => i.id === id);
    if (!x) return;
    SALVAS[id] = Object.assign({}, x, {salvaEm: Date.now()});
  }
  guardarSalvas(); desenhar();
}
function botaoSalvarDet(id) {
  return SALVAS[id]
    ? `<button type="button" class="salvar on" data-salvar-det="1" title="Clique para tirar das Oportunidades">⭐ Salva nas Oportunidades${(SALVAS[id].meusItens || []).length ? ` (${SALVAS[id].meusItens.length} ${SALVAS[id].meusItens.length === 1 ? "item" : "itens"})` : ""}</button>`
    : `<button type="button" class="salvar" data-salvar-det="1" title="Guardar em Oportunidades para analisar depois">☆ Salvar</button>`;
}
// salvou em outra aba (na página da licitação): a busca atualiza o número e os botões sozinha
window.addEventListener("storage", (e) => {
  if (e.key !== "busca_salvas") return;
  try { SALVAS = JSON.parse(e.newValue || "{}") || {}; } catch (er) { SALVAS = {}; }
  $("#n-salvas").textContent = Object.keys(SALVAS).length;
  if (!document.querySelector("#pg")) desenhar();
});
function botaoSalvarTodas(lista) {
  if (!lista.length) return "";
  const faltam = lista.filter((x) => !SALVAS[x.id]).length;
  if (!faltam) return `<span class="salvar-todas feito" title="Todas desta busca já estão nas Oportunidades">⭐ Todas salvas</span>`;
  return `<button type="button" class="salvar-todas" data-salvar-todas="1" title="Guardar todas desta busca em Oportunidades">⭐ Salvar todas (${faltam.toLocaleString("pt-BR")})</button>`;
}
function salvarTodas() {
  const novas = filtrar().filter((x) => !SALVAS[x.id]);
  if (!novas.length) return;
  if (novas.length > 30 && !confirm(`Salvar as ${novas.length.toLocaleString("pt-BR")} licitações desta busca nas Oportunidades?`)) return;
  const agora = Date.now();
  novas.forEach((x) => { SALVAS[x.id] = Object.assign({}, x, {salvaEm: agora}); });
  guardarSalvas(); desenhar();
}
// ===== procurar a palavra também dentro dos itens (busca do PNCP) =====
const NOS_ITENS = {}; let ESPERA_ITENS = 0, ITENS_BUSCANDO = "";
function chaveItens() { return semAcento($("#q").value).trim().replace(/\s+/g, " "); }
async function buscarNosItens() {
  const k = chaveItens();
  if (k.length < 3 || NOS_ITENS[k] || ITENS_BUSCANDO === k) return;
  ITENS_BUSCANDO = k; desenhar();
  const ids = new Set();
  try {
    for (let pg = 1; pg <= 4; pg++) {  // até 2.000 licitações abertas com a palavra
      const url = "https://pncp.gov.br/api/search/?" + new URLSearchParams({q: $("#q").value.trim(), tipos_documento: "edital", status: "recebendo_proposta", tam_pagina: 500, pagina: pg});
      const r = await fetch(url, {headers: {Accept: "application/json"}});
      if (!r.ok) break;
      const d = await r.json(), it = d.items || [];
      it.forEach((x) => { if (x.numero_controle_pncp) ids.add(x.numero_controle_pncp); });
      if (it.length < 500 || ids.size >= (d.total || 0)) break;
    }
    NOS_ITENS[k] = ids;
  } catch (e) {}
  if (ITENS_BUSCANDO === k) ITENS_BUSCANDO = "";
  if (chaveItens() === k) desenhar();
}
function achadaSoNosItens(x) {
  const q = semAcento($("#q").value).trim().split(/\s+/).filter(Boolean).map(raiz), ni = NOS_ITENS[chaveItens()];
  return q.length && ni && ni.has(x.id) && !q.every((p) => semAcento(x.objeto + " " + x.orgao + " " + x.unidade + " " + x.municipio).includes(p));
}
function desenharSalvas() {
  // as que ainda estão abertas primeiro (a disputa mais próxima em cima); as encerradas vão para o fim
  const agora = agoraLocal(), fechou = (x) => !!(x.encerramento && x.encerramento < agora);
  const lista = Object.values(SALVAS).sort((a, b) => fechou(a) - fechou(b) || (a.encerramento || "9").localeCompare(b.encerramento || "9"));
  let h = `<div class="cab"><span><b>${lista.length}</b> oportunidade${lista.length === 1 ? " salva" : "s salvas"} para analisar</span><button type="button" class="mini-bt" id="voltar">← Voltar para a busca</button>${lista.length ? `<button type="button" class="remover-todas" id="remover-todas" title="Tirar todas das Oportunidades">🗑 Remover todas</button>` : ""}</div>`;
  if (!lista.length) h += `<div class="vazio"><b>Nenhuma oportunidade salva ainda</b>Na busca, clique em ☆ Salvar na licitação que quer analisar depois.</div>`;
  h += lista.map((x) => cartao(x, true)).join("");
  $("#res").innerHTML = h;
}
function seloPortal(p) {
  if (p === "Compras.gov.br") return `<span class="selo-cg" title="Licitação do Compras.gov.br"><i><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M3 4h2l2.5 11h11L21 7H6.5"/><circle cx="9" cy="20" r="1.4"/><circle cx="18" cy="20" r="1.4"/></svg></i><span>Compras<em>.gov.br</em></span></span>`;
  const CARRINHO = `<i><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M3 4h2l2.5 11h11L21 7H6.5"/><circle cx="9" cy="20" r="1.4"/><circle cx="18" cy="20" r="1.4"/></svg></i>`;
  if (p === "ComprasMG") return `<span class="selo-cg selo-cmg" title="Licitação do ComprasMG">${CARRINHO}<span>Compras<em>MG</em></span></span>`;
  if (p === "Licitar Digital") return `<span class="selo-cg selo-cld" title="Licitação do Licitar Digital">${CARRINHO}<span>Licitar Digital</span></span>`;
  if (p === "Portal de Compras Públicas") return `<span class="selo-cg selo-cpp" title="Licitação do Portal de Compras Públicas">${CARRINHO}<span>Portal de Compras Públicas</span></span>`;
  if (p === "Licitações-e") return `<span class="selo-cg selo-cbb" title="Licitação do Licitações-e (Banco do Brasil)">${CARRINHO}<span>Licitações-e</span></span>`;
  if (p === "SISLOG") return `<span class="selo-cg selo-csl" title="Licitação do SISLOG">${CARRINHO}<span>SISLOG</span></span>`;
  const cor = {"ComprasMG": "selo-mg", "SISLOG": "selo-go", "Licitar Digital": "selo-ld"}[p];
  if (cor) return `<span class="selo-p ${cor}" title="Licitação do ${esc(p)}">${esc(p)}</span>`;
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
      d.itens.map((i) => `<tr><td>${esc(i.n)}</td><td class="d">${esc(i.descricao)}${i.tipo ? `<span class="tipo">${esc(i.tipo)}</span>` : ""}<button type="button" class="copiar" data-t="${esc(i.descricao)}" title="Copiar a descrição deste item">📋 Copiar</button>${i.me && !/sem benef|nao se aplica|não se aplica/i.test(i.me) ? `<small>${esc(i.me)}</small>` : ""}</td><td>${qtd(i.qtd)}</td><td>${esc(i.un)}</td><td>${i.vu != null ? brl(i.vu) : "—"}</td><td>${i.vt != null ? brl(i.vt) : "—"}</td></tr>`).join("") +
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
  desenharPortais(); desenharCats(); desenharMods(); desenharUfs();
  $("#aba-op").setAttribute("aria-pressed", VER_SALVAS);
  if (VER_SALVAS) return desenharSalvas();
  if (!DADOS) return;
  const lista = filtrar(), nome = DADOS.uf === "BR" ? "todo o Brasil" : (UFS.find((u) => u[0] === DADOS.uf) || ["", DADOS.uf])[1];
  let h = `<div class="cab"><span><b>${lista.length.toLocaleString("pt-BR")}</b> licitaç${lista.length === 1 ? "ão encontrada" : "ões encontradas"} em <b>${esc(nome)}</b> com proposta em aberto${DIA ? ` · disputa ${MODO_DIA === "no" ? "no dia" : "a partir de"} <b>${DIA.split("-").reverse().join("/")}</b>` : ""}</span>${botaoSalvarTodas(lista)}<span class="at">atualizado ${esc(DADOS.atualizado)}</span></div>`;
  if (DADOS.guardada) h += `<div class="carregando" style="padding:12px 16px"><div class="spin"></div>Mostrando a lista de ${esc(DADOS.atualizado)}. Atualizando com o PNCP…</div>`;
  if (DADOS.progresso) h += `<div class="carregando" style="padding:12px 16px"><div class="spin"></div>Carregando os estados: ${DADOS.progresso.feitos} de ${DADOS.progresso.total}. As licitações vão aparecendo enquanto isso.</div>`;
  if (DADOS.erros.length) h += (DADOS.uf === "BR" || DADOS.uf === "MEUS")
    ? `<div class="aviso">O PNCP não respondeu para: <b>${esc(DADOS.erros.join(", "))}</b>. Clique em Buscar de novo daqui a alguns minutos (os estados que já vieram ficam guardados e voltam rápido).</div>`
    : `<div class="aviso">O PNCP não respondeu para: ${esc(DADOS.erros.join("; "))}. Tente buscar de novo em alguns minutos.</div>`;
  if (ITENS_BUSCANDO && ITENS_BUSCANDO === chaveItens()) h += `<p class="loc" style="margin:0 0 10px">🔎 Procurando "${esc($("#q").value.trim())}" também dentro dos itens das licitações…</p>`;
  if (DADOS.cortadas.length) h += `<div class="aviso">Muitas licitações em ${esc(DADOS.cortadas.join(", "))}: mostrando as 5.000 primeiras de cada uma. Use a palavra-chave para achar o que procura.</div>`;
  if (!lista.length) h += `<div class="vazio"><b>Nenhuma licitação com esses filtros</b>Tente marcar mais portais ou modalidades, ou mudar a palavra-chave.</div>`;
  h += lista.slice(0, MOSTRAR).map((x) => cartao(x)).join("");
  if (lista.length > MOSTRAR) h += `<button type="button" class="mais" id="mais">Mostrar mais (${(lista.length - MOSTRAR).toLocaleString("pt-BR")} restantes)</button>`;
  $("#res").innerHTML = h;
  pedirNotas();
}

let pedido = 0;
async function buscarVarios(meu, lista, novo, modo) {
  if (novo) { DADOS = {uf: modo, itens: [], erros: [], cortadas: [], atualizado: "", status: {}, progresso: null, vistos: new Set()}; UFSEL = []; }
  if (!DADOS.progresso) DADOS.progresso = {feitos: 0, total: 0};
  DADOS.progresso.total += lista.length;
  if (!DADOS.carregando) DADOS.carregando = new Set();
  lista.forEach((u) => DADOS.carregando.add(u));
  lista.forEach((u) => { delete DADOS.status[u]; DADOS.erros = DADOS.erros.filter((x) => x !== u); });
  const vistos = DADOS.vistos, meuDados = DADOS;
  desenhar();
  let ultimoDesenho = 0;
  async function umEstado(uf) {
    try {
      const d = await buscarEstado(uf, PREF.mods);
      if (meu !== pedido) return true;
      d.itens.forEach((x) => { if (!vistos.has(x.id)) { vistos.add(x.id); DADOS.itens.push(x); } });
      d.cortadas.forEach((c) => { if (!DADOS.cortadas.includes(uf + " " + c)) DADOS.cortadas.push(uf + " " + c); });
      DADOS.atualizado = d.atualizado;
      if (d.erros.length && !d.itens.length) return false;  // PNCP não respondeu nada: tenta de novo depois
      return true;
    } catch (e) { return false; }
  }
  async function processa(uf, ultima, falhas, fila) {
    const ok = await umEstado(uf);
    if (meu !== pedido || DADOS !== meuDados) return false;
    DADOS.carregando.delete(uf);
    if (ok) { DADOS.status[uf] = "ok"; DADOS.progresso.feitos++; }
    else if (ultima) { DADOS.status[uf] = "erro"; DADOS.progresso.feitos++; DADOS.erros.push(uf); }
    else falhas.push(uf);
    if (PRIORIDADE.has(uf) || Date.now() - ultimoDesenho > 1500 || !fila.length) { ultimoDesenho = Date.now(); desenhar(); }
    return true;
  }
  async function rodada(fila, nos, ultima) {
    const falhas = [], extras = [];
    // estado marcado por ela que ainda está esperando na fila: sai da fila e começa já
    if (!ultima) DADOS.furarFila = (uf) => {
      const i = fila.indexOf(uf); if (i < 0) return;
      fila.splice(i, 1); extras.push(processa(uf, false, falhas, fila));
    };
    async function trabalhador() {
      while (fila.length) {
        if (!(await processa(fila.shift(), ultima, falhas, fila))) return;
      }
    }
    await Promise.all(Array.from({length: nos}, trabalhador));
    while (extras.length) await extras.shift();
    if (!ultima) DADOS.furarFila = null;
    return falhas;
  }
  let falhas = await rodada(lista.slice(), 3, false);  // 3 estados por vez (no máximo 4 pedidos ao PNCP ao mesmo tempo)
  for (let t = 0; t < 2 && falhas.length && meu === pedido && DADOS === meuDados; t++) {
    await new Promise((ok) => setTimeout(ok, 4000));  // respira e tenta de novo, um por vez
    falhas = await rodada(falhas, 1, t === 1);
  }
  if (meu !== pedido || DADOS !== meuDados) return;
  if (DADOS.progresso && DADOS.progresso.feitos >= DADOS.progresso.total) DADOS.progresso = null;
  desenhar();
}
async function buscar() {
  const uf = $("#uf").value, meu = ++pedido;
  if (uf === "BR" || uf === "MEUS") {
    PREF.uf = uf; salvar(); MOSTRAR = 40; $("#bt").disabled = true;
    try { await buscarVarios(meu, UFS.map((u) => u[0]), true, uf); } finally { if (meu === pedido) $("#bt").disabled = false; }
    return;
  }
  PREF.uf = uf; salvar(); MOSTRAR = 40; desenharUfs();
  $("#bt").disabled = true;
  // mostra na hora a última lista guardada deste estado (até 24 h) e atualiza por trás
  const chave = "busca_ultima_" + uf + "_" + PREF.mods.slice().sort().join(",");
  let velha = null;
  try { velha = JSON.parse(localStorage.getItem(chave) || "null"); } catch (e) {}
  if (velha && Date.now() - velha.quando < 24 * 3600 * 1000 && velha.d && velha.d.uf === uf) {
    DADOS = Object.assign(velha.d, {guardada: true}); desenhar();
  } else {
    $("#res").innerHTML = `<div class="carregando"><div class="spin"></div>Buscando licitações abertas de ${esc(uf)} no PNCP. Leva alguns segundos.</div>`;
  }
  try {
    const d = await buscarEstado(uf, PREF.mods);
    if (meu !== pedido) return;
    if (DADOS && DADOS.guardada && !d.itens.length && d.erros.length) { DADOS.guardada = false; DADOS.erros = d.erros; desenhar(); return; }
    DADOS = d; desenhar();
    if (!d.erros.length) { try { localStorage.setItem(chave, JSON.stringify({quando: Date.now(), d})); } catch (e) {} }
  } catch (e) {
    if (meu !== pedido) return;
    if (DADOS && DADOS.guardada) { DADOS.guardada = false; DADOS.erros = [e.message]; desenhar(); }
    else $("#res").innerHTML = `<div class="erro"><b>Não deu para buscar.</b> ${esc(e.message)}</div>`;
  } finally { if (meu === pedido) $("#bt").disabled = false; }
}

$("#f").addEventListener("submit", (e) => {
  e.preventDefault();
  VER_SALVAS = false;
  if (!DADOS || DADOS.uf !== $("#uf").value) return buscar();
  // Brasil / meus estados: "Buscar" de novo tenta só os estados que falharam
  if ((DADOS.uf === "BR" || DADOS.uf === "MEUS") && !DADOS.progresso && DADOS.erros.length) {
    const falhos = DADOS.erros.filter((u) => /^[A-Z]{2}$/.test(u));
    if (falhos.length) { buscarVarios(pedido, falhos, false); return; }
  }
  // um estado só: se faltou alguma parte (PNCP pediu para esperar), "Buscar" tenta de novo
  // (as modalidades que vieram certas ficam guardadas por 2 h, então só busca o que faltou)
  if (DADOS.uf !== "BR" && DADOS.uf !== "MEUS" && !DADOS.guardada && DADOS.erros && DADOS.erros.length) return buscar();
  MOSTRAR = 40; desenhar();
});
$("#uf").addEventListener("change", () => { VER_SALVAS = false; buscar(); });
$("#tema").addEventListener("click", () => {
  const claro = document.documentElement.getAttribute("data-theme") !== "light";
  if (claro) document.documentElement.setAttribute("data-theme", "light"); else document.documentElement.removeAttribute("data-theme");
  try { localStorage.setItem("siga_tema", claro ? "claro" : "escuro"); } catch (e) {}
});
$("#aba-op").addEventListener("click", () => { VER_SALVAS = !VER_SALVAS; window.scrollTo(0, 0); desenhar(); });
guardarSalvas();
function mostraLimpa() { $("#limpa").hidden = !$("#q").value; }
$("#limpa").addEventListener("click", () => { $("#q").value = ""; $("#q").dispatchEvent(new Event("input")); $("#q").focus(); });
$("#q").addEventListener("input", mostraLimpa);
setTimeout(mostraLimpa, 0);
$("#q").addEventListener("input", () => { MOSTRAR = 40; if (DADOS) desenhar(); clearTimeout(ESPERA_ITENS); ESPERA_ITENS = setTimeout(buscarNosItens, 700); });
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
$("#f-ufs").addEventListener("click", (e) => {
  const bt = e.target.closest("button[data-uf]"); if (!bt) return;
  VER_SALVAS = false;
  if ($("#uf").value === bt.dataset.uf && DADOS) { desenhar(); return; }
  $("#uf").value = bt.dataset.uf; buscar();
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
document.querySelectorAll("#f-tipo button").forEach((x) => x.setAttribute("aria-pressed", x.dataset.t === PREF.tipo));
$("#f-tipo").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  PREF.tipo = b.dataset.t; salvar(); MOSTRAR = 40;
  document.querySelectorAll("#f-tipo button").forEach((x) => x.setAttribute("aria-pressed", x === b)); desenhar();
});
$("#f-cat").addEventListener("change", (e) => {
  if (e.target.dataset.todascat) {
    PREF.cats = e.target.checked ? [...document.querySelectorAll("#f-cat [data-cat]")].map((x) => x.dataset.cat) : [];
    salvar(); MOSTRAR = 40; desenhar(); return;
  }
  const k = e.target.dataset.cat; if (k == null) return;
  PREF.cats = e.target.checked ? [...new Set(PREF.cats.concat(k))] : PREF.cats.filter((x) => x !== k);
  salvar(); MOSTRAR = 40; desenhar();
});
$("#f-cat").addEventListener("click", (e) => {
  if (!e.target.dataset.limpacat) return;
  PREF.cats = []; salvar(); MOSTRAR = 40; desenhar();
});
$("#f-mod").addEventListener("change", (e) => {
  const m = +e.target.dataset.mod; if (!m) return;
  PREF.mods = e.target.checked ? [...new Set(PREF.mods.concat(m))] : PREF.mods.filter((x) => x !== m);
  if (!PREF.mods.length) { PREF.mods = [m]; e.target.checked = true; }
  salvar(); buscar();
});
$("#res").addEventListener("click", (e) => {
  if (e.target.id === "mais") { MOSTRAR += 40; desenhar(); return; }
  const bs = e.target.closest("[data-salvar]");
  if (bs) { alternarSalva(bs.dataset.salvar); return; }
  if (e.target.closest("[data-salvar-todas]")) { salvarTodas(); return; }
  if (e.target.id === "voltar") { VER_SALVAS = false; desenhar(); return; }
  if (e.target.id === "remover-todas") {
    const n = Object.keys(SALVAS).length;
    if (!confirm(`Remover todas as ${n} oportunidades salvas? Isso não pode ser desfeito.`)) return;
    SALVAS = {}; guardarSalvas(); desenhar(); return;
  }
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
        ${x.numero || x.uasg ? `<span class="tag num">${esc(x.numero || "")}${x.numero && x.uasg ? " · " : ""}${x.uasg ? "UASG " + esc(x.uasg) : ""}</span>` : ""}
        <span class="tag mod">${esc(x.modalidadeNome)}</span>
        ${x.srp ? `<span class="tag srp">Registro de preços</span>` : ""}
        ${dias != null && dias >= 0 ? `<span class="tag prazo${dias <= 2 ? " urg" : ""}">${textoPrazo(dias)}</span>` : ""}
        <span class="tag">📍 ${esc(x.uf)}</span>
        
        ${seloPortal(x.portal)}
        ${(x.categorias || []).filter((k) => k !== "Outros").map((k) => `<span class="tag cat">${esc(k)}</span>`).join("")}
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
      ${x.linkPortal ? `<a class="pri" href="${esc(x.linkPortal)}" target="_blank" rel="noopener">${x.portal === "Compras.gov.br" ? "ComprasGov" : esc(x.portal.length > 28 ? "Portal" : x.portal)}</a>` : ""}
      ${x.linkPncp ? `<a href="${esc(x.linkPncp)}" target="_blank" rel="noopener">Ver no PNCP (oficial)</a>` : ""}
      ${x.cnpj ? htmlNota(x.cnpj) : ""}
      <button type="button" data-cal="${esc(x.id)}">📅 Salvar no calendário</button>
      <button type="button" data-copiar="${esc(location.href)}">🔗 Copiar link</button>
      ${botaoSalvarDet(x.id)}
    </div>
    ${""}<section class="secao" id="sec-docs"><h3>Documentos do edital</h3><div class="carregando" style="padding:12px"><div class="spin"></div>Buscando documentos e itens no PNCP…</div></section>
    <section class="secao" id="sec-itens" hidden></section>
    <section class="secao"><h3>Objeto completo</h3><p class="txt">${esc(x.objeto)}</p>
      ${x.complemento && x.complemento !== x.objeto ? `<h3>Informação complementar</h3><p class="txt">${esc(x.complemento)}</p>` : ""}</section>
    <section class="secao"><h3>Dados da contratação</h3>${blocoDados(x).replace(/^[\s\S]*?<div><h4>Dados da contratação<\/h4>/, "<div>")}</section>`;
  pedirNotas();
  alvo.addEventListener("click", (e) => {
    const bc = e.target.closest("[data-copiar]"), bk = e.target.closest("[data-cal]"), bs = e.target.closest("[data-salvar-det]");
    if (bk) salvarCalendario(x);
    if (bs) {
      if (SALVAS[x.id]) {
        delete SALVAS[x.id];
        alvo.querySelectorAll(".marcar-item:checked").forEach((c) => { c.checked = false; c.closest("tr").classList.remove("escolhido"); });
        const si = alvo.querySelector("#sec-itens"); if (si && si.redesenhar) si.redesenhar(false);
      } else { const y = Object.assign({}, x, {salvaEm: Date.now()}); delete y.complemento; SALVAS[x.id] = y; }
      guardarSalvas();
      bs.outerHTML = botaoSalvarDet(x.id);
    }
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
    const sec = alvo.querySelector("#sec-itens");
    sec.hidden = false;
    sec.redesenhar = (so) => {
      const escolhidos = new Set(((SALVAS[x.id] || {}).meusItens || []).map((m) => String(m.n)));
      const lista = so ? d.itens.filter((i) => escolhidos.has(String(i.n))) : d.itens, tot = lista.reduce((s, i) => s + (i.vt || 0), 0);
      sec.innerHTML = `<h3>${so ? `Meus itens (${lista.length} de ${d.itens.length})` : `Itens da contratação (${d.itens.length})`}</h3><div class="tw"><table><thead><tr><th>Item</th><th style="text-align:left">Descrição</th><th>Un.</th><th>Qtd</th><th>Valor unit.</th><th>Valor total</th></tr></thead><tbody>` +
        lista.map((i) => `<tr${!so && escolhidos.has(String(i.n)) ? ' class="escolhido"' : ""}><td>${esc(i.n)}${so ? "" : `<input type="checkbox" class="marcar-item" data-item="${esc(i.n)}"${escolhidos.has(String(i.n)) ? " checked" : ""} title="Marque os itens em que você vai participar" aria-label="Participar do item ${esc(i.n)}">`}</td><td class="d">${esc(i.descricao)}${i.tipo ? `<span class="tipo">${esc(i.tipo)}</span>` : ""}<button type="button" class="copiar" data-t="${esc(i.descricao)}" title="Copiar a descrição deste item">📋 Copiar</button>${i.me && !/sem benef|nao se aplica|não se aplica/i.test(i.me) ? `<small>${esc(i.me)}</small>` : ""}</td><td>${esc(i.un)}</td><td>${qtd(i.qtd)}</td><td>${i.vu != null ? brl(i.vu) : "—"}</td><td>${i.vt != null ? brl(i.vt) : "—"}</td></tr>`).join("") +
        `</tbody>${tot ? `<tfoot><tr><td colspan="5">${so ? "Total dos meus itens" : "Total estimado dos itens"}</td><td>${brl(tot)}</td></tr></tfoot>` : ""}</table></div>`;
    };
    sec.redesenhar(!!((SALVAS[x.id] || {}).meusItens || []).length);
    sec.addEventListener("change", (e) => {
      if (!e.target.matches(".marcar-item")) return;
      e.target.closest("tr").classList.toggle("escolhido", e.target.checked);
      const marcados = [...sec.querySelectorAll(".marcar-item:checked")].map((c) => String(c.dataset.item));
      if (!SALVAS[x.id]) { const y = Object.assign({}, x, {salvaEm: Date.now()}); delete y.complemento; SALVAS[x.id] = y; }
      SALVAS[x.id].meusItens = d.itens.filter((i) => marcados.includes(String(i.n)))
        .map((i) => ({n: i.n, descricao: i.descricao, un: i.un, qtd: i.qtd, vu: i.vu, vt: i.vt}));
      guardarSalvas();
      const b = alvo.querySelector("[data-salvar-det]"); if (b) b.outerHTML = botaoSalvarDet(x.id);
    });
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
PAGINA = PAGINA.replace("__CONFIG_PNCP__", json.dumps({
    "categorias": CATEGORIAS, "servico": SERVICO, "produto": PRODUTO, "assistencia": ASSISTENCIA,
    "portais": [[n, list(a), list(b)] for n, a, b in PORTAIS_CONHECIDOS], "modalidades": MODALIDADES,
}, ensure_ascii=False))


# ================================================================ senha de acesso
# Usuário e senha ficam no Render (Environment → SITE_USUARIO e SITE_SENHA), iguais nos dois serviços
# (busca-licitacoes e consulta-orgaos).
# Entrando uma vez em sigalicita.com.br, o navegador fica liberado também em score.sigalicita.com.br (por 30 dias).
# Sem SITE_SENHA configurada, o site continua aberto como antes.
import hashlib
import hmac

COOKIE_ACESSO = "sl_acesso"
ABERTOS_SEM_SENHA = ("/entrar", "/sair", "/tarefa/email", "/logo-email.png")


def _senha_site():
    return os.environ.get("SITE_SENHA", "").strip()


def _usuario_site():
    return os.environ.get("SITE_USUARIO", "").strip().lower()


LOGO_ENTRAR = "data:image/png;base64,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"


def _token_site():
    chave = (_usuario_site() + "\n" + _senha_site()).encode("utf-8")
    return hmac.new(chave, b"sigalicita-acesso-v1", hashlib.sha256).hexdigest()


def _host(handler):
    return (handler.headers.get("Host") or "").split(":")[0].lower()


def _logado(handler):
    if not _senha_site():
        return True
    for parte in (handler.headers.get("Cookie") or "").split(";"):
        nome, _, valor = parte.strip().partition("=")
        if nome == COOKIE_ACESSO and hmac.compare_digest(valor, _token_site()):
            return True
    return False


def _cookie_acesso(handler, valor, segundos):
    host = _host(handler)
    extra = "; Domain=.sigalicita.com.br" if host.endswith("sigalicita.com.br") else ""
    if host.endswith("sigalicita.com.br") or host.endswith(".onrender.com"):
        extra += "; Secure"
    return f"{COOKIE_ACESSO}={valor}; Path=/; Max-Age={segundos}; HttpOnly; SameSite=Lax{extra}"


def _volta_segura(v):
    v = str(v or "/")
    return v if v.startswith("/") and not v.startswith("//") and "\n" not in v and "\r" not in v else "/"


def _pagina_entrar(volta="/", erro=False):
    from html import escape as e
    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>SigaLicita · Entrar</title>
<link rel="icon" type="image/png" sizes="64x64" href="data:image/png;base64,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">
<link rel="apple-touch-icon" href="data:image/png;base64,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">
<meta name="robots" content="noindex">
<style>
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#0d1422;color:#e8eef8;font:16px system-ui,-apple-system,"Segoe UI",sans-serif;padding:16px;box-sizing:border-box}}
.box{{width:100%;max-width:360px;background:#151f32;border:1px solid #26344f;border-radius:18px;padding:28px 24px;box-shadow:0 10px 30px rgba(0,0,0,.35)}}
.logo{{display:block;width:100%;max-width:260px;height:auto;margin:0 auto 14px}}
p{{margin:0 0 20px;color:#94a3bb;font-size:14px;text-align:center}}
label{{display:block;font-size:13px;font-weight:600;color:#94a3bb;margin-bottom:6px}}
input{{width:100%;box-sizing:border-box;font:inherit;padding:12px 14px;border-radius:10px;border:1px solid #31415f;background:#0d1422;color:#e8eef8}}
input:focus{{outline:2px solid #38bdf8;border-color:#38bdf8}}
input:-webkit-autofill,input:-webkit-autofill:hover,input:-webkit-autofill:focus{{-webkit-box-shadow:0 0 0 1000px #0d1422 inset!important;box-shadow:0 0 0 1000px #0d1422 inset!important;-webkit-text-fill-color:#e8eef8!important;caret-color:#e8eef8;transition:background-color 9999s ease-out 0s}}
html{{color-scheme:dark}}
.sw{{position:relative}} .sw input{{padding-right:48px}}
.olho{{position:absolute;right:6px;top:50%;transform:translateY(-50%);width:38px!important;height:38px;margin:0!important;padding:0!important;border-radius:8px!important;background:transparent!important;font-size:18px;line-height:1;cursor:pointer}}
.olho:hover{{background:#26344f!important}}
.olho{{color:#c9d5ea;display:grid;place-items:center}}.olho:hover{{color:#fff}}.olho .f,.olho.on .a{{display:none}}.olho.on .f{{display:block}}
button{{width:100%;margin-top:14px;font:inherit;font-weight:800;padding:12px;border:0;border-radius:10px;background:#38bdf8;color:#06243a;cursor:pointer}}
.erro{{background:#371a1c;border:1px solid #7a2f2f;color:#f6766f;border-radius:10px;padding:10px 12px;font-size:14px;margin-bottom:14px}}
</style></head><body><form class="box" method="post" action="/entrar">
<img class="logo" src="{LOGO_ENTRAR}" alt="SigaLicita"><p>Área restrita. Entre com o seu e-mail e senha.</p>
{'<div class="erro">E-mail ou senha incorretos. Tente de novo.</div>' if erro else ''}
<label for="u">E-mail</label><input id="u" name="usuario" type="email" inputmode="email" autocomplete="username" autocapitalize="none" placeholder="seu@email.com" autofocus required>
<label for="s" style="margin-top:12px">Senha</label>
<div class="sw"><input id="s" name="senha" type="password" autocomplete="current-password" required>
<button type="button" class="olho" id="olho" aria-label="Mostrar senha" title="Mostrar senha" onclick="var s=document.getElementById('s'),v=s.type==='password';s.type=v?'text':'password';this.setAttribute('aria-label',v?'Esconder senha':'Mostrar senha');this.title=this.getAttribute('aria-label');this.classList.toggle('on',v);"><svg class="a" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg><svg class="f" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.6 5.1A10 10 0 0 1 12 5c6.5 0 10 7 10 7a17 17 0 0 1-2.6 3.5M6.6 6.6C3.7 8.4 2 12 2 12s3.5 7 10 7a9.6 9.6 0 0 0 5.4-1.6M9.9 9.9a3 3 0 0 0 4.2 4.2"/><path d="M2 2l20 20"/></svg></button></div>
<input type="hidden" name="volta" value="{e(_volta_segura(volta))}">
<button type="submit">Entrar</button></form></body></html>"""


def _barrar_sem_senha(handler, caminho):
    """Devolve True se respondeu (pediu a senha); False se pode seguir."""
    if caminho in ABERTOS_SEM_SENHA or _logado(handler):
        return False
    if caminho.startswith("/api/"):
        corpo = b'{"erro": "acesso restrito: entre com a senha"}'
        handler.send_response(401)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
    else:
        corpo = _pagina_entrar(handler.path).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(corpo)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(corpo)
    return True


def _redirecionar(handler, destino, cookie=None):
    handler.send_response(303)
    handler.send_header("Location", destino)
    if cookie:
        handler.send_header("Set-Cookie", cookie)
    handler.send_header("Content-Length", "0")
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()


def _tratar_entrar_sair(handler):
    """POST /entrar (confere a senha) e GET /sair. Devolve True se tratou."""
    caminho = urllib.parse.urlparse(handler.path).path
    if handler.command == "GET" and caminho == "/sair":
        _redirecionar(handler, "/", _cookie_acesso(handler, "", 0))
        return True
    if handler.command == "GET" and caminho == "/entrar":
        _redirecionar(handler, "/")
        return True
    if handler.command == "POST" and caminho == "/entrar":
        tam = min(int(handler.headers.get("Content-Length") or 0), 4000)
        dados = urllib.parse.parse_qs(handler.rfile.read(tam).decode("utf-8", "replace"))
        senha = (dados.get("senha") or [""])[0]
        usuario = (dados.get("usuario") or [""])[0].strip().lower()
        volta = _volta_segura((dados.get("volta") or ["/"])[0])
        if _senha_site() and hmac.compare_digest(senha.strip(), _senha_site()) and (not _usuario_site() or hmac.compare_digest(usuario, _usuario_site())):
            _redirecionar(handler, volta, _cookie_acesso(handler, _token_site(), 30 * 86400))
        else:
            time.sleep(1.5)  # deixa tentativa por tentativa mais lenta
            corpo = _pagina_entrar(volta, erro=True).encode("utf-8")
            handler.send_response(200)
            handler.send_header("Content-Type", "text/html; charset=utf-8")
            handler.send_header("Content-Length", str(len(corpo)))
            handler.send_header("Cache-Control", "no-store")
            handler.end_headers()
            handler.wfile.write(corpo)
        return True
    return False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("  " + (fmt % args) + "\n")

    def _send(self, code, body, ctype):
        data = body.encode("utf-8")
        zip_ok = len(data) > 2000 and "gzip" in (self.headers.get("Accept-Encoding") or "")
        if zip_ok:
            import gzip
            data = gzip.compress(data, 6)
        self.send_response(code)
        if zip_ok:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

    def do_POST(self):
        if not _tratar_entrar_sair(self):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        # endereço antigo (.onrender.com): leva para o endereço novo (o e-mail diário continua no antigo)
        if _host(self).endswith(".onrender.com") and u.path in ("/", "/index.html", "/licitacao"):
            return _redirecionar(self, "https://sigalicita.com.br" + self.path)
        if _tratar_entrar_sair(self) or _barrar_sem_senha(self, u.path):
            return
        if u.path == "/logo-email.png":
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo_email.png"), "rb") as f:
                dados = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(dados)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(dados)
            return
        if u.path == "/diagnostico":
            try:
                txt = diagnostico()
            except Exception as e:
                txt = "ERRO: " + erro_txt(e)
            return self._send(200, txt, "text/plain; charset=utf-8")
        if u.path == "/tarefa/email":
            q = urllib.parse.parse_qs(u.query)
            segredo = os.environ.get("ALERTA_CHAVE", "")
            if not segredo or q.get("chave", [""])[0] != segredo:
                return self._send(403, "chave errada ou ALERTA_CHAVE não configurada", "text/plain; charset=utf-8")
            if q.get("ver"):
                return self._send(200, "último envio: " + (_envio["ultimo"] or "nenhum desde que o site ligou")
                                  + (" · enviando agora..." if _envio["rodando"] else ""), "text/plain; charset=utf-8")
            # só um e-mail por dia: o cron-job chama algumas vezes (caso o site esteja dormindo); &forcar=1 manda de novo
            if _envio["dia"] == time.strftime("%Y-%m-%d") and not q.get("forcar"):
                return self._send(200, "ok: o e-mail de hoje já foi enviado (" + _envio["ultimo"] + "). Para mandar de novo, coloque &forcar=1 no fim do link.", "text/plain; charset=utf-8")
            if not _envio["rodando"]:
                threading.Thread(target=tarefa_email, daemon=True).start()
            return self._send(200, "ok: o resumo está sendo preparado e chega no e-mail em 1 ou 2 minutos", "text/plain; charset=utf-8")
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
        if u.path == "/diagnostico3":
            return self._send(200, diagnostico_velocidade(), "text/plain; charset=utf-8")
        if u.path == "/diagnostico2":
            try:
                txt = diagnostico_busca()
            except Exception as e:
                txt = "ERRO: " + erro_txt(e)
            return self._send(200, txt, "text/plain; charset=utf-8")
        if u.path in ("/", "/index.html", "/licitacao"):
            pagina = PAGINA
            if _senha_site():
                from html import escape
                nome = os.environ.get("SITE_NOME", "").strip() or "Rute Silva"
                pagina = pagina.replace("<!--CONTA-->", f'<div class="conta" title="Você está conectada">👤 <b>{escape(nome)}</b>'
                                        '<a href="/sair" title="Sair do SigaLicita (e do Score)">Sair</a></div>', 1)
            return self._send(200, pagina, "text/html; charset=utf-8")
        self._send(404, "não encontrado", "text/plain; charset=utf-8")


def main():
    na_nuvem = "PORT" in os.environ
    srv = ThreadingHTTPServer(("0.0.0.0" if na_nuvem else "127.0.0.1", PORT), Handler)
    print(f"Busca Licitações rodando na porta {PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
