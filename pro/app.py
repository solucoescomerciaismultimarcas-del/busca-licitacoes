"""SigaLicita Pro — site com login que junta as ferramentas do licitante.

Para rodar no computador:  python3 app.py   (abre em http://localhost:8000)
Variáveis de ambiente (no Render):
  PRO_ADMIN_EMAIL / PRO_ADMIN_SENHA / PRO_ADMIN_NOME  → cria a conta da administradora na primeira vez
  PRO_SEGREDO   → texto secreto para assinar o login (se faltar, todos saem a cada reinício)
  PRO_BANCO     → caminho do arquivo do banco (padrão: pro.db ao lado deste arquivo)
"""
import hashlib
import hmac
import html
import os
import secrets
import sqlite3
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PASTA = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8000"))
SEGREDO = (os.environ.get("PRO_SEGREDO") or secrets.token_hex(32)).encode()
BANCO = os.environ.get("PRO_BANCO", os.path.join(PASTA, "pro.db"))
DURACAO_LOGIN = 30 * 86400  # 30 dias
SIGALICITA = "https://sigalicita.com.br"
SIGASCORE = "https://score.sigalicita.com.br"

# ---------------------------------------------------------------- banco de dados
_trava = threading.Lock()


def banco():
    con = sqlite3.connect(BANCO)
    con.row_factory = sqlite3.Row
    return con


def criar_tabelas():
    with _trava, banco() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            nome TEXT NOT NULL,
            empresa TEXT DEFAULT '',
            senha TEXT NOT NULL,
            plano TEXT DEFAULT 'combo',
            admin INTEGER DEFAULT 0,
            ativo INTEGER DEFAULT 1,
            criado REAL NOT NULL)""")
        email = (os.environ.get("PRO_ADMIN_EMAIL") or "").strip().lower()
        senha = os.environ.get("PRO_ADMIN_SENHA") or ""
        if email and senha and not con.execute("SELECT 1 FROM usuarios WHERE email=?", (email,)).fetchone():
            con.execute("INSERT INTO usuarios (email, nome, empresa, senha, admin, criado) VALUES (?,?,?,?,1,?)",
                        (email, os.environ.get("PRO_ADMIN_NOME", "Administradora"),
                         os.environ.get("PRO_ADMIN_EMPRESA", ""), cifrar(senha), time.time()))


def cifrar(senha, sal=None):
    sal = sal or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", senha.encode(), sal.encode(), 200_000).hex()
    return f"{sal}${h}"


def senha_confere(senha, guardada):
    sal, _, _ = guardada.partition("$")
    return hmac.compare_digest(cifrar(senha, sal), guardada)


def usuario_por_email(email):
    with banco() as con:
        return con.execute("SELECT * FROM usuarios WHERE email=?", (email.strip().lower(),)).fetchone()


def usuario_por_id(uid):
    with banco() as con:
        return con.execute("SELECT * FROM usuarios WHERE id=?", (uid,)).fetchone()


# ---------------------------------------------------------------- login (cookie assinado)
def _assinar(texto):
    return hmac.new(SEGREDO, texto.encode(), hashlib.sha256).hexdigest()


def cookie_login(uid):
    valor = f"{uid}.{int(time.time()) + DURACAO_LOGIN}"
    valor = f"{valor}.{_assinar(valor)}"
    return f"pro_sessao={valor}; Path=/; Max-Age={DURACAO_LOGIN}; HttpOnly; SameSite=Lax" + ("; Secure" if os.environ.get("RENDER") else "")


def usuario_logado(handler):
    for parte in (handler.headers.get("Cookie") or "").split(";"):
        nome, _, valor = parte.strip().partition("=")
        if nome != "pro_sessao":
            continue
        uid, _, resto = valor.partition(".")
        validade, _, assinatura = resto.partition(".")
        if not hmac.compare_digest(_assinar(f"{uid}.{validade}"), assinatura):
            return None
        if not validade.isdigit() or int(validade) < time.time():
            return None
        u = usuario_por_id(int(uid)) if uid.isdigit() else None
        return u if u and u["ativo"] else None
    return None


# ---------------------------------------------------------------- páginas
def ler(nome):
    with open(os.path.join(PASTA, nome), encoding="utf-8") as f:
        return f.read()


def saudacao():
    hora = time.gmtime(time.time() - 3 * 3600).tm_hour  # horário de Brasília
    return "Bom dia" if 5 <= hora < 12 else "Boa tarde" if hora < 18 else "Boa noite"


PLANOS = {"combo": "Plano Combo", "pro": "Plano Pro", "teste": "Período de teste"}


def pagina_entrar(erro="", email=""):
    aviso = f'<div class="erro">{html.escape(erro)}</div>' if erro else ""
    return ler("entrar.html").replace("{{AVISO}}", aviso).replace("{{EMAIL}}", html.escape(email))


def pagina_inicio(u):
    primeiro = (u["nome"] or "").split(" ")[0] or "tudo bem"
    sub = " · ".join(x for x in [u["empresa"], PLANOS.get(u["plano"], "")] if x)
    return (ler("painel.html")
            .replace("{{SAUDACAO}}", html.escape(f"{saudacao()}, {primeiro}!"))
            .replace("{{SUB}}", html.escape(sub))
            .replace("{{SIGALICITA}}", SIGALICITA).replace("{{SIGASCORE}}", SIGASCORE))


ARQUIVOS = {"/logo.png": ("logo.png", "image/png"), "/logo-clara.png": ("logo-clara.png", "image/png"),
            "/estilo.css": ("estilo.css", "text/css; charset=utf-8"),
            "/tema.js": ("tema.js", "text/javascript; charset=utf-8")}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("  " + (fmt % args) + "\n")

    def _enviar(self, code, corpo, tipo="text/html; charset=utf-8", extra=None):
        dados = corpo if isinstance(corpo, bytes) else corpo.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(dados)))
        self.send_header("Cache-Control", "public, max-age=86400" if tipo.startswith("image/") else "no-store")
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(dados)

    def _ir(self, destino, cookie=None):
        extra = [("Location", destino)] + ([("Set-Cookie", cookie)] if cookie else [])
        self._enviar(303, "", extra=extra)

    def do_GET(self):
        caminho = urllib.parse.urlparse(self.path).path
        if caminho in ARQUIVOS:
            nome, tipo = ARQUIVOS[caminho]
            with open(os.path.join(PASTA, nome), "rb") as f:
                return self._enviar(200, f.read(), tipo)
        if caminho == "/saude":
            return self._enviar(200, "ok", "text/plain; charset=utf-8")
        if caminho == "/sair":
            return self._ir("/entrar", "pro_sessao=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
        u = usuario_logado(self)
        if caminho == "/entrar":
            return self._ir("/") if u else self._enviar(200, pagina_entrar())
        if not u:
            return self._ir("/entrar")
        if caminho == "/":
            return self._enviar(200, pagina_inicio(u))
        self._enviar(404, "Página não encontrada", "text/plain; charset=utf-8")

    def do_POST(self):
        caminho = urllib.parse.urlparse(self.path).path
        if caminho != "/entrar":
            return self._enviar(404, "", "text/plain; charset=utf-8")
        tamanho = min(int(self.headers.get("Content-Length") or 0), 10_000)
        campos = urllib.parse.parse_qs(self.rfile.read(tamanho).decode("utf-8", "replace"))
        email = (campos.get("email") or [""])[0].strip()
        senha = (campos.get("senha") or [""])[0]
        u = usuario_por_email(email) if email else None
        if not u or not senha_confere(senha, u["senha"]):
            time.sleep(1)  # atrasa quem tenta adivinhar senha
            return self._enviar(200, pagina_entrar("E-mail ou senha não conferem.", email))
        if not u["ativo"]:
            return self._enviar(200, pagina_entrar("Esta conta está bloqueada. Fale com o suporte.", email))
        self._ir("/", cookie_login(u["id"]))


def main():
    criar_tabelas()
    print(f"SigaLicita Pro em http://localhost:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
