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


SERVICO = ["prestacao de servico", "prestacao de servicos", "servicos de", "servico de", "servicos continuos", "mao de obra",
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
        if _PROD_RE.search(t) and not re.search(r"(?<![a-z])(prestacao de servic|servicos? de|mao de obra|obras?|reforma|pavimenta|engenharia|locacao|terceiriza|vigilancia|consultoria|assessoria)", t):
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
                + (' · <b style="color:#b3261e">🔧 Exige assistência técnica</b>' if x.get("assistencia") else "") + '</div>'
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
            f'<h1 style="font-size:20px;margin:0;color:#0c326f">Busca Licitações · resumo do dia</h1>'
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

    cats = _lista_env("ALERTA_CATEGORIAS")
    so_produto = os.environ.get("ALERTA_TIPO", "produto").lower().startswith("prod")

    def passa(x):
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
    corpo = json.dumps({"from": "Busca Licitações <onboarding@resend.dev>", "to": [para],
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
        assunto = f"Busca Licitações: {len(novas)} nova{'s' if len(novas) != 1 else ''} · {len(urgentes)} encerrando em 2 dias ({', '.join(ufs)})"
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
.tag.mod{color:#9cc8ff;border-color:#2b4a74;background:#12243d}
.tag.srp{color:#c9a7ff;border-color:#4b3a74;background:#211a3d}
.tag.prazo{color:var(--warn);border-color:#6e5317;background:var(--warn-bg)}
.tag.prazo.urg{color:var(--bad);border-color:#7a2f2f;background:var(--bad-bg)}
.tag.portal{color:var(--muted)}
.tag.portal.meu{color:var(--gold);border-color:#7a5a12;background:var(--gold-soft)}
.tag.cat{color:#a8e3c8;border-color:#285a45;background:#11291f}
.tag.assist{color:#ffb4a8;border-color:#7a2f2f;background:var(--bad-bg);font-weight:700}
.selo-cg{display:inline-flex;align-items:center;gap:6px;font:800 12px var(--font);padding:3px 10px 3px 4px;border-radius:7px;background:#1351b4;color:#fff;border:1px solid #2a6bd1;white-space:nowrap;letter-spacing:.01em}
.selo-cg i{display:inline-grid;place-items:center;width:18px;height:18px;border-radius:5px;background:#ffcd07;color:#0c326f;font-style:normal}
.selo-cg i svg{display:block}
.selo-cg em{font-style:normal;color:#ffcd07}
.selo-p{display:inline-flex;align-items:center;font:800 12px var(--font);padding:3px 10px;border-radius:7px;color:#fff;white-space:nowrap;letter-spacing:.01em}
.selo-mg{background:#c8102e;border:1px solid #e23a52}
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
.acoes a.pri{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.acoes a.pag{color:var(--gold);border-color:#7a5a12}
.acoes button.salvar{color:var(--ink)}
.acoes button.salvar.on{background:var(--gold);border-color:var(--gold);color:#241a00}
.acoes button.remover{color:var(--bad);border-color:#7a2f2f}
.aba-op{margin-left:auto;align-self:center;display:inline-flex;align-items:center;gap:8px;font:700 14px var(--font);padding:9px 16px;border-radius:999px;border:1px solid #7a5a12;background:var(--gold-soft);color:var(--gold);cursor:pointer}
.aba-op b{display:inline-grid;place-items:center;min-width:22px;height:22px;padding:0 6px;border-radius:999px;background:var(--gold);color:#241a00;font-size:12px}
.aba-op[aria-pressed="true"]{background:var(--gold);color:#241a00}
.aba-op[aria-pressed="true"] b{background:#241a00;color:var(--gold)}
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
    <img class="marca" src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAoMAAADICAYAAACTbusIAABR6ElEQVR42u2dd3gUxRvHv3Mll0vvvZBQQu+9CIpS7IqIDVEBBTs2ighYUVEQsIH+EBWUIkWK0kXpvZcACSG9XHruklzZ+f1xlNRLcrt3uSTv53nyiLe7U97Z2f3uOzPvMNgQl8DODzCgF8CDAAQALMD8XwQwQA2CIAiCIAgCAMCBYgCZ5j9+/b8snQOHdBkn/7RVvkzS1Dw7eLuqZPcwsAcAPgyMuVHTEgRBEARBiFWKvAhgWziwXltq+gv5Z3IdSgy6+HfqwhhmM8aGUmsRBEEQBEHYWhvyLVwwvafTnD1ar2JQ5d+2hZI5zebgIxhjjJqGIAiCIAjCboKQM8b+MJj49FLNqUt2FYPO3m0jFEqn6Rx4ljEoqDkIgiAIgiDqSxTCyMCXCILsfZ3mRKrNxaCLX6d7ZTK2HAweZH6CIAiCIAhHUYUoEBierutiE3mdhGBApxmMYRFjzJksThAEQRAE4UAwqMD5KKVbsNGgTd9T+8tqRZjaNdD3JwY2iixNEARBEATh2HDOl2szFc8Dx3SixaCLX5tgJlP9zRg6kWkJgiAIgiAajCA8wQXZvTXNI5RZTiZMzWSqv0gIEgRBEARBNCwYY12YTNgEhKmtFoNugX4/M4bOZE6CIAiCIIiGKQhdA31/snROtQtIXP07TWGMvUpmJAiCIAiCaMCCEKy90iWw1KDL2Fv18Spw8+88kjO+gjEmIxMSBEEQBEE0bDjnAufCvbqsM3/XLAZ9Wni4Kd2uAPAn0xEEQRAEQTQaQZihNShikHssv+zvlTx/rgrXaSQECYIgCIIgGheMsUBXpXFqpd/L/o+zV5dIuUqIZWAqMhlBEARBEETjgoOXgJuaaTPPZtz4rZxnUO7E3ychWI8NxLmo403ZbpzXzj5B3hwdIjnZlSAIgmiSMDBnBvn75X+7jptPl7ZcwU8zVrct6ghpRQ1jzOrjTVM0M9RkkuZBHPf2AIZ14eja3Pzb2IUMm44ysitBEATRBN+jMIGZumgzzpwBAMXNAwphLmOMhCDRKARg5yiOu7txDOsCxIRVPp6aSzYlCIIgmiaMQc65bA6AYbfEoE8LDzDcTuYhHEoAggP8xo1bs+euf1uO4V04hnfjCPWt/vzdZ4HjceQJJAiCIJqyIsTt8O7midxj+QoAcJO53A0wJ7IMUa/ir8LcPcaYxd2znRQcgzoA93TjuKszh687u3mHX08QZhciv/5nniI7Zy0JQYIgCKKpa0Hm5CY3DC8CVigAgMvZg/R6JOpbADJYFn8A4O7McVdnYHg3AYM7MbiqUF4A3hSB5W/5Gz/svwAcLeMVpPmCBEEQRJN9B8tkw3BdDDLG2XDQ+5CoBwEI1DwE7OfBcXdXjru7Af3bAkpFBfFnTvXWbzfSY5WPfbmBbnSCIAiCuM5dAJhCHdixFwAPsgdhcwHIGFgt5wCG+5lXAN/djaNHi6q8d7yMzmOw7FI0Hzt6Bdh7nryCBEEQBGF+FyNEHdixl0IGWW8yB2EbAWiWYbys+GPXh4OroF2EeQXw3d042oaz8mKubGJAFd4/S4Uwn/T5OmoTgiAIgiiLDLLeCgDNyBT2R+XbDmrvGKg8m0Hu5AEmd4KxNB8ZRz6r9ppXHmuF5hH+KDEw5OoUuJIi4GJ8Ns5fOO84AhAc4KxWeo0xjl4tgeHdOIZ3AyL9y4i/66ndVIDsppKs82cPAJy5Bvx7VlZGI5JXkCAIgiDAeZAC4EGgCYM2Q+7sC4/IIVD7xkCpDoDMyRUymbLKc/XadItp9Q+Lxe3tY6s8llAQjIS8QMRle+Fkghy79l2AJivdTvfRLSlY0wpgpYJjQFvz8O+wroC/R2U5ecsTWGF1sBXS9Ma1n66he5EgCIIgKsOCFAwsiAwhHTJnP3hGDIaLf0c4uYWAKVR2kdrNPNLQzCMNgyIAdAH4g0BCQQhOpIXj0BUl/jscj/hr0orDGwtBWA2izVXFMbgTx91dgcGdODxcWAXJxq9fWWbunyReu1tewR2nyCtIEARBEJXelAzNFBxoRq9FsQrQCX7txsAtuBfkShdHaVxEeaYiyjMVD7cGcC9wOTcMK4+3wK9/nkRefoHEQrA8vu4cQ7uYVwDf1pZD5VRRMPIKkk3qu/CWV/CLdawqjUgQBEEQBA0Ti0Md0AW+rR6Fk0dEg/A0tfROxvTByXhroBO2xXfH0l0m7DtwQoTgLF/nUF+Oe7qZYwD2agnI5VUJQGZXz1xsCrDlRHkRS15BgiAIgrj5Ng9SMDAVGaIuNpPDr/1YuIX0hlzh3CCr4KzQ4/5Wh3F/K+DKY2FYcbwllq49icLC/DrY4dY/mwdxfPuCgM7RVQ0X8zK/sSpFpA0+c27m9dmaiuUhIUgQBEEQZV6LngqyQi2Rq+HfYSzcQnpBxuSNplotvJIx/Y5kPN/LB0uPD8biFcdRWJRbF72FPjEoIwQtqEY7cyEZ2HyM5goSBEEQhCVIDNaATOkJ/87Pw8W/E2RM1mjrGeCag3cGbMfzPd2w4sxALPz9PDSarFpdu+xfht4xHCP7VaEU7U2ZVcjzNtD9SxAEQRAkBq2EKTwQ0PkFuAR0hqwJeZO8VEWY0P0fjO7kjCUn7sDcn09BV5Rd43Wv/MDg7MRxXw+gXodir2d9NQPYcJhV0IjkFSQIgiCIisjIBOXhHPBt+wyaDfkWboFdmpQQLIursgSv9NyBw3NMGPfknYCs/HcDYwy8nN0Ynv+GYeORepfxAIAv1pvLRBAEQRAEicHaC6CgXoga9j94RQ1p1EPCdcHfJQcfD9mG/fMCMGxQ10qyi5eRhIKDCMLkbGDNgYp7EFNbEgRBEASJwWpQuAQh7LbPEdj1NcgVajJIFTT3SsbPzx7Fxtkt0aF1+K0D/FbMQUcRhHPWVfAKkhAkCIIgCBKD1eEWMgARA+dA5R5G3qNa0DMkFmvf1GDEPb3MOuu60epfEJrzT84GVu+r4BUkNUgQBEEQJAYrSQcOBHR5HQGdJ4LJ5HQn1AEPJy2+ffQAFrzTHQqF3KIg3HrcXu1pLsNXGwCTQHEFCYIgCILEoAXkKm9EDl4I95Ce5A0Uwah2h7Hj8xC0iAqpVhA+u5AhMcvWJTHPCczIA377j5X9GSi3zIUgCIIgiCYvBlXeMYi8Yz6Ual9qfQlo43sNSydmQ+2sqiQI1U4cv07iiPC3sRS87hWc9ycr5xXkoCDTBEEQBFETTSrOoFtIP/OwMK0UlozzScDIz0tQXHJrqznOOXzcBKx+B2gfaesSmAVfRh6w7N9yPxMEQRAEQWLwFh5Rw+HX5imH9BQZS3Kh12ZAX5Rs8bwjVwCVEgj3A8L967/cey8AT81lKNaXt2mLYGDl2+Zy2lwKcgbGgAWbAIOJvIIEQRAEUVeYW2DnRu9D8Wn9OLyi7613cWAy6FCadwUl+VehL0iGQZsKQ1EquFB6XdhYFjBljzs7cUQFAC2DgZhQoEMzjh4tOXzc7FPHVXuB135kECoEdu7WnGPFWxweLvYohXnbu+xCoOskhhJD+WFqEoMEQRAEUTON3jPo3+EFuIffVm/CoCD5PxRnnUFpXhwMunTJ0i3RM1xIBi4kAzgC3Fg1G+bH0TUK6NuG45G+HO5q6ev9yR8M8zdWTve+HhzfvMChUtrHtje8ggs34aYQvPmVQ0KQIAiCIGpFo/YMBnZ7A25B3e2eb0lePAqT/kVR6n4IRm0dxE3tPYO1Qe3EcW8P4InbBPRtLY04mvAdw7qDldOaOJxj5ijBfiLMvNkweQUJgiAIQiSN1jPo12G83YVgQdJ/yE/4G/qCaw5hg2I9w+p9wOp9ckQHcoy9k+PJgRxqVd2FUoEOeHIuw+HLFT1wHJ+O5nhmMGDPmH43Qkl/+3dFryCjcEEEQRAEUQcapWfQM/o++LZ+zC7eIZOxGAXXdiI/YQtMJTniBI7EnsGq8HHjeHYwx9i7OHzda5dWcjYw8jOG+Izy56uUHD+9yjG4o71b2DxXMF8LdJ7EoCtlktmHIAiCIEgMNnBcgnoiqOtrNhcFnHPkX9uB3EurIRiKJEvT1mLwBmonjtfvB14azqG04B8+nwSM+JQhp6h8vl6uHCvf5ugcZf82vj5CjE//AOZtlNnEPgRBEARBYrABovKOQWif92weR7A49wqyzvwPhkJph4PtKQZvEOnP8fFTHHd1rnzsnzPAswsqh45p5s+xajJHpIThbbIKgGuZQPcWNVoJ1XkFAZorSBAEQRB1pdHMGZQpPRDSe5pNhaCxNB/ZF35DUcoecQpc7gwX/05QeUZDrvIw/zl5QjDpkXbwg2qv+36igFYhDJoCILMAyMoHLqcBfx9lyNNZJ4KuZTE8NY9hcEeOT0YLaBZgTmfZbuCtpezm7h436NTM7BH0dpPOrgkZwIjPGLIKgOVvcAxoa9F6AIAftt0SgjePkBAkCIIgiLrrksbiGYy4fQGULraLcqzTnEHGsa8gGIutU90u/nAJ6ArXgK5Q+7YBk1XW4XptOpJ2v1FtGiveFHB7FfPzBIHjaBzDthPAtpNAbIp1gthJwTF/HMfpawzf/V1ZWA3twrH4RQ5nJ+nseuQy8NQ8hjytOT+VklcvCK+PD2tLzF7BAh15BQmCIAhCLI3CM+jb7hmbCUHOOXIvrUHOlXVgddjjjINB7dUS6sAucAvoBiePMJvVXyZj6NkS6NkSmP4okJJtwl9HGbadYjgQCxiMtRNJeiPDhO+qFlVj7uD47Gku6UrdLceBcV8DeiNupltqYHhyLrDqbY7eMbesaVZ75v8s3oabQvDmVw0JQYIgCIJommLQyaMZPCPvsknaJoMOGce+QnH22ToFTXEJ7A7fNk/AyTWoXmwS6sswfigwfihHZj7H7D8Yft9Teci3KiqLKo73H+eYMEzaMi7aCsz4jeFGKJiy8yFLDQyPfQH89gZH3zZlVCCAEj3Hoi1lF42QECQIgiAIMcgacuE5ZAjpPd0mYsCgy0Ly3mkozj5bB2EaidC+HyC4+xv1JgQrEuDJMG8s8N8nHH1i6jYjwEnB8fNr0gpBzoF3fmaY8ZsMFeMS3hjuBcwxEp+Yy7D/Yvky/7idIVfLyt0FBEEQBEE0UTEY0msK5ErpN8EtyYtH8r7pMOoya3W+3NkXAZ1eRFj/T+Ds3cIhbdUqBFg/jeP3NwXEhAo1nu/lyrFuKsewrtKVodQAjJnP8POuqkUgY6ySIHxyrgwHYq+3i57j2zJzGSmUDEEQBEGIp8EOEzv7dYSLX3vJ09VlnkT6sXnggqHGc5ncGV7N74dX1N2QKZwahN3u6AgMbMex/D8BH65ilebeAUCYL8eayRzNAqXLN7fIvIPJsbia5/rxm/uLALpShie+BMYPAS4kM2QXkvgjCIIgCBKDAII6vyx5mtqME0g/OqdW5ypdQxDcYzKUrv4NznZyOYOMAYVVLIzu1Izjtzc5/Dykyy9JYw4dcy2zvJBzV3PMHh+ElxdqAG68KQ455+ZogtdP15UyzN9YhWAkryBBEARBiKZBDhN7txgBucpN0jRL8uKQcfyrWp3r7NsWof0+aJBCkHOOmb8Bb/4kq7Sg5I4OHBvelVYInrwKDJ1VWQgGe3Nsmckxslsqxj8xqNwxs8jj5YaMCYIgCIIgMXhdKSjh3fIBSZPUFyYj7dDsWg0Nu4X2R0ivaTaZq2hr9EZg9DwZvt9audnH3GGO7ydlDMGdp4H7P648tNsugmPbLI4Wweb/f2vgAfj7+1cShLe8hGZhWPaPgbyCBEEQBNEkxWBg11erDNhsLcbibKQe/AiCUWfxPM4B75hRCOz8os23u7MFeVrggY8Ztp+qLKKmjxTw+RgOmYTVWrrTPEew1FA+vwFtOTZN5wjwuvWbl6oQ74+tepNjxhgY2E1xeOOPIAiCIIgmKAYVzn5wDZRueatg0iP18Kcw6QssnsdkSgR1ex0+LR5okI18LRO4aybD8fjyIkoh5/jhJQGv3CtdXpxzvL+CYfIvlYehHx9g3srORVX5ugdiDqNt61bUIwmCIAiCxGD1BHZ9VVKvUPa5X2AoSqnxvKCeU+EW3LNBNvCpq2YhmJhVefHG6nc47pewWqUGYPw3snLhX24wZYSAr8ZxyKu54xQyAW+O9KQeSRAEQRB2psGsJpY7+0Hl1Vyy9LTpR1GQtKvG8wI6vwQX39YNsnG3nQTGfV15qDbYm+OPybfm7ElBgQ547IvKoWMUco4F4zhG9K05jWHRRxARHYPE+FjqmQRBEARBYrA8/h3HS+YVNOiykHHy2xrP84y6G+6h/Rpkwy7aCsz8vfIWdO0iOFa8WX7OnliSNMCoOQxx6eXzcnXmWDaJo28ttbRCJmDSI/6Y9DmJwYbG2KcfwW39eiAyIhThYUHwcHdDcmoGEpNScSXuGr5ZvBzxCUlkKMKm9Gxhwsh+AiL9OcL9OAK8ODLzZUjWAAmZDKv3yXD4irxJ2eTVewxoH8kR4QeE+3EwGUdSlgzXsoCz1xgWbFbSjUOAuQV2dvj4HUyhRtSQHyRbuJG8byZK8y5bPEft1x7BPafYdbGIXpuOpN1vVHt8xZsCbu9oOQ3OgTd/Ylj+b2XhPKAtxy+vVz1nz1rOXgMenVN5xXCwN8eKtzhah9UtvVKjEn1nBiI5OZV6ZwOgfdtWmD/nXfTsZvnGLNLq8MX8/2Huwp8oZBAhOR5qjumPGvBYfwEyWfVOA0Hg+G2PHB+tVKCotHEvRBvU3oTZo00I9+M1fMwzvPWTHPsuyulGasI0iDmDfu2ekUyUFabsq1EIKt1CENTtDbuvGjaVWl7Ioim03KlL9MDoeVULQUuLN6xl52ng3o8qC8GYUHPomLoKQQBQKQx4+ZFm1DMbAPcMHYQDu1bWKAQBwM3VBbOmvYKNq78nwxHSvh/cBez4QI8nbuMWhSAAyGQMTw00n+/t1ng/SibdZ8SyScYahSBg9haufNuIl+820s3UhJE7uQXNcvRCBnR+GTKZ+K8WwViM9KNzwE2l1T8sFK4I6/s+5CoPm9fLWFqAotQDyLm0BpmnF6MgcYfFofDNRxm+2ijDwVigqBgI8gLc1eZj2YXmXT4OxFa+fvLDAt5/ApBJ+CH823/AC98yGEyVQ8esfofD2936tCO987BoGyCYTNRDHZTAAD+s/f1ruLio63Rds4hQMDDs2X+UjEhIwi+TDGhTxw9PDxegbbiAtQcbnzesZwsT5o011nlaVe8YAf+dkyEtl0J3NUUcfpjYNbgPgrq+IklamvPLkH/1LwvWkCO0zww4e7e0aZ3yr25FYeoBlOZdKvc755a3WKvqeLsIjoFtOdYdYpU6cV0Wb9SFT/5gmL+xcjmDvTmOza1+xXBdGL9iMDb8/Q/1UAflrzU/YEC/7lZdazAYcffD43DwyCkyJCGKl+42YuoI6z8aZ/6uwP92NB5B6KHm+OejUgR6WSfokjQMd81QNvohdKIyDj9M7BV9jyTpGEtykZ+wpca8bCUEORdQkPQfEna9Cs35nysJQWs5l8jw7ZbKX3OuzmYPnZRC0GAEJn5ftRDknCMtl+F0gjR5PdC9iHqng9KmdXOrhSAAKJUKjHvmUTIkIZoxt4sbPXjmjsY1NPpAT5PVQhAwDxkP7ybQjdUEcejVxJwDKs9mkqSVG7cJ4NXf5Eyhhnfz+21Sj6KMo8iJXQVDYbJd7Gbt4g2LdSgGnppX9TA0gJsey682MPz8unhn8+2Rp+Du7onCwnzqpQ5G65bRotNoFhlKhiREoVJyhPiISyPCj0PGOATeODxhUYHin71RQbTAi8Sgg+EZOUSSRRwmgxaFiTssnuPd/EHIJN5vWDDpkXnqe2jTDtpAKPNyIuwGMaEcf7wjbeiY1BxzDMHYlJofmFtOAFfSIDqGoauyBKPu6Y0fV/xHvbQKmkdFICQkAH4+3ggOCkCAvw845yjS6lBQWISCgiJkZGbj2ImzKCzSSpq3FEIuIiyEGpEQdx8GiBctcjlDhD9HQmbjEIPh/uLTiPQjzyCJQQfDI/J2SdLJv7oFXDBU/0Bw8oBn1FBJy27QZSH96JfQFybaxDZVzS3sE2OO6+emli6fi8nAI58zZOXX9mHJMH8TsHC8+Af1vR0z8eMK6qQAEN0sHCMfGobBt/dFn56d63RtfEISLsTG4dCRU/hx6WrR4jAtPUt0fTTZudSoDZDRA40I87Oub/+wTQ5NoXQzk5I0EnywCxwZeY1nflymBHXJKpDRjU5i0LFwcg8XnQbnAvKvbbN4jnfMo5DJnSQrt05zDhnH50Mw2G/e28N9zItFlBK26H/ngGcWMGhLKscQtLTibO0B4MMnAC9Xcfl3DboMZ2cXlJTommwHnfb2BDx4z51o09r63Xeim4Ujulk47hk6CG+/Pg6r123Bt4uXI/byVavSO3LsjOh6nTlHgcUbIiP6mtC9hXXXrjsoh6ZQurLoSmVIzAIiRHjD4jMYivWNRwyeviZBGgk1n9MiSMDujw1WpX8lDRg0XUWdycFw2E8AtX9nSYaIizVnIeirfwIp1H7wCB8kWbmLc2KRfuRzUUKQc/MwsPnv1r+r4437BXw3QVohuGa/eVeRikKwTwzHf59w9GxZfXmMJoYNh8WXQSU3YGDfLk2yY4558iHEntiKqW++IEoIVsTdzRXPjR6BQ7tXY85H78DZue4P5biriaI9e6fOkBgkxHMuSdw74lxi41o1e/Kq+Hfm8XjyDJIYdCDcwm6TJJ3C5L0Wj/u0fkKy4NLG4mykH5trcUi6avFXXuwxZh4GNv/d+jfAyp0rYxzzxgqYPEJa23+xjuHFRbJKk6rv62FeoezhAnz4JK9BTErzkO3XvmltldSzW0f8t3U5vv5yBkKCA2yWj1wux4Rxj+PArlW4rX+POl//7eLlVuedlJyG31ZtoKcvIZoFm+QwmqwbttYbgIWbFY3KHrEpMuw+a/2zd8cphoRMEoMkBh0ItZd4b4hg0kObfqj6yivd4BbcS5LyCiY90o5+YdELWVEAgpu9gGYByGoMEnpDGAKAWmleMfzEbdLZ3CSYQ8fMWV+5HK/cI+DHl295HztHAZ2jqn8IH7zEkJojvkw9wlKaTGd86P67sG3DEnTp1NZuebaIjsCGld/hqcfqtpJ+zvz/YdvOvXXOr6SkFKPHv428/EIQhFjOXJPhg1XWCbqpy+S4mNL4hM/E75RIsmJa79UM4MVFCrqpSAw6Fgq1r+g0tOlHLHrpXAO71TlKe3VknvwO+oLaTdi44dnj4OUEXm0J8AI2vccxsL109i4qNg8Lrz3AKghQs/dxehVh4YZ0sfxFvmqfeNu284uHs8q50XfEu+7ohx8WfgS53P4BcOVyOb7+cgbGPv1Ina4b++I0/Lx8Xa3PvxKfiKfGvYVjJ87Rk5eQjCU7FJi1Qo7cWs7MyS0C3l2mwMq9jVP4FJYwPD5XiV2na//83XaS4Ym5SuhKyStIYtCBcPJqKcnQrTbzuMXjLgFdpRFS6UcteiArcmsIuO5iqXmQed/f9pHS2TszD7j7Q4Y958uXR+3EsfyN6r2PQzpZTnf7CfFlU8kNGDygbaPuhH17dcGvP86BSuVUb2WQy+WY++lUDBpQe095Xn4hXn7zA/S4bQT+3LwTBYVVv43PXbiMN6bORpe+D2Drjr0gCKn5cbsCfd5xwtebZUjSVP1cTdYAX2+Woc87Tvj5H3mjtkdCpgxPz3fCyM+VOHCRodRQ+cO91MCx/wLDI58p8dxCJyRpSAg2ZRzy08gjfKAk6ZRkX7CgyORwCegkOg/OBeReWi06HXdnjts7AHd14WgTCgR4cfi4MeTpgMw8jrg0hvhMYOydHO5q6SY9X0kDHvms8lZ2vu4cq962LDo7NAMCvaoPzXAyASjWA2qRGmdwJzk272icHZAxhsULP4Kri7reyyKTyfDtvJnoNWhkncLPXLwUj6fGvgXAvDtJj64dEB4WjBMnz2PP/qOSxzkkiCo/yksZPl2rxKdrAW83jp4tBLQJ47iYwnDosgy5RU1vi7UDsTKMnGN+AHeJNqFTMw6lHDgWJ6OFIoTji0EnN/FBbQ26LJhK86o9rvZtL0k4mcLkPdAXJll9vZszx/i7OF66p6LIM//b3wPw92BoF1FOgd5QEqLKvv8i8PRXDIXF5dNpHsSx8m2OcL+a0xjameOX3VWXw2hiOBEvoG9rceX0dzc02g44+vEHEBnhOAGYw8OC8flH72Di6zOtuv7CxThcuBhHT1aiXsktYth6Uo6tJ8kWNzgRL8eJeLIDUY0zwBELpXQJFJ1GSa7lvX9dA8WHLOFcQO7ltVZdK5dxPDtYwME5HFMeQR28fez6SpKbpbAq/w2HgZGfVxaC3ZpzbJlZOyEIAENqMOPhy+JvsSgvTaPtgJNeftbhyvTkqPvQPCqCno4EQRAkBuuxUE5uotMozU+wLAaDeojOIz9xF4zF1u3G8GAvjg8e5/D3sDZ3ZrUQnL8RGP+NDEZTeSE4tAvHuqnm0DG1ZWB7QCmvvhxnJQiCGuSmAUfjm+Mz4sGhaBHteKKLMYYxTz7UIG3aPCoCgwf1kTQ2Y33h4+2JOwb2Rveu7UE0XHw9BHSLFtCrpQmtQgQEeNLev/bG3ZmjV0sT2oTRVnvV4XDDxJwzMCb+xW/Qpld7TOkWDoWzt+g8dOlHa1GfyqO593bn+HbCDUEn6rVdp7MFAXhjCcPveypfN3GYgJmP8TovanFSALd3BLZVs1jkSpoEHVmpQ2hYJFKTkxpV5xt25wDRaZw5dwlrN2xDQmIKAvx80a5NCzz9xIPiheoDQzHjo/nVHu/Xuys+mjHJqrQPHD6BabPmSiKWXnjuMXRoH4NmEaFo2TyyUhDttPQsJCSmYNvOvVi2YgPSM6r+eNu24ScoFXV/HBZptbhv5ARJ7odxY0aib+8uiIoMR7PIUPj5ln9G5eTmIyk5DfsPHseylRtw+mzVgbu/+vxddGrf2qoyvPzmBzh34XKD6kfrp5Za9zGvlWHMAmnjmD7Sx4SerUwI9QFCfDhCfAHXauK6J2UBZxIZzlyT4Z8zMpxNlMY3M3qgESP6mqy69sftCmw6Wv79O7SzCROHG8v95qy0/t0V6sMtttnO03LR8R8f7WdEnxgBUYEcUQGAbwWnS04hR5KGYf9FOVbuleFKetW2/+BxAzo2s05ATvqfE642oD2vHU4MOntFSxLuxaCtXoWoPKNEpy8YdCjOrjlERsWqdG/O8fXzggRCsBaqs6xwLQXGLmTYdabiORyfPc3xzGDrxWnHyOrFYFy6NNVr1zyw0YnBiHBxcwX/3Lzz5sKNsiz+aSV+WzIXEeHBIsoWjKF39q929a+3l6fVHqssjbgAlFGRYXhl4mg88eh9NS68CQ7yR3CQP/r07IyZU1/G7j2H8PlXP2LPvvIfcnXd7/kGYuMleri7YeK4xzF2zEgEB/nXKH59vD3RqUNrTBz/BM5fvIIlv67Bov+V38C7datoq9vGzdWlwfUja7fHyy6QxkvUNVrAYwOMuKcbh2cdtuAM9wfC/Tnu7mbC5IdNOHSJYdEWObadEucMCfPjVttk45HKvwV4VpWe9d5NtYpZLF98hnVpq504xtxuwti7TAiuwdfj487g4w50ijJh4nATzicBS3cq8Nue8rZvHWq9LV2cufTveRvicMPETp7SDO9YEoNylYfo9LWZJwFu+eur4i0tYxzfT+RQq+x7g2gKgHs/qkoIAo/0Fa4LQevxc6++8+qNDKnZ4odFIoIaX6zBsJAgq68tLdVj0uRPqjx26sxFTHh9hujy9e3V1aHs5eXpjiXffYLThzZi/DOPWrUCe9CAXvhrzQ+YNe2Veq/Pu+9MxIXjf2P65BdrFIJV0bZ1C3zx8WSs+mU+fLw9QdiXkX1N2PWBHhveNeCJ2+omBKuiVyuOJa8asfN9PbpEm8jAdeDVeww4NrcU0x+tWQhW2ZfCgc+fMeKHlwxwd26aw/iOJwbdg0SnoddmWPxykTuJF4O6rNM1nlNReo0exBHubyPDVeMVvJIGDJ3Fqt2DMz5D/C3gV8N7KC5DvPiN8G98nS80xPrt5q4lpVr0sO3ZdxQHj5wSVT5rBIqtaBYRip2bf8HIh4ZLkt6brz6HnZt/RnSzcPMzQ2+/FevOziqs+mU+przxPDzcxc+PHj7kNhzYtQqDB/UhVWAHXFQCvn5ej3ljjWgVKr1wiAnjWDfFgMkPGaCQ0/xCSyjlHD+8ZMA7DwvwkCDk2vCuAnZ8oEfvVk1vbqHDiUG5k/gvXGNprsXjCpWX6DxKa9htpOxewzcYP8TGHZuX2d8OwMFYYPj7MiRns0plu1E+KRZ4+NWgrTPyJHiBKhvXQ7FN6+aidhspLKw5dt/5i1dElTEwwM8hbNWjawfs3PwzWrVoJmm6Pbt1xMbVi+xaF38/H2xd/z8MH3KbpOmGBAdg/Ypv0bF9DCkEG9IhUsC2WQY82Mu2zyOFnOGVewVsmKaHh5oEYVV4ughYM8WA4V2lFW6hvsAfkw1oEdS0BKHDzBnknIOBwajToFhzXpxQy7tscUqDsSRPdB4GbXqdpk20i+BoGWLj4WHGborBDYeBlxaZI8+Xm4PJAXbDZ8kBvYFh63EONxExj1OyYdEWp+Jhleu+LGnp2bfyaASxYwWTuAdNyxY1b0Gz8LtfsH3nPqvzyM3Lr3c7Db2zP35ZPAcuLraZJhARHowvP5lil7q0iI7A+hXf2TSu5KIFHyC/oMjq66v6iCXMjLvLiKkjjFAp7fcA6tgMWDfFgEfmKJtk0OzqCPPlWPm2AZE2HLz4+nkjCnTifDMkBq0WM0BO7O+SpVUdObG/2TwPxssffKgXt5sNv/kL+GClrNZlfnq+zKa2WLxdhsXbxWZwEbc0LL8laBsosZevirrew90NK3/5CqOefr3ac67EJ+JKfGKDtVHrmGgs+/GLSiuEpeb550bZpT6rfl1g8wDj7du2Evk9SYKjOiE46zFTvXyJmoeN9XjkMyU0hbRrCAD88rrepkIQANpHcpF9qWHZlO4sG8EruMra2ymcXGZeRSFIX/qOSlKyuLg7dw8ZiH+3LHPIWIVSsGDOezYXgvZi1rRX0LJ5JN30DZBWIQKmjjDWaxlaBANLXjXS8xzAG/cb0SqE7ksSgw2G8p8FHq71ki0YYxAEeoA4IilpmaLT6Nq5HfbuWIHJk8Y3KtuMGzPS6nAvjkab1s3x6sSn6YZvgDg7cSx+0WDXoeFq+3o0x0t3N+1Vxs0CBLxyD620JjHYQKgq5F+Ah33y9qxi7h8N/TgmyclpkqTj6qLG9Mkv4urZXZj21gSHWgVsDf5+PpjpAKFfpOK7ee9DqVTQDd8A+fBxI1oEO055Jt1nbHILG8oyf5wR1JVIDDYYyqzjuElmgX3yzi8m+zcUNvy1S9L0/Py8MfWtF3Dp5DZs37gUr704pkEKw+efGwUvT/dG0ca39e+Bbl3a0c3eAIn0F/DYAMfyQjk7MUwZ0TQ9Y92iBXRrTqNcJAYbHOVv2kw7LczMyq8cRoYcg47Juo3bcfzkOZuk3btHJ3w043WcO/IX1ixfiJEPDW8w3ql7hw1qNG380H130Y3eQHn6dpNDjqrc2cmEwCa4v/H9PWl42JaQw9VGVFzteu4aw/Cutu/Al1Mrl4RwXN77aD42/7HYZukrlQoMGdwfQwb3xxefTMbaDduw4NtfcPVaskPao3WraNErYiuSlp6FnNw8RDcLh1pt351sht8lbTzBvPxCJKekISQ4kHYdsTEP9hK/ejhfC5xLZLiYwuDrztEmnIte/KCQM4y9y4RP/rDP6zszn+FohZClzkpm9Wrb4lKOc0nV2/VqNZsUDO0i7fB4nhZI1jCE+Ajwcaf3pGOJQQ54xzwKtbe4l0FxbixyY1dXe9wnZhScvVtaX0xuQtrhzwAuWKhK+fAnu04Dbz1kexP+fbwKo3LLN3pA55ehcPayOk+DLgtZp6sP3usROQRuwT1F1cug0yDr9PdmeduIXJ3/7T2Cn5atxbNPPWzzvHy8PTFuzEiMGzMSa//chi8XLsHps7EOZY9HHhomSTrnL17BtFlzcfzkOeTm3Zqj0TomGiMeGIopbzxv87oM7N8ToSGBotPJLyjClBlzsHvPYSSn3NrsOzjIH4MH9cEns96Et5cHCOkY2tmEQC9xz5lFW2X4cJWy0u/39TDh8zEGuIvYMeO+7vYTg1tPyrH1ZPkA+S2CBOz+2Lpde1JyGB6cXbcoAd2bCwiTIAZ+vhaYukyB/RdYuTA9vu4c/doI+PhJI7zdmuY97zBi8MYLXuHsDbVfW5Ga0mTxg06u8hSdh8zJHYI+36KwLcuxeCBfB3jacB94zjl2na448s9q/Lh1DeoOmcLJ+ow15y3m4eQZKdreyIlttE7OV9/6EF4e7njofvsNKT78wBA8/MAQbNqyG29M+QRp6VkOYQsphoj/3vYfnp0wBVpd5Qm0F2Pj8fHn3+HYibNYtOBDm3rXHn5giOg0Yi9fxRPPvoFLVxIqHUtLz8KyFRtw+Ohp/PrjHLRt3YJUnESMEjlX8LO1cizcXPXrdeMROVKzGVa9o7d6lXK4PxDhJyBR0zRmej3QS/wQcWwyw5iFSiRrKts8u5Bhw2E5jsfLsORlPdqGN7173uHuJGNpnniF6+Rt8zycvVrUStyWFWU/bLWtmlm1l6GgmFUoh+VrlK7B4oRgLeypUHmLrptJX9ioO+KzE6di+659ds/33mGDcPCf1Xh85L0OYYd2bVqKun7N+q149OnXqhSCZdmyfQ/ufng8SkpKbVaXzh1ai7r+0pUEdB/wcJVCsOJ5vQaNxMXYeFJxEtGrpfVTeuLTWbVC8JZzQIaNR+SiynhHx6azqrhTlLgpVpdSGAbPdKpSCJYlWcMwZJYKF1NIDNY7+qJ00WkoXQNrEBbil/a6hdS8KXzFrZ3mbwKSNbaZN1hYzPH+ygqLR2oRoNQtpK8EQs2yPZVu4ofKDMWaRt0RTSYTRo5+DXMX/gS93mDXvH28PbF44YeY/f6b9WqD1jHRoq7PzSvA5Blf1Pr8cxcuY8H3v9qsPkGB4lZyT6lDXQDg7emfk4qTADcVh6eIuLBL/6nda/WH7XIcvQKr/3ya0HCm2AUz7y6v2yDo1F+a3nIKh6uxPj9OdBpMroTCxR9GXVY14kX80l7XgM4AkwO8evd1Re+g3sgw8XuGVW9zqFXSeglf/1GG7MIKXsFajKu6BvcWL2QsegYZlC7ixaC+4Fqj74wmkwkzP16A9Zt24Ju5M9GhXSu75v/yC0/BxUWN197+qF7qHx4qLqDbnK9+REamps7XjHxoGKIiwyStC2MMgQG+Vl+/8e9/6uwp3r3nEP5Yt0WyeZdNlYgA64VHUQmwYk/txOC5RFmd5841TTgCvTisnSe06YgMB2Lr5vc6ckWO1ftMGNmv6azadjzPYGGyJBvuKF2qf7Ho866KN5zSBWrfdrW5j8tx+LIMr/4grdm/WMew6WjFkDK1sJFrMFTuoaLzL82/ajEPxsTXtzQvrsl0yhOnzqPfnY/hhVdn4My5S3bN+7nRI/D9/A/qpd6hwdZ/NJhMJnz/v7rva15SUop1G7ZLXpew0CDI5dYPA678Y7NV1y1ftZG0g9iPEl/r30BnrjHoSilim5T4e5hXUFvLqn1yK69rWt5Bh7trGQMEo/hhMpVns2qPGXTpMOjEbwXmEtS9FhWq/NOGIwwvL2bQG8TJXs45Pl4NzFnPqrRjTbgGdhdtA8GkR3H2+WqPO1loh7rUU1/UtCZxcM7x26qN6Dt4FJ587k3sPXDMbnk/Oeo+PDnqfrvXOSQ4wOprU9MyYTBYt3/s5bgEyevSLELcR9aFWOs+fk6euUDqQayQF7FqtZCC/tugPcTNjbyUZp2QPJfYtMLNOOQnjKk0V3Qazt6WJ29r04+KzsMz4g4o1NbNC1q9j+GRzxgy860ThAU6jnFfy7Bgk3VNyBRqeLd4QLQNdJmnLA6Vq71jxAtOYylYE96gfcNfuzD8oXGIbj8Yb077FDv+2W/ThQ8A8PbrY+0ewidIxG4p1xJTrb429tJVyesiRtjq9YYaF41Uh0aTi5zcfBDWE+5n/bOmQEfx6qQmWMT6Q70BNS4aqbYtixlyCmmYuF4x6MQvIlH7WhYh2szjovNgTAafmFFWX3/osgx93pFh7p+ArqR21xhNHD/tBPq8I6s0NAxUXrRSHV7N74dMKT7OjTbTssfK2Uf8vDdTSQ49EQFkaXKweMlKPPT4S4huPxgTX5+JLdv32CSv5lERGPPEQ3atn7+fj9XXJiRa7zk+cvyM5HUJDPCrl7qYhXEKdRYxHyVe1gsA8gxKT4CIxSMJmeLE+bWspjPk75CD4qV5cXD17yhO5SrUULqFw1CUVOXxkuwLMBl0kIsURO6hfZEX9yf0hUlWXV9UwvDZWoaFmzkGdwSGdOFoEwoEeHH4uDHk6YDMPI64NIbtpxl2nmKVFoqUF6g13/xyJw94RQ0X3U6cc+gyqhfVTK6Ck7v4gE36omQQFV46RVosW7EBy1ZsgJ+vN0Y+PByPPjQc3bu2lyyPB++7E0uXr7VbnYqLrX+TeokIuhweFix5XXQ6EXURuS+zh4cbdRAxbSfC6e6uJvs5Unt4uYnz7Hm4NB3PoEOKwcKkf+HTUrxXQu0TU60YBDiKs07DLUT8alqf1o8j/cjnIm94ho1HgI1HbD/M4NXqEcjkTqLTKc2Lg2Aoqva4s08bSRaPaDNP0RPRAprsXHz3w2/47offMHhQH8yY8hK6dm4nOt1WLZrZtR5ZGuunh0RFWj9Hr01Mc8nrUtdVzeU8If6+cHVR1xgrsTrEzlds6qTmWv8MFuNVJKomM9/69gjwBFRKjlKDdWk087d+FXNDwyF9oMbiTAiCUXQ6LgFdLIuMDGkm5bsGdIZHs9qHc+Cc3/wTgzXpqH3bwzPiDknqrc04XqNdxMI5R1HaAXoi1pKduw9g4LCn8MqbH4pOKzwsWLSXyl4CKlKEALKF6E1NF7dArXPHNlZd175tK1GrmAkgQ8SUdX/aFVBy0nPFibE2YdYtQIkOFCCXN505oA47IG7QZUsgBjtBpqg+eqgu87hoQXYDv7ZPQe1XuyE6xpj5D8wqUVj2mhtp1QYn93AEdXtdEm8dAGgzT1iqJdxCxQe05sZSwFQCom4sXb4WI558RXQ6XTq1s1uZMzKt7/Nuri5Wb/9mi20Ak1MyRF0/4oGhVl03asTddPOLFfIixIe/p0AGlFoM5okTZA/2su4d/3CfptWWDhtIpzjnHFQid65gTAbX4F4oTNpV5XHBWIyi1H1wD+0vuryMyRDU9XWkHHi/9vMH2a3A0GaBd2PO3y2hV04omk+4eawuyFXeCO45WZJFIwBQknsJhsLqA0Gr/TpArhQ/d6m0kc4XvGNgb7w3+SWrr9+95xDen/21xXO27dyLfQePo1/vrlbn065NC/zz30G72CRNpDdt2lsTsPbPbXUWgj27dZS8LhmZGhiNRigU1j1in37iQSz8/ldcvVb7+z84yB8vPDcKhEghn229+PBxZ+gWLeBYfM0f3M5OHL1aWi84NIUM5xLrx59jz8HwPC2D0cStjjU4epAR32+RIT2v9rbyded4/i5Tk7rvHVYM5l/dAi8JhjPdQ/pVKwYBIPviCrgF9waTiTeFTOmC4B7vIHn/zDqvgL0hAs0aj1X4/VYHtCbcB1OoEdxzMhTOPpK1T9bZpZY9NRJscwcARakHG2XHKygsErXYw93dtUYxCAAHD58UJQZLS/X288iIFIMxLaPw7FMP46dltV/0IkaQ10RmVo7VIWZUKidMe3sCxr88vdbXzJjyMtRqZ1JzYj9KcsR5ol6914QxC2oWHs/eYcK7I60XHD9sk9WbGCwqtt5G7uq6S8mMPIZQKzf0USkZ3n7QhDeX1t5Wbz9khEsT60oOO0xsLEqGIIgPPu3s2xpyp+oncphKcpCfsFU6da32RVi/D6HyaukQdlS6BpvL4xEhWZqFaYehL0iwoD7lcAvuKf7rk3PkJ+5olB3v1JmLoq5v2TwSHu41e17F7nMcdzXRbja5GBuPzCxx00Pem/ISBg/qU6tzFy/8EC2bR9qsPmIDQD/2yD14ZcLoWp37yoTReOqx+0FI8KFWzBCbbL3YGdxJQJ/WlkWeuzPH+CHiPE+7ztTf3NAMEYs6Ar0Y/D3qJghPJYgT6KMGCBhze+3WIYy53YinBja94X6HDqJTmhcvOg3GWI2LO3Ivr4XJoJNOEDp7I7TPe3AL6Vev9lP7tkVYvw/h5BYiWZqcC8i5sMzygy5sIGQK8Z9VxpIcMEHfKDuewWBESqr188pkMhnemTSuxvO6dGorqpzWBj+2lq079oq63t/PB2uWL8SnH7wFpbJqb3/vHp1w6uAGPD7yXpvWZcPmXaLT+GTWG9i4+vtq4xaGhgRi4+rv8cmsN0jFScjve8W9Gn99zYC7OlUv9ha9aECAp/Xp60qAfRfqd3GDTsRU7gXjDQjxqb3g2nhEvPD9+CkTlk3Sw8u1aiHq6yFg2SQ9Pn7K1CTveYfefC8vYTvUPuJ3sPCKGob8+E0QjFULPsFYjNwr6+HX5gnJys5kCgR2eQkqr+bQnF8GBvt+aXhG3QPf1o+ByaT9esy/thPGYo2lisO75YOS5FWYur9Rd75DR05ZvegBAF57cQzi4hOrHRadOfVlDLtrgNXpFxeXIDkl3a42WbthG0Y/Lm5nHLlcjpeefxLPjR6BuKuJiLuahOycPLSMjkTz6AhRu4PUheUrN+DL2VPg6iIu+NygAb1w5fR2XI67hoRrybiWlIrw0GBER4Xb1LPZlFm1V44pDxvh7GSd4HJ2YvjpVSN2nDLhdIIM5xIZSgzmuWgvDBXQLkLcrLvNxxgEXr9isLCEw8XZujIMaMtxeI4BablAak75ufEHLsrw2TplJTE45xkj3ET6GAa15zi7QI9LqcC1TIbELIYwf46oAI5WIU37nndoMahL2w+BT4SMiRM0MoUzPJsNQ+6V6ucSFSRshVezoVCofSWtg1fUMKh920Bz7meU5Fy0uc2c3MLg2240XPw6SJ62YCxB3uU1Fs9xC+kPpdpPdF6cc+ReXt+oO9/qdVtEiUEAWPDFe3hs5L3YtfsA4hOS4eLijKjIMDz68HDRwZT/23fU7jbZ8c9+pKVnIVjE1nQ3UKud0b5tK7Rv26re2njn7gO4/25pQjm1bB7ZoMXf2DtNyCmy3uuSkc+wZId9XlkFxQxbT8jwQC9xou3OThx3dpLe0/T91vp/dcenyxAoMq5isDcQ7F0+DU1B1ef+c1qG+3pK41RpFQK0CuEAKC5kgxCDAFCaGwe1BFuaeUYPR97VzeCmqsOZc8GA7NjVCOw8QfI6qDwiEdpnBrQZJ5F9YRkM2lTJ85CrvOETMwruYf0lCx1Tkbz4zTDpCyycweAjkVfQWKwBTI17b6fNW3cjI1MjausyAOjbqwv69uoiefl+/HlVvdhly449ePaphxtFG2/YvFMyMdjQefw2cS/y80mwmxgEgGX/yfFAL6PD2XH/BYbYlPqf4XUgVlbj3Egp2XhUOjFIVMbhN97Lvvi7JLEA5UpXeLWwLFSKUv6rMZCyGFwDOyNi0BfwbfcsVF7SeCuU7pHwjhmFiEFz4RF+m82EYElePHLj/rR4jkfEHVC6BkkjPK9uafSdj3MuybwyW3DxUrzN9j2uicVLVjaaNl655q86hYchHIcDF+U4fNnxgg7/uN0xgorvOW9f2/x1TI6rGXRfNlkxWJobC5NBGg+Rd/Q9ULpaHjrLOD4fJRIsXLGEV7O7ENZvFiLv/B7+HZ6HS0A3QFbL7eGYAs6+7eHXdgwi7/gaEbfNhk+LByBTqGxWXoMuC2mHPwUs7AojU7rBp/VjEokkEwoS/m4SHXDeN0uRk5vvcOX66de19Zb32fOXMHfhT42mjV9/52PJgtsT9uXF7xXILXKc8uw5z7DtlGOIwSNXZLiWZd883/pJQX2pqYpBANCmH5IkHSZTwL/j85aFiGBA2uFPYSzOtnm9FCoPeEQMQnCPN9F8+FKE9n3f4vkBnV5E87t/QWjvafCMGgqF2sfmZRQMOqQd+sTiHsQA4NvmSciVrpLkWZxzqcl0wKTkNLz29kcOVaaLsfH49ofl9VqGjz7/FlfiExtFG+/69yB+X72J3jYNkPQ8GV77UekQAkRTAEz8XulA1mH47m/7CtNDl+VYsYe2W2yyYlBz7mdwQZq5CWqfGLjWEBBZMBQh9fBsCEb7boMmV3mIOi41nAtIO/oFDDrLvnmVV0t4hA+UKE+OrNM/NKlOuH7TDvzy23qHKEtmVjZGjn613sthMBjx6lsfwmRqHGEeps78UnQMRaKexPwZGb79u34FiCBwvLhYgTytYw1br9grR5adBzY+WKlAZj7dl01SDELQQ6s5LVly/u3GQK6yHOTJUJSK9GNzwXnTnbCadXpxjSugmcwJgV1elk4EFKXCqEtvcrZ+ffLHdd5KTWqKtDo8+vRrSEhMcQib7Nl/FEuXr7N5Pn9v+8/qnVZq6zHKyc3HW9M+s3ldEhJTcPzkOVEfY0RlPl2rwPH4+hFiegPw4mIF9l9wPI+Y0cQwaYkCJpP97pvCEoZ3frb9QqKETIhq84bWlWQNpaDZZ5ZIlpbcyR2BXV9H2W3fqqJYcxaa8782yYdfXvxfKEz+r8bzAjpNgNLFX7J8NRd+b5L2NhiMGPPCZMz7emm95G8ymTB24jQcO3HOoewyZcYX2LRlt83Svxgbj2cnTLFqm0egbttDrtu4HbM+WQhBsM0HZmGRFo8/8waKS0qtTsNaOzR2OGd48ksltpyw7yszXws8OU+JTUccN/DH7rNyzFxh3/LtOCXHrBVyCIJtFFdhMcfTXykhoiuhoXWlBiMGjSXZks4lU/vEwDvm0RrPK0jYipwrfzapB19R6kFk17DLCAC4h98Bt5De0gkinQbFWcfRlJnx0XyMHP0qriWm2i3PCxfjcN+jE/DXtn8dzh4lJaV4/JlJ+OqbnyVPOyc3H6PGvA6tzn4hjL5csARjX3oXJWLeMlUgCAImvjYTZ89fAmEbCksYxn2txLvLFCgutb3b58gVhmEfKHEg1vFf00t3KfDOUgX0Bvvl+eN2BcZ/o5S8LQSB46VFTojPkDWp+7tB1TbjxNeSDmN4N78fat92NZ6XG7sSmae+l2zeoiOTfWkN0o8vqPE8pVso/No9LeGXN0fm6UX0xgGwZfse9Bw0Agu++0X03sIWP3QKizDrk4XodftI7KmHANN14b0Pv8IbU2fDaJQm7tuJU+cx5P5nEZ+QZPe6/LFuCx587EVosnMlSS89IwsjR7+GPzfvpM5jB37+R457P3ZCQqZt0i8u5fhwlRwPzVYiSdNwXtG/7ZFjxOcKJNpxhfHWk3KMnKOsNlB1nftSLjDqSyV2nZE1ufu6QdXYVKKBTnNGsvQYYwjs9jqUbqE1fxUm/4eUAx/AZChqlDeCYNIj/dh85F1eU6N7W67yQkjPKZDJnSTL31CUhpLscyDM6HQlePf9eejU537M/HiBpMO3V68lY97XS9H79kfx5YIlDWae2A8/rUKfwaOw4o/NVnvWCgqLMPuLRbht6JOIvXz15u9OTvZdpbnv4HH0GDgCs79YhCxNjtXprP1zG/rcMQrbdu6lTmNHYlNkGDTdCRO+l+OfMwxGCebMXU4FPl0jR5/JTli0VYGapjE5Iifi5eg7xWyXY3H2Kf/Jq3LcNs0Jn64Rt5hlzQEZ7njPCQcuNs3VyoqGVuD0Y18hesgPku25K1e6IqT3dKTsmwljseVPvdK8y0jeMw3BPafAya3xbGRoLMlF2pHPoS+4VvPXg9IVIX1mSLptH+ccaUfm0BumCpJT0jF34U+Yu/AnNI+KwAP3Dka3Lu3Rs1sHBAXWfq6mRpOL9Zt3YO2GbQ7vBbTExdh4jH95Ot77cD5emfAURj/+ILy9al5l/9e2f7Fuw3as+GOztH1HhKdSo8nFJ198j7lf/4RnR4/A+GcerXG7OUEQcPTEWazfuAMr/tgsSkgSItvexLDpiAKbjgCBnhyP9TdhRF8B0UG1F4ZnEoB/z8mw45QcR+Maizfqll2CvAT0iRHQvSVHu3ABni6Apyvg7syhVkknFguKGb7+S4FFW+UY1d+EsXea0LKGV7QgcByNk+HPQzKsPyRDvk7WpO9n5hbYucEtH/OJeRzeLe6TtmMXZyN533swlebVbDS5CkHd35B8/1+9Nh1Ju9+o9nhQj8lwDegkaZ6l+QlIO/xpDdvM3ap3aN/3ofKIkLQMRRknkXH0c3q71JHOHdugeXQEPNxc4e7uBnd3V6idVcjNK0CWJgcZmRpkZuUgNS0TGZmaRmuHqMgwhIUFISjAH2GhgfD28kR6hgaJSam4ei0Z5y5crvH604c2WuchunwV3QdIt3VeUKA/IsKCERIcgJDgQAQG+CK/oBBXryUjKTkNR4+fpRvfwfFQc/h5cPh7cPh5AH4eHL7u5mN5OqBAy5CnA45cZk1egNgSbzeOYG/zX4g3h78XR75WhsQsIDmb4UIy2b4sioZY6JzY3+ERORhypYt0hlD7IqTPe0jZ/wEEvWVfMzeVIu3QbPi0fgLeze9tsI1fmLIfmSe/rt1Xg1yF4F7vSi4EuWBCxvF51BOt4OTpCzh5+kKTt8PVa8mitnxrUYM3zhJSzfu7QXpGFtIzsujmbsAUFDMUFDPE09Zp9UpuEUNuEcP5JLJFbWiw0jjj+NeSz3Vycg1GWL8PoFD71U6UXvwNKftn2Xz7OqnRF6Uh7ejcWgtBmdIVoX1mQu3dQvKyZF/4DRAM1BOJeiOqWZjV12ZmUiBpgiAaPoqGWvBizUkUa87CxV/aoVqliz9C+32ItEOfQF9Y8ydFSe4lpOybDtegXvBpPQpOrkEOazNjaR5yYlejIOlfMNQu1pnc2Qehvd+D0jVQelFamIL8JrIHMVE7/Hy90btHZ6uuvXL1Gi7G1v3DbNidA6z/KG3Ew+8EQZAYbBCkHv4MUUN+gFypltYoKk+E9p2FtMOfoSS3dnG7tOmHUJR+FJ4Rg+DdagQUKi+HsZPJoENe3AbkXd0CCPpar1FTugYjpPd7UDhLXxcuGJFy4APqgUT5jw+5HL8vnWvVtecuXEbv2x+t0zXdurTDXXf0s14M0hZzBEE0Ahr0DEoGAamHZtskNIZMoUZwr3fh0WxIHcpjQkHiTiT+MwnZF1fBZCyuV/sIJgNy4zYh8Z/XkBe3ARBqv+WWS0BXhPX/yDZCkHNknPgOgqGQeiBRXlxlaqzew7ddm5YYNeLuOl0zY4q4rRSPHDtDjUYQRINH0dAroM+/gryrf8E7+h7pBaFcCf92z0Dt08YcdNpUu9hm3FSKvLj1yE/4G2rfdnAJ6AzXgK5QqH1sbg9jaQF0mSehzTqBkqyzEIzaOipsBXzbPAGvqGE2K6M28wS06Qeo9xFVciE2DgH+1oUu+mbuTOTlF2DrDstx94IC/TH306m4Y6D1O+hosnPx797D1GAEQZAYdARyLiyHi38nqNzDbJK+W3AvOHlEIv3oFzAU1X6bMG4qhS7zOHSZx6HBEijdw+Di3xkuAV2g9m4JJhNvfs45SvPioMs6BW3mSZTmxVm9J6Jc5Y2g7pPg7NXCZm1lLC1AxtEvqOcR1XL2/GUM7N/TqmtVKif8sWwh9h08jrUbtuHsuUtITctEQmIKenTtgObREejQrhWefuIheHm6iyrnlu17qLEIgmgUNMg4g9UoGUTd+R1kCmfbCs9La5B7eY00RXb2hVLtC5mzN5Rqf4Ax5MdVH+/MPXww5E6uMBRrYCrJhbE4u8ZA2bXFI+JO+LR+TNJwPZWEq2BC4j+TYCyhSfdE9fTr3RVb1v/P4cv55HNvYsNfu6jBCIIgMehIKN3DEd7/E8l2J6kOgzYDmacXoSTnorRiiXMwC269mo5bg5N7OAI6TYDKM8qmNruxy0hx1knqdUSN7Nz8M3p26+iw5dPqihEecxsMBiM1FkEQDZ5GFYLbUJiEtCNzbL7XqtI1EKF9ZiCg80uQOXk2zK8AuRq+bZ9G2IDZdhGCmvPLSAgStWbxkpUOXb6Nf+0iIUgQRKOhUXkGb+AecSf82z8ruRetKgRjKQoS/0F+wt8wFovbOcAenkGZkwc8Iu+CZ7OhUDi52aU98q7+jezzv1JvI+rErr9+QY+uHRyuXDm5+eg24CFoNLnUSARBkBh0ZHxiHoNX8/vsIggB83y4orRDyIvfBH1BgsOJQYVLEDyj74ZH2EDI5Eq7tUNh6iFknphPPY2oM9HNwrFv5wq4ubo4VLnGvDAZa//cRg1EEESjQe7kFjSrMVasOPss5CovOHtF20dVMxlUHuHwjBwM16AekKs8YTLoatznuHI6TNTxigLQPXwQfNs8Cd/Wj0Pt1dzm8ynLos04iYzjc6mXEVaRm1eAgoIiDBWxQ4jUrP1zG2Z/sYgahyCIRkWj9QzewKf1E/CKvsduHsKK6HWZ0KUfR3HOeRRnnwc36qo9V7RnUOYEtU/r67ENO0LlEVlvdtdmnED60TnUwwjRTHtrAqa8+Xy99eEbaDS56Nr/QeTmFVCjEARBYrCh4Rl1L3zbPF7vLxMAKM69gpLs8ygtSIJBmwpDUSq4UFp3MShzgtIlCE5uweb4hT5toPZrW+/145yjIGk3NGd+oN5FSMYzTz6MeZ9NhUJRP6FRz1+8gmcmTMGFi3HUGARBkBhsqLiF3Y6AjuMcQhBWxFiSC702A/qiZGSfXVLteV4tHoaLbzsoXPyhdPFzuHpwzpFzeS3yJIrDSBBlGTyoD+Z9Ng1RkWF2zXfJr2vw2tsfUQMQBNFoabRzBiuiL0hAaf5VuIX0cThBKFOooXTxg0zpgoKErdWLweb3w8W/vU0DQ4sRgllnf0LB1c3UqwibcDUhGT8tWwuVkxJdO7WDXG7b+a+5eQV44dX3MO/rpWR8giAaNbKmVFld5gkk73kXgrGEWl5KISgYkX5kDgoTd5AxCJtSUlKK6R98he4DHsY3i5dDky19eJeCwiJ8s3g5etw2Aus2bCejEwTR6Gkyw8TlkKsR3v8jKF2D4UhOQr02HUm736j2eFCPyXAN6ORQpjTqC5G8ZxpMJdnUm4h64bnRI/DkY/ejfZtWcHGxfjvK4yfPYd3G7Vi2YoNNRCZBEASJQQeDgyG4+1twDexCYtBKSvLikXrwQ3BTKfUkwiFoHRONDm1j0KFdK4SGBCIwwBeBAX4I8PeFj7fnzXA1uXn5SEvPwpX4a7gSdw3/7TuCK/GJZECCIEgMNkW8W46Ad8uHHWIeYUMSg4WpB5FxYiEYOPUigiAIgmjAKJq6AXIvr0FpQTICu74CmUxGd4QFbmz5nBO7Cnlx68HIJARBEATR4CH1A0CXcQhphz6G0aADLyN6iIpq0IiM4/ORF7eebEEQBEEQJAYbFyU5F5C67z0Yi7PByOVVCZNBh9RDs6FNP0TGIAiCIAgSg40TgzYNyXunozQ/wewhRNP2EnJutoGhOBsp+95DSc4FukkIgiAIgsRg40bQ5yPlwPsozjpzc04c57zJLJPg/LoA5gBjgD4/Acl7p8OgTaObgyAIgiBIDDYRQWQqRerhz1CYsheM4eZK4xueskZZZ9yqG2MAGKDLOoOUA+9D0OfTTUEQBEEQJAabFgwCMk9+i5zYP67//3WRxBuXKCw7FH6zjgAKEncj9fBnFEOQIAiCIBo5CjKBZXKvrIVel47AThPBZPJyi0s4vz6WytGgFp3w68Lvxn8rxojJiV2N3CvrKHQMQRAEQZAYJABAm7ofqaUFCOo+CXKF+ubvNwNV3/AY3vzdAQVgGVdm2WKXO0cwIuPU99Cm7qdGJwiCIIgmAg0T15KS7LNI2TcDxpJq9ixlt0TWDeFVnyuROec3F4PcHAZm1QtVk7HYHDqGhCBBEARBkBgkqsZQlILkfe9BX5Bc7TllBVdZcciB6/MNbaMQOS8jAMHBGLtZFksiELgROmYGhY4hCIIgCBKDRE2YSnKQvH8GijW1F06M3Zqbd2tlslkUcvCbYrE2OrHsueUWf1xP25xX7cep9QXJ5tAxRSnUuARBEARBYpCoDdxUgtTDs1GYstfqNG6IQgZ2UyzWZq5h2XNre011FGsuIHn/DAodQxAEQRBNGFpAYrUiNCLz5Lcw6LLg0/KhBlf8wpS9yDy1GOBGakuCIAiCIDFIWEvupdUw6jLh32EcmEzeIMqcc3ktci/9QY1HEARBEASJQSkoTP4XhuJsBHd/EzKFymHLyQUTss78iMLkf6nRCIIgCIIAQHMGJaMk+yyS98+qPvRMPSMYr2+xR0KQIAiCIAgSg7bBUHgNyfveQ2lRqkOVy1iSi+T9s1CSfZYaiSAIgiAIEoO2xFSSg5R9dQs9Y0tKi1KRvO89GAqvUeMQBEEQBEFi0B5wow6ph2ejKPVgvZajWHMBKftmwFSSQ41CEARBEASJQTsrQmScWIDcKxvrJfui1INIPTwb3KijtiAIgiAIolpoNbGNyYn9HcbiLPi1fwaM2Ud751xej9xLq8j4BEEQBEHUiAwctP2EjSlI3IG0I3MgGPU2zYdzAZmnFpEQJAiCIAiiluIB+TKAp5MlbE9x1imkHPwAppICm6QvGEuRdmQOhY4hCIIgCKIuajBdBsZIDNoJfX48kvZPh14rrclNJQVI3j8LxVmnyMgEQRAEQdQextJlnDyDdsVUrEHKvvdQnHtJkvRKi1KRtH86hY4hCIIgCKLOcPB0GUCeQXsjGLRIPfgRitKPikqnOPeSOXRMsYaMShAEQRCEFbB0GQTyDNaPIjQi/ehc5MX/ZdXlRakHkXrwIwodQxAEQRCECD3C0xUAPwkwMkZ9aHEGZF9YBoM2HX7tn631dblxm5Bz8TcyIEEQBEEQIuEnFdos4y63AKd8MHiSQeqHgsQdMJbkwjvm0ZoaDFlnlqAgcQcZjSAIgiAIkToQBdos4y45kGVSugV2ZmDtySr1h0GbhuKcixD01Yee0RelQpd5nIxFEARBEIR4Lcj4eoP27EoZADATX08mcQBBWJhk8XhpfjwZiSAIgiAISbih/2QAUGRS/s3B9WQWgiAIgiCIxg8H1xcJur9uikHkHssHxz9kGoIgCIIgiCahBv9BzpWCW2IQAJjwNlmGIAiCIAii8cMYf+vGv2+KQW3GmTMcfCWZhyAIgiAIovHCwVcWZZw+W0kMAoAJfCrnMJGZ6kulM1HHCYIgCIIgahCCJaZSNqXsb+XEYEnG6asM/EcyFUEQBEEQRGOELSrJO5lQrRg0K0bTTA5eQsYiCIIgCIJoRHAUMAifVPy5khjUZp7N4OCvkMUIgiAIgiAaDyYIzxVlnM6s+Lu8qpMN2ozjStdANWOsP5mOIAiCIAiiYcM5/0yXeXphVcfk1V1k0GbscnILagugHZmQIAiCIAiiwSrBNdrMUxMA8KoO17A8NUztGuC3nzF0JksSBEEQBEE0NB2I09pMTW8gubi6c2SWk0guFozG+zl4EpmTIAiCIAiiIQlBniwYhXssCcFaiEGgOOdskjZD0ZpzvpzMShAEQRAE0SCE4HJtpiKmOOd0ck3nymuXZJrBoM1Yq3ALMoLz2xlFPyYIgiAIgnBEEcg5wwxd5qnXgTRDba6ps6hzCez8AAN+Y4ALmZwgCIIgCMJRlCAKBIandRkn/6zLZfK65mPQpscq1cFLGeNuHKwzYzUPNRMEQRAEQRA20oAcRgb8TxBKH9FlnT1a1+tFDfeq/Dq1UsjxIThG0tAxQRAEQRCEPUUg5wxsjYHrp5Zmnb9ibTqSCDgXv/bdmUz+IWNsGDUNQRAEQRCEzYXgVs4xVZd16oTYtKT15nl28HZVye9mwIMAHwbG3Ki5CIIgCIIgRKu/IoBt4eB/akuFzcg/kytV0jYd2r2+2KQXwIMABAAswPxfBDBATS1LEARBEARxXe8BxQAyzX88E0AmA0sTBOzTZZ38y1b5/h9DETP+6l709gAAAABJRU5ErkJggg==" alt="SigaLicita" width="170" height="64">
    <div class="top-t"><h1>Busca Licitações</h1><p>Licitações com proposta em aberto no PNCP, com filtro por portal</p></div>
    <button type="button" class="aba-op" id="aba-op" aria-pressed="false" title="Licitações que você salvou para analisar depois">⭐ Oportunidades <b id="n-salvas">0</b></button>
  </div>

  <form class="busca" id="f" autocomplete="off">
    <label class="q"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
      <input id="q" placeholder="O que sua empresa vende? Ex.: material de escritório" aria-label="Palavra-chave"></label>
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
const FORTE_RE = /(?<![a-z])(prestacao de servic|servicos? de|mao de obra|obras?|reforma|pavimenta|engenharia|locacao|terceiriza|vigilancia|consultoria|assessoria)/;
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
function doisd(n) { return String(n).padStart(2, "0"); }
async function paginaP(uf, mod, pg) {
  const fim = new Date(); fim.setDate(fim.getDate() + 120);
  const url = `${PNCP_URL}?dataFinal=${fim.getFullYear()}${doisd(fim.getMonth() + 1)}${doisd(fim.getDate())}&codigoModalidadeContratacao=${mod}&uf=${uf}&pagina=${pg}&tamanhoPagina=50`;
  for (let t = 1; t <= 5; t++) {
    let r;
    await vaga(PRIORIDADE.has(uf));
    try { r = await fetch(url, {headers: {Accept: "application/json"}}); }
    catch (e) { soltaVaga(); if (t >= 2) throw new Error("sem conexão com o PNCP"); await dormir(1500); continue; }
    try {
      if (r.status === 204) return {};
      if (r.ok) { const txt = await r.text(); return txt.trim() ? JSON.parse(txt) : {}; }
    } finally { soltaVaga(); }
    if ((r.status < 500 && r.status !== 429) || t === 5) throw new Error("PNCP respondeu " + r.status);
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
  for (let i = 0; i < rs.length; i++) {
    if (rs[i].status === "fulfilled") { itens = itens.concat(rs[i].value.data || []); continue; }
    await dormir(8000);
    itens = itens.concat((await paginaP(uf, mod, i + 2)).data || []);  // se falhar de novo, o estado aparece como "falhou"
  }
  const d = new Date(), agora = `${d.getFullYear()}-${doisd(d.getMonth() + 1)}-${doisd(d.getDate())}T${doisd(d.getHours())}:${doisd(d.getMinutes())}`;
  const lista = itens.map(resumirP).filter((x) => !(x.encerramento && x.encerramento < agora));
  const res = {itens: lista, cortado: tot > 100};
  CACHE_P[k] = {quando: Date.now(), res};
  return res;
}
async function buscarEstadoDireto(uf, mods) {
  const itens = [], erros = [], cortadas = [], vistos = new Set();
  const rs = await Promise.allSettled(mods.map((m) => modalidadeP(uf, m)));
  rs.forEach((r, i) => {
    const nome = CFG.modalidades[mods[i]] || mods[i];
    if (r.status === "rejected") { erros.push(nome + ": " + r.reason.message); return; }
    r.value.itens.forEach((x) => { if (!vistos.has(x.id)) { vistos.add(x.id); itens.push(x); } });
    if (r.value.cortado) cortadas.push(nome);
  });
  const d = new Date();
  return {uf, itens, erros, cortadas, atualizado: `${doisd(d.getDate())}/${doisd(d.getMonth() + 1)}/${d.getFullYear()} ${doisd(d.getHours())}:${doisd(d.getMinutes())}`};
}
// tenta direto; se o navegador não conseguir falar com o PNCP, usa o servidor como antes
async function buscarEstado(uf, mods) {
  try {
    const d = await buscarEstadoDireto(uf, mods);
    if (d.itens.length || !d.erros.some((e) => e.includes("sem conexão"))) return d;
  } catch (e) {}
  const r = await fetch(`/api/busca?uf=${uf}&mods=${mods.join(",")}`), d = await r.json();
  if (!r.ok) throw new Error(d.erro || "Falha na busca.");
  return d;
}
const $ = (s) => document.querySelector(s);
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
const CONSULTA = "https://consulta-orgaos.onrender.com/?cnpj=";
const MEUS = ["Compras.gov.br", "ComprasMG", "SISLOG", "Licitar Digital"];
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
  const vmin = numero($("#vmin").value), vmax = numero($("#vmax").value);
  const lim = PRAZO ? parseFloat(PRAZO) : null;
  return DADOS.itens.filter((x) =>
    (!PREF.portais.length || PREF.portais.includes(x.portal)) &&
    (!PREF.tipo || (x.tipo || "Produto") === PREF.tipo) &&
    (DADOS.uf !== "BR" || !UFSEL.length || UFSEL.includes(x.uf)) &&
    (!PREF.cats.length || (x.categorias || []).some((c) => PREF.cats.includes(c))) &&
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
      ${x.numero ? `<span class="tag num">${esc(x.numero)}</span>` : ""}
      <span class="tag mod">${esc(x.modalidadeNome)}</span>
      ${x.srp ? `<span class="tag srp">Registro de preços</span>` : ""}
      ${encerrada ? `<span class="tag encerrada">Encerrada</span>` : dias != null ? `<span class="tag prazo${urg ? " urg" : ""}">${textoPrazo(dias)}</span>` : ""}
      <span class="tag">📍 ${esc(x.uf)}</span>
      ${x.uasg ? `<span class="tag">UASG ${esc(x.uasg)}</span>` : ""}
      ${seloPortal(x.portal)}
      ${(x.categorias || []).filter((k) => k !== "Outros").map((k) => `<span class="tag cat">${esc(k)}</span>`).join("")}
      ${x.tipo === "Serviço" ? `<span class="tag">Serviço</span>` : ""}
      ${x.assistencia ? `<span class="tag assist" title="Eletrodoméstico ou objeto que fala em assistência técnica: confira as exigências no edital">🔧 Exige assistência técnica</span>` : ""}
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
      ${emSalvas ? `<button type="button" class="remover" data-salvar="${esc(x.id)}">🗑 Remover</button>`
        : ""}
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
    ? `<button type="button" class="salvar on" data-salvar-det="1" title="Clique para tirar das Oportunidades">⭐ Salva nas Oportunidades</button>`
    : `<button type="button" class="salvar" data-salvar-det="1" title="Guardar em Oportunidades para analisar depois">☆ Salvar</button>`;
}
// salvou em outra aba (na página da licitação): a busca atualiza o número e os botões sozinha
window.addEventListener("storage", (e) => {
  if (e.key !== "busca_salvas") return;
  try { SALVAS = JSON.parse(e.newValue || "{}") || {}; } catch (er) { SALVAS = {}; }
  $("#n-salvas").textContent = Object.keys(SALVAS).length;
  if (!document.querySelector("#pg")) desenhar();
});
function desenharSalvas() {
  // as que ainda estão abertas primeiro (a disputa mais próxima em cima); as encerradas vão para o fim
  const agora = agoraLocal(), fechou = (x) => !!(x.encerramento && x.encerramento < agora);
  const lista = Object.values(SALVAS).sort((a, b) => fechou(a) - fechou(b) || (a.encerramento || "9").localeCompare(b.encerramento || "9"));
  let h = `<div class="cab"><span><b>${lista.length}</b> oportunidade${lista.length === 1 ? " salva" : "s salvas"} para analisar</span><button type="button" class="mini-bt" id="voltar">← Voltar para a busca</button></div>`;
  if (!lista.length) h += `<div class="vazio"><b>Nenhuma oportunidade salva ainda</b>Na busca, clique em ☆ Salvar na licitação que quer analisar depois.</div>`;
  h += lista.map((x) => cartao(x, true)).join("");
  $("#res").innerHTML = h;
}
function seloPortal(p) {
  if (p === "Compras.gov.br") return `<span class="selo-cg" title="Licitação do Compras.gov.br"><i><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M3 4h2l2.5 11h11L21 7H6.5"/><circle cx="9" cy="20" r="1.4"/><circle cx="18" cy="20" r="1.4"/></svg></i><span>Compras<em>.gov.br</em></span></span>`;
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
  desenharPortais(); desenharCats(); desenharMods(); desenharUfs();
  $("#aba-op").setAttribute("aria-pressed", VER_SALVAS);
  if (VER_SALVAS) return desenharSalvas();
  if (!DADOS) return;
  const lista = filtrar(), nome = DADOS.uf === "BR" ? "todo o Brasil" : (UFS.find((u) => u[0] === DADOS.uf) || ["", DADOS.uf])[1];
  let h = `<div class="cab"><span><b>${lista.length.toLocaleString("pt-BR")}</b> licitaç${lista.length === 1 ? "ão encontrada" : "ões encontradas"} em <b>${esc(nome)}</b> com proposta em aberto${DIA ? ` · disputa ${MODO_DIA === "no" ? "no dia" : "a partir de"} <b>${DIA.split("-").reverse().join("/")}</b>` : ""}</span><span class="at">atualizado ${esc(DADOS.atualizado)}</span></div>`;
  if (DADOS.progresso) h += `<div class="carregando" style="padding:12px 16px"><div class="spin"></div>Carregando os estados: ${DADOS.progresso.feitos} de ${DADOS.progresso.total}. As licitações vão aparecendo enquanto isso.</div>`;
  if (DADOS.erros.length) h += (DADOS.uf === "BR" || DADOS.uf === "MEUS")
    ? `<div class="aviso">O PNCP não respondeu para: <b>${esc(DADOS.erros.join(", "))}</b>. Clique em Buscar de novo daqui a alguns minutos (os estados que já vieram ficam guardados e voltam rápido).</div>`
    : `<div class="aviso">O PNCP não respondeu para: ${esc(DADOS.erros.join("; "))}. Tente buscar de novo em alguns minutos.</div>`;
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
  $("#res").innerHTML = `<div class="carregando"><div class="spin"></div>Buscando licitações abertas de ${esc(uf)} no PNCP. Leva alguns segundos.</div>`;
  try {
    const d = await buscarEstado(uf, PREF.mods);
    if (meu !== pedido) return;
    DADOS = d; desenhar();
  } catch (e) {
    if (meu === pedido) $("#res").innerHTML = `<div class="erro"><b>Não deu para buscar.</b> ${esc(e.message)}</div>`;
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
  MOSTRAR = 40; desenhar();
});
$("#uf").addEventListener("change", () => { VER_SALVAS = false; buscar(); });
$("#aba-op").addEventListener("click", () => { VER_SALVAS = !VER_SALVAS; window.scrollTo(0, 0); desenhar(); });
guardarSalvas();
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
  if (e.target.id === "voltar") { VER_SALVAS = false; desenhar(); return; }
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
        ${(x.categorias || []).filter((k) => k !== "Outros").map((k) => `<span class="tag cat">${esc(k)}</span>`).join("")}
        ${x.assistencia ? `<span class="tag assist">🔧 Exige assistência técnica</span>` : ""}
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
      if (SALVAS[x.id]) delete SALVAS[x.id];
      else { const y = Object.assign({}, x, {salvaEm: Date.now()}); delete y.complemento; SALVAS[x.id] = y; }
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
PAGINA = PAGINA.replace("__CONFIG_PNCP__", json.dumps({
    "categorias": CATEGORIAS, "servico": SERVICO, "produto": PRODUTO, "assistencia": ASSISTENCIA,
    "portais": [[n, list(a), list(b)] for n, a, b in PORTAIS_CONHECIDOS], "modalidades": MODALIDADES,
}, ensure_ascii=False))


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

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
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
            return self._send(200, PAGINA, "text/html; charset=utf-8")
        self._send(404, "não encontrado", "text/plain; charset=utf-8")


def main():
    na_nuvem = "PORT" in os.environ
    srv = ThreadingHTTPServer(("0.0.0.0" if na_nuvem else "127.0.0.1", PORT), Handler)
    print(f"Busca Licitações rodando na porta {PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
