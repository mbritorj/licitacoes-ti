#!/usr/bin/env python3
"""
Prospecção diária da ISAKMP: rascunhos de e-mail pedindo cadastro de fornecedor.

Lê o data.json do painel de licitações, escolhe as contratações de serviços de TI
publicadas nos últimos dias, baixa do PNCP o aviso / edital / termo de referência
de cada uma, procura nele o e-mail do setor de compras e gera um rascunho por
órgão (modelo C), citando o número da contratação e o objeto.

Nada é enviado. O resultado fica em rascunhos/AAAA-MM-DD/:
  - index.html  : lista para revisar, com botão que abre o rascunho no programa de e-mail
  - NN_<orgao>.eml : o mesmo rascunho em arquivo, para quem preferir abrir assim
E cada órgão escolhido entra em contatos.csv, para não ser repetido.

Uso (no Mac, na pasta do robô):
    python3 -m pip install --user pypdf          # uma vez, para ler PDF
    python3 prospeccao.py --dados https://<painel>/data.json
    python3 prospeccao.py --dados site/data.json --quantidade 5

Usa pncp.py (mesma pasta) para falar com o PNCP.
"""

import argparse
import csv
import html
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

import pncp
from pncp import log, normalizar, agora

BASE = Path(__file__).resolve().parent
MAX_BYTES = 25 * 1024 * 1024
RX_EMAIL = re.compile(r"[a-z0-9][a-z0-9._%+-]*@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}", re.I)
RX_BOM_LOCAL = re.compile(r"licit|compra|cpl|pregao|pregoeir|dispensa|contrat|suprim|cotac|aquisic|"
                          r"material|almox|cpcl|sal|dcl|dlc|cel|comissao|agente")
RX_RUIM_LOCAL = re.compile(r"ouvidor|protocolo|gabinete|imprensa|comunicacao|^rh|recursoshumanos|"
                           r"esic|sic@|noreply|no-reply|naoresponda|lgpd|dpo|encarregado|tributo|iptu")
RX_PUBLICO = re.compile(r"\.(gov|leg|jus|mil|mp|def|tc)\.br$|\.edu\.br$")
FEDERAL = re.compile(r"ministerio|comando d[aoe]|exercito|marinha|aeronautica|universidade federal|"
                     r"instituto federal|tribunal regional|justica federal|fundacao oswaldo|agencia nacional|"
                     r"conselho regional|conselho federal|instituto nacional|empresa brasileira|"
                     r"superintendencia regional|policia federal|policia rodoviaria federal|"
                     r"advocacia-geral|receita federal|ibge|ibama|icmbio|inss|funai|fiocruz|cnpq|capes|ebserh")
PRIORIDADE_DOC = [r"aviso", r"edital", r"termo de referencia|\btr\b", r"projeto basico", r"estudo tecnico|etp"]
PEQUENAS = {"de", "da", "do", "das", "dos", "e", "a", "o", "em", "na", "no", "para"}
SIGLAS = {"rj", "sp", "mg", "es", "ba", "df", "rs", "go", "ti", "tic", "uerj", "ufrj", "saae", "ipedf", "sesc"}

CAMPOS_CSV = ["data", "orgao_cnpj", "orgao", "unidade", "uf", "municipio", "contratacao", "id_pncp",
              "email", "outros_emails", "status", "observacoes"]


# --------------------------------------------------------------------------
# texto e nomes
# --------------------------------------------------------------------------

def titulo(texto):
    palavras = []
    for i, p in enumerate((texto or "").strip().split()):
        b = p.lower()
        if b in SIGLAS:
            palavras.append(b.upper())
        elif i and b in PEQUENAS:
            palavras.append(b)
        else:
            palavras.append(b[:1].upper() + b[1:])
    return " ".join(palavras)


def resumir_objeto(objeto, limite=220):
    t = re.sub(r"\s+", " ", (objeto or "")).strip().rstrip(".;:")
    if t.isupper():
        t = t.lower()
    t = t[:1].lower() + t[1:] if t[:2] not in ("TI", "PC") else t
    if len(t) > limite:
        t = t[:limite].rsplit(" ", 1)[0].rstrip(",;:") + "..."
    return t


def nome_contratacao(item):
    mod = (item.get("modalidade") or "Contratação").replace(" - ", " ").strip()
    mod = {"Dispensa": "Dispensa Eletrônica", "Pregão Eletrônico": "Pregão Eletrônico"}.get(mod, mod)
    numero = str(item.get("numero") or "").strip()
    ano = item.get("_ano") or ""
    if numero and "/" not in numero and ano:
        numero = f"{numero}/{ano}"
    return f"{mod} nº {numero}" if numero else mod


def eh_federal(item):
    return bool(FEDERAL.search(normalizar(f"{item.get('orgao')} {item.get('unidade')}")))


# --------------------------------------------------------------------------
# documentos e e-mails
# --------------------------------------------------------------------------

def ordenar_documentos(arquivos):
    def peso(d):
        t = normalizar(f"{d.get('tipo')} {d.get('titulo')}")
        for n, rx in enumerate(PRIORIDADE_DOC):
            if re.search(rx, t):
                return n
        return len(PRIORIDADE_DOC)
    return sorted(arquivos or [], key=peso)


def baixar(url, pausa_cliente):
    pausa_cliente.esperar()
    req = urllib.request.Request(url, headers={"User-Agent": pncp.USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as r:
        dados = r.read(MAX_BYTES + 1)
        nome = r.headers.get_filename() or urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]
    if len(dados) > MAX_BYTES:
        raise RuntimeError("arquivo acima de 25 MB")
    return dados, nome or ""


def texto_pdf(dados):
    try:
        from pypdf import PdfReader
        leitor = PdfReader(io.BytesIO(dados))
        return "\n".join((p.extract_text() or "") for p in leitor.pages[:80])
    except ImportError:
        pass
    except Exception as e:
        log(f"    pypdf não leu o PDF ({e}), tentando pdftotext")
    if shutil.which("pdftotext"):
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            f.write(dados)
            f.flush()
            r = subprocess.run(["pdftotext", "-l", "80", f.name, "-"], capture_output=True, timeout=120)
            return r.stdout.decode("utf-8", "ignore")
    log("    sem leitor de PDF: rode 'python3 -m pip install --user pypdf'")
    return ""


def texto_docx(dados):
    with zipfile.ZipFile(io.BytesIO(dados)) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    return re.sub(r"<[^>]+>", " ", xml.replace("</w:p>", "\n"))


def extrair_texto(dados, nome, profundidade=0):
    inicio = dados[:8]
    nome = nome.lower()
    if inicio.startswith(b"%PDF"):
        return texto_pdf(dados)
    if inicio.startswith(b"PK"):
        try:
            with zipfile.ZipFile(io.BytesIO(dados)) as z:
                if "word/document.xml" in z.namelist():
                    return texto_docx(dados)
                if profundidade:
                    return ""
                partes = []
                for n in z.namelist():
                    if n.lower().endswith((".pdf", ".docx")):
                        partes.append(extrair_texto(z.read(n), n, 1))
                return "\n".join(partes)
        except zipfile.BadZipFile:
            return ""
    if nome.endswith((".txt", ".html", ".htm")) or inicio.lstrip().startswith(b"<"):
        return dados.decode("utf-8", "ignore")
    return ""


def emails_do_texto(texto):
    # PDFs quebram e-mails em fim de linha ("licitacao@prefeitura.\nsp.gov.br")
    t = re.sub(r"(@[\w.-]*\.)\s*\n\s*", r"\1", texto or "")
    t = re.sub(r"\s*@\s*", "@", t)
    achados = []
    for m in RX_EMAIL.findall(t):
        e = m.lower().strip(".-_")
        if e not in achados:
            achados.append(e)
    return achados


def pontuar(email, plataformas):
    local, _, dominio = email.partition("@")
    if any(p in dominio for p in plataformas):
        return -100
    if "isakmp" in dominio or "q13" in dominio:
        return -100
    pontos = 0
    if RX_BOM_LOCAL.search(normalizar(local)):
        pontos += 5
    if RX_RUIM_LOCAL.search(normalizar(local)):
        pontos -= 4
    if RX_PUBLICO.search(dominio):
        pontos += 2
    if dominio in ("gmail.com", "hotmail.com", "outlook.com", "yahoo.com.br", "yahoo.com", "bol.com.br"):
        pontos -= 1   # comum em prefeitura pequena; aceito, mas fica atrás do institucional
    return pontos


class Pausa:
    def __init__(self, segundos):
        self.segundos = segundos
        self.ultima = 0.0

    def esperar(self):
        import time
        falta = self.segundos - (time.time() - self.ultima)
        if falta > 0:
            time.sleep(falta)
        self.ultima = time.time()


def achar_email(item, cliente, pausa, cfg):
    arquivos = item.get("arquivos")
    if arquivos is None:
        arquivos = pncp.consultar_arquivos(cliente, item) or []
    encontrados, fonte = [], None
    for doc in ordenar_documentos(arquivos)[: int(cfg.get("max_documentos_por_contratacao", 3))]:
        try:
            dados, nome = baixar(doc["url"], pausa)
        except Exception as e:
            log(f"    não baixou '{doc.get('titulo')}': {e}")
            continue
        achados = emails_do_texto(extrair_texto(dados, nome))
        novos = [e for e in achados if e not in encontrados]
        encontrados.extend(novos)
        if novos and fonte is None:
            fonte = doc
        plataformas = cfg.get("dominios_plataforma", [])
        if any(pontuar(e, plataformas) >= 5 for e in encontrados):
            break
    plataformas = cfg.get("dominios_plataforma", [])
    validos = sorted((e for e in encontrados if pontuar(e, plataformas) > -100),
                     key=lambda e: -pontuar(e, plataformas))
    return validos, fonte


# --------------------------------------------------------------------------
# rascunho
# --------------------------------------------------------------------------

def montar_rascunho(item, cfg):
    orgao = titulo(item.get("orgao"))
    unidade = titulo(item.get("unidade"))
    quem = orgao if not unidade or normalizar(unidade) in normalizar(orgao) else f"{unidade} ({orgao})"
    contratacao = nome_contratacao(item)
    federal = eh_federal(item)
    assunto = "Cadastro de fornecedor ISAKMP Tecnologia | contratações de TI"

    pedidos = []
    if not federal:
        pedidos.append("O cadastro da ISAKMP como fornecedora. Se houver formulário ou lista de "
                       "documentos, envio no mesmo dia.")
    elif cfg.get("sicaf_ativo"):
        pedidos.append("A ISAKMP está cadastrada no SICAF e pode ser consultada pelo CNPJ.")
    pedidos.append("A inclusão do nosso e-mail comercial na lista de empresas consultadas em pesquisas de "
                   "preços e dispensas de TI.")
    if len(pedidos) == 1:
        pedido_txt = "Escrevo para pedir a inclusão do nosso e-mail comercial na lista de empresas consultadas " \
                     "em pesquisas de preços e dispensas de TI."
        if federal and cfg.get("sicaf_ativo"):
            pedido_txt += " A ISAKMP está cadastrada no SICAF e pode ser consultada pelo CNPJ."
        bloco_pedidos = pedido_txt
    else:
        bloco_pedidos = "Escrevo para pedir duas coisas:\n\n" + "\n".join(f"{n}. {p}" for n, p in enumerate(pedidos, 1))

    servicos = "\n".join(f"- {s}" for s in cfg.get("servicos", []))
    corpo = (
        "Prezados,\n\n"
        f"A ISAKMP Tecnologia da Informação acompanha as contratações de TI publicadas no PNCP por {quem} e viu a "
        f"contratação {contratacao} (PNCP {item.get('id')}), referente a {resumir_objeto(item.get('objeto'))}. "
        f"{bloco_pedidos}\n\n"
        f"Podemos atender em:\n{servicos}\n\n"
        f"{cfg.get('frase_ampliacao', '')}\n\n"
        "Dados da empresa: CNPJ 47.995.974/0001-16, Rio de Janeiro/RJ, ME. Site: isakmp.tech.\n\n"
        f"Atenciosamente,\n\n{cfg.get('assinatura', '')}\n\n"
        "Se preferir não receber novos contatos da ISAKMP, basta responder com \"remover\".\n"
    )
    corpo = re.sub(r"\n{3,}", "\n\n", corpo)
    return assunto, corpo, contratacao, federal


def gravar_eml(caminho, remetente, para, assunto, corpo):
    msg = EmailMessage()
    msg["X-Unsent"] = "1"
    msg["From"] = remetente
    msg["To"] = para
    msg["Subject"] = assunto
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(corpo)
    caminho.write_bytes(bytes(msg))


def mailto(para, assunto, corpo):
    q = urllib.parse.urlencode({"subject": assunto, "body": corpo}, quote_via=urllib.parse.quote)
    return f"mailto:{urllib.parse.quote(para, safe='@.')}?{q}"


def gravar_html(caminho, linhas, dia):
    blocos = []
    for n, l in enumerate(linhas, 1):
        outros = ", ".join(html.escape(e) for e in l["outros"]) or "nenhum"
        fonte = (f'<a href="{html.escape(l["fonte_url"])}">{html.escape(l["fonte_titulo"])}</a>'
                 if l.get("fonte_url") else "não identificado")
        blocos.append(f"""
<section>
  <h2>{n}. {html.escape(l['orgao'])} <small>{html.escape(l['uf'] or '')}</small></h2>
  <p><b>Contratação:</b> <a href="{html.escape(l['link_pncp'] or '#')}">{html.escape(l['contratacao'])}</a>
     {'<span class="tag">federal</span>' if l['federal'] else ''}</p>
  <p><b>Para:</b> {html.escape(l['email'])} · <b>outros e-mails no documento:</b> {outros}</p>
  <p><b>E-mail encontrado em:</b> {fonte}</p>
  <p><a class="bt" href="{html.escape(l['mailto'])}">Abrir rascunho</a>
     <a class="bt sec" href="{html.escape(l['eml'])}">Arquivo .eml</a>
     <button onclick="navigator.clipboard.writeText(document.getElementById('t{n}').innerText)">Copiar texto</button></p>
  <details><summary>Ver texto</summary><pre id="t{n}">{html.escape(l['corpo'])}</pre></details>
</section>""")
    caminho.write_text(f"""<!doctype html><html lang="pt-br"><meta charset="utf-8">
<title>Prospecção ISAKMP {dia}</title>
<style>
body{{font:15px/1.5 -apple-system,Segoe UI,Arial,sans-serif;max-width:900px;margin:24px auto;padding:0 16px;color:#1d2b2f}}
h1{{font-size:22px}} h2{{font-size:17px;margin:0 0 6px}} small{{color:#667}}
section{{border:1px solid #d5dde0;border-radius:8px;padding:14px 16px;margin:14px 0}}
.bt,button{{display:inline-block;background:#0c4b53;color:#fff;border:0;border-radius:6px;padding:6px 12px;text-decoration:none;font-size:14px;cursor:pointer;margin-right:6px}}
.bt.sec,button{{background:#e8eef0;color:#0c4b53}} .tag{{background:#ffe3b0;border-radius:4px;padding:1px 6px;font-size:12px}}
pre{{white-space:pre-wrap;background:#f6f8f9;padding:10px;border-radius:6px}}
</style>
<h1>Prospecção ISAKMP, {dia}: {len(linhas)} rascunhos</h1>
<p>Revise cada um antes de enviar. Envie um a cada 10 a 15 minutos. Depois de enviar, marque o status em contatos.csv.</p>
{''.join(blocos)}
</html>""", encoding="utf-8")


# --------------------------------------------------------------------------
# histórico e seleção
# --------------------------------------------------------------------------

def ler_historico(caminho):
    if not caminho.exists():
        return []
    with caminho.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


def acrescentar_historico(caminho, linhas):
    novo = not caminho.exists()
    with caminho.open("a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS_CSV, delimiter=";")
        if novo:
            w.writeheader()
        for l in linhas:
            w.writerow({k: l.get(k, "") for k in CAMPOS_CSV})


def chave_orgao(cnpj, unidade):
    return f"{cnpj}|{normalizar(unidade)}"


def selecionar(itens, cfg, historico, hoje):
    corte_pub = (hoje - timedelta(days=int(cfg.get("dias_publicacao", 30)))).strftime("%Y-%m-%d")
    corte_rep = (hoje - timedelta(days=int(cfg.get("dias_sem_repetir_orgao", 120)))).strftime("%Y-%m-%d")
    bloqueados_email = {normalizar(h.get("email")) for h in historico if h.get("status", "").lower() == "remover"}
    bloqueados_cnpj = {h.get("orgao_cnpj") for h in historico if h.get("status", "").lower() == "remover"}
    recentes = {chave_orgao(h.get("orgao_cnpj"), h.get("unidade")) for h in historico
                if (h.get("data") or "") >= corte_rep and h.get("status", "").lower() != "descartado"}
    excluir = [re.compile(p) for p in cfg.get("excluir_orgaos", [])]
    cats = set(cfg.get("categorias", ["Serviço"]))
    prefer = cfg.get("ufs_preferidas", [])

    candidatos, vistos = [], set()
    for i in itens:
        if not cats.intersection(i.get("categorias") or []):
            continue
        if (i.get("publicacao") or "")[:10] < corte_pub:
            continue
        nome = normalizar(f"{i.get('orgao')} {i.get('unidade')}")
        if any(rx.search(nome) for rx in excluir):
            continue
        k = chave_orgao(i.get("_cnpj"), i.get("unidade"))
        if k in recentes or k in vistos or i.get("_cnpj") in bloqueados_cnpj:
            continue
        vistos.add(k)
        candidatos.append(i)
    candidatos.sort(key=lambda i: (i.get("uf") not in prefer, -(i.get("pontos") or 0),
                                   "".join(chr(255 - ord(c)) for c in (i.get("publicacao") or ""))))
    return candidatos, bloqueados_email


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Gera rascunhos de e-mail de prospecção a partir do painel")
    ap.add_argument("--config", default=str(BASE / "prospeccao.json"))
    ap.add_argument("--dados", help="data.json do painel (caminho ou URL); padrão: painel_url do config")
    ap.add_argument("--quantidade", type=int)
    ap.add_argument("--saida", default=str(BASE / "rascunhos"))
    ap.add_argument("--historico", default=str(BASE / "contatos.csv"))
    ap.add_argument("--pausa", type=float, default=5.0, help="segundos entre chamadas ao PNCP")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    fonte = args.dados or cfg.get("painel_url")
    if not fonte:
        sys.exit("Informe o data.json do painel: --dados <URL ou caminho> (ou painel_url em prospeccao.json)")
    dados = pncp.ler_json(fonte)
    if not dados or not dados.get("itens"):
        sys.exit(f"Não consegui ler itens de {fonte}")

    qtd = args.quantidade or int(cfg.get("quantidade", 10))
    hoje = agora()
    dia = hoje.strftime("%Y-%m-%d")
    historico_path = Path(args.historico)
    historico = ler_historico(historico_path)
    candidatos, bloqueados_email = selecionar(dados["itens"], cfg, historico, hoje)
    log(f"{len(candidatos)} contratações candidatas; meta: {qtd} rascunhos")

    pasta = Path(args.saida) / dia
    pasta.mkdir(parents=True, exist_ok=True)
    cliente = pncp.ClientePNCP(pausa=args.pausa)
    pausa = Pausa(args.pausa)
    escolhidos, sem_email, tentativas = [], [], 0
    usados = set()
    for item in candidatos:
        if len(escolhidos) >= qtd or tentativas >= int(cfg.get("max_tentativas", 40)):
            break
        tentativas += 1
        log(f"[{tentativas}] {item.get('orgao')} / {item.get('unidade')} ({item.get('uf')})")
        emails, doc = achar_email(item, cliente, pausa, cfg)
        emails = [e for e in emails if normalizar(e) not in bloqueados_email and e not in usados]
        if not emails:
            sem_email.append(item)
            log("    nenhum e-mail aproveitável")
            continue
        para = emails[0]
        usados.add(para)
        assunto, corpo, contratacao, federal = montar_rascunho(item, cfg)
        n = len(escolhidos) + 1
        slug = re.sub(r"[^a-z0-9]+", "_", normalizar(item.get("orgao")))[:40].strip("_")
        eml = pasta / f"{n:02d}_{slug}.eml"
        gravar_eml(eml, cfg.get("remetente", ""), para, assunto, corpo)
        escolhidos.append({
            "data": dia, "orgao_cnpj": item.get("_cnpj"), "orgao": titulo(item.get("orgao")),
            "unidade": titulo(item.get("unidade")), "uf": item.get("uf"), "municipio": item.get("municipio"),
            "contratacao": contratacao, "id_pncp": item.get("id"), "email": para,
            "outros_emails": ", ".join(emails[1:4]), "status": "rascunho", "observacoes": "",
            "outros": emails[1:4], "federal": federal, "corpo": corpo, "link_pncp": item.get("link_pncp"),
            "fonte_url": (doc or {}).get("url"), "fonte_titulo": (doc or {}).get("titulo") or "documento",
            "mailto": mailto(para, assunto, corpo), "eml": eml.name,
        })
        log(f"    {para}")

    if not escolhidos:
        sys.exit("Nenhum rascunho gerado: nenhum e-mail encontrado nos documentos das candidatas.")
    gravar_html(pasta / "index.html", escolhidos, dia)
    acrescentar_historico(historico_path, escolhidos)
    log(f"{len(escolhidos)} rascunhos em {pasta} ({len(sem_email)} contratações sem e-mail no documento)")
    log(f"Abra {pasta / 'index.html'} para revisar e enviar.")


if __name__ == "__main__":
    main()
