#!/usr/bin/env python3
"""
Robô de licitações de TI - ISAKMP

Consulta a API pública do PNCP (Portal Nacional de Contratações Públicas),
lista as contratações com recebimento de propostas em aberto nas UFs
configuradas, filtra as de TI e grava docs/data.json para o painel.

Usa só a biblioteca padrão do Python (3.9+). Configuração em config.json.

Uso:
    python robo.py                      # coleta completa
    python robo.py --max-paginas 2      # teste rápido
    python robo.py --offline amostra.json   # classifica um arquivo local, sem rede
"""

import argparse
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("America/Sao_Paulo")
except Exception:  # pragma: no cover
    TZ = None

BASE = Path(__file__).resolve().parent
API_CONSULTA = "https://pncp.gov.br/api/consulta/v1/contratacoes/proposta"
API_ITENS = "https://pncp.gov.br/api/pncp/v1/orgaos/{cnpj}/compras/{ano}/{seq}/itens"
USER_AGENT = "robo-licitacoes-isakmp/1.0 (+https://isakmp.tech)"


# --------------------------------------------------------------------------
# utilitários
# --------------------------------------------------------------------------

def agora():
    return datetime.now(TZ).replace(tzinfo=None) if TZ else datetime.now()


def normalizar(texto):
    if not texto:
        return ""
    t = unicodedata.normalize("NFKD", str(texto))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t.lower()).strip()


def parse_data(valor):
    if not valor:
        return None
    s = str(valor).replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:26], fmt)
        except ValueError:
            continue
    return None


def log(msg):
    print(f"[{agora():%H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------
# classificação
# --------------------------------------------------------------------------

class Classificador:
    def __init__(self, cfg):
        self.minimo = cfg.get("pontuacao_minima", 3)
        self.categorias = {
            nome: [(re.compile(p), int(w)) for p, w in termos]
            for nome, termos in cfg["categorias"].items()
        }
        self.exclusoes = [(re.compile(p), int(w)) for p, w in cfg.get("exclusoes", [])]
        fabs = cfg.get("fabricantes_destaque", [])
        self.fabricantes = [(f, re.compile(r"\b" + re.escape(normalizar(f)) + r"\b")) for f in fabs]

    def classificar(self, objeto):
        texto = normalizar(objeto)
        pontos = 0
        cats = {}
        termos = []
        for nome, regras in self.categorias.items():
            melhor = 0
            for rx, peso in regras:
                m = rx.search(texto)
                if m:
                    termos.append(m.group(0))
                    melhor = max(melhor, peso)
                    pontos += peso
            if melhor:
                cats[nome] = melhor
        vetos = []
        for rx, peso in self.exclusoes:
            m = rx.search(texto)
            if m:
                pontos += peso
                vetos.append(m.group(0))
        fabricantes = [f for f, rx in self.fabricantes if rx.search(texto)]
        if fabricantes:
            pontos += 3
        # categoria principal: as de maior peso; empate mantém todas
        if cats:
            topo = max(cats.values())
            categorias = [c for c, w in cats.items() if w == topo]
        else:
            categorias = []
        return {
            "relevante": pontos >= self.minimo and bool(categorias or fabricantes),
            "pontos": pontos,
            "categorias": categorias,
            "termos": sorted(set(termos)),
            "vetos": vetos,
            "fabricantes": fabricantes,
        }


# --------------------------------------------------------------------------
# cliente da API
# --------------------------------------------------------------------------

class LimiteExcedido(Exception):
    pass


class ClientePNCP:
    def __init__(self, pausa=5.0, tentativas=5):
        self.pausa = pausa
        self.tentativas = tentativas
        self.chamadas = 0
        self._ultima = 0.0

    def get(self, url, params=None):
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        espera_429 = 60
        for tentativa in range(1, self.tentativas + 1):
            intervalo = time.time() - self._ultima
            if intervalo < self.pausa:
                time.sleep(self.pausa - intervalo)
            self._ultima = time.time()
            self.chamadas += 1
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    if r.status == 204:
                        return None
                    corpo = r.read()
                    if not corpo.strip():
                        return None
                    return json.loads(corpo.decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 204:
                    return None
                if e.code == 429:
                    log(f"  429 (limite de chamadas), aguardando {espera_429}s")
                    time.sleep(espera_429)
                    espera_429 = min(espera_429 * 2, 300)
                    continue
                if e.code in (500, 502, 503, 504):
                    log(f"  HTTP {e.code}, nova tentativa {tentativa}/{self.tentativas}")
                    time.sleep(15 * tentativa)
                    continue
                if e.code in (400, 404, 422):
                    # parâmetro rejeitado ou recurso inexistente: não adianta repetir
                    raise RuntimeError(f"HTTP {e.code} em {url}: {e.read()[:300]!r}")
                raise RuntimeError(f"HTTP {e.code} em {url}")
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                log(f"  erro de rede ({e}), nova tentativa {tentativa}/{self.tentativas}")
                time.sleep(15 * tentativa)
            except json.JSONDecodeError:
                log(f"  resposta não-JSON, nova tentativa {tentativa}/{self.tentativas}")
                time.sleep(30)
        raise LimiteExcedido(f"falhou após {self.tentativas} tentativas: {url}")


# --------------------------------------------------------------------------
# coleta
# --------------------------------------------------------------------------

def coletar(cfg, cliente, max_paginas=None):
    data_final = (agora() + timedelta(days=int(cfg.get("dias_a_frente", 365)))).strftime("%Y%m%d")
    registros = {}
    combos_ok, combos_falha, erros = [], [], []
    for uf in cfg["ufs"]:
        for cod, nome_mod in cfg["modalidades"].items():
            pagina, total_paginas = 1, None
            log(f"{uf} / {nome_mod}")
            try:
                while True:
                    params = {
                        "dataFinal": data_final,
                        "codigoModalidadeContratacao": cod,
                        "uf": uf,
                        "pagina": pagina,
                        "tamanhoPagina": 50,
                    }
                    resp = cliente.get(API_CONSULTA, params)
                    if not resp:
                        break
                    dados = resp.get("data") if isinstance(resp, dict) else resp
                    if not dados:
                        break
                    for reg in dados:
                        chave = reg.get("numeroControlePNCP") or f"{uf}-{cod}-{reg.get('id')}"
                        registros[chave] = reg
                    total_paginas = resp.get("totalPaginas") if isinstance(resp, dict) else None
                    restantes = resp.get("paginasRestantes") if isinstance(resp, dict) else None
                    if pagina == 1:
                        log(f"  {resp.get('totalRegistros', '?')} registros, {total_paginas or '?'} páginas")
                    if restantes == 0 or (total_paginas and pagina >= total_paginas):
                        break
                    if max_paginas and pagina >= max_paginas:
                        break
                    pagina += 1
                combos_ok.append(f"{uf}-{cod}")
            except Exception as e:  # segue para a próxima combinação
                log(f"  FALHA: {e}")
                combos_falha.append(f"{uf}-{cod}")
                erros.append(f"{uf} / {nome_mod}: {e}")
    return list(registros.values()), combos_ok, combos_falha, erros


def montar_item(reg, cls, hoje_iso):
    org = reg.get("orgaoEntidade") or {}
    uni = reg.get("unidadeOrgao") or {}
    cnpj = org.get("cnpj") or ""
    ano = reg.get("anoCompra")
    seq = reg.get("sequencialCompra")
    link_pncp = f"https://pncp.gov.br/app/editais/{cnpj}/{ano}/{seq}" if cnpj and ano and seq else None
    compl = normalizar(reg.get("informacaoComplementar"))
    texto_me = normalizar(reg.get("objetoCompra")) + " " + compl
    me_texto = bool(re.search(r"exclusiv\w*\s+(para\s+|a\s+|as\s+)?(participacao\s+(de\s+)?)?(me|epp|microempresas?|me/epp|me e epp)", texto_me))
    return {
        "id": reg.get("numeroControlePNCP"),
        "objeto": (reg.get("objetoCompra") or "").strip(),
        "orgao": (org.get("razaoSocial") or "").strip(),
        "unidade": (uni.get("nomeUnidade") or "").strip(),
        "uf": uni.get("ufSigla"),
        "municipio": uni.get("municipioNome"),
        "modalidade": reg.get("modalidadeNome"),
        "modalidade_id": reg.get("modalidadeId"),
        "modo_disputa": reg.get("modoDisputaNome"),
        "numero": reg.get("numeroCompra"),
        "processo": reg.get("processo"),
        "srp": bool(reg.get("srp")),
        "valor": reg.get("valorTotalEstimado"),
        "abertura": reg.get("dataAberturaProposta"),
        "encerramento": reg.get("dataEncerramentoProposta"),
        "publicacao": reg.get("dataPublicacaoPncp"),
        "categorias": cls["categorias"],
        "termos": cls["termos"],
        "fabricantes": cls["fabricantes"],
        "pontos": cls["pontos"],
        "me_epp_texto": me_texto,
        "beneficio": None,
        "link_pncp": link_pncp,
        "link_origem": reg.get("linkSistemaOrigem"),
        "_cnpj": cnpj,
        "_ano": ano,
        "_seq": seq,
        "primeiro_visto": hoje_iso,
    }


def buscar_beneficio(cliente, item):
    """Lê os itens da contratação e resume o benefício de ME/EPP (LC 123)."""
    if not (item.get("_cnpj") and item.get("_ano") and item.get("_seq")):
        return None
    url = API_ITENS.format(cnpj=item["_cnpj"], ano=item["_ano"], seq=item["_seq"])
    try:
        resp = cliente.get(url, {"pagina": 1, "tamanhoPagina": 500})
    except Exception as e:
        log(f"  itens de {item['id']}: {e}")
        return None
    itens = resp.get("data") if isinstance(resp, dict) else resp
    if not itens:
        return None
    exclusivos = cota = outros = 0
    for it in itens:
        nome = normalizar(it.get("tipoBeneficioNome") or "")
        cod = it.get("tipoBeneficioId", it.get("tipoBeneficio"))
        if isinstance(cod, dict):
            cod = cod.get("id")
        if "exclusiva" in nome or cod == 1:
            exclusivos += 1
        elif "cota" in nome or cod == 3:
            cota += 1
        else:
            outros += 1
    if exclusivos and not (cota or outros):
        return "Exclusiva ME/EPP"
    if exclusivos or cota:
        return "Parcial ME/EPP"
    return "Ampla"


# --------------------------------------------------------------------------
# principal
# --------------------------------------------------------------------------

def carregar_anterior(caminho):
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except Exception:
        return {"itens": []}


def main():
    ap = argparse.ArgumentParser(description="Robô de licitações de TI (PNCP)")
    ap.add_argument("--config", default=str(BASE / "config.json"))
    ap.add_argument("--saida", default=str(BASE / "docs" / "data.json"))
    ap.add_argument("--max-paginas", type=int, default=None, help="limita páginas por UF/modalidade (teste)")
    ap.add_argument("--offline", help="arquivo JSON com registros brutos do PNCP, sem acessar a rede")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    cls = Classificador(cfg)
    saida = Path(args.saida)
    anterior = carregar_anterior(saida)
    prev = {i["id"]: i for i in anterior.get("itens", []) if i.get("id")}

    inicio = agora()
    hoje_iso = inicio.strftime("%Y-%m-%d")
    cliente = ClientePNCP(pausa=float(cfg.get("pausa_entre_chamadas_seg", 5)))

    if args.offline:
        bruto = json.loads(Path(args.offline).read_text(encoding="utf-8"))
        registros = bruto.get("data", bruto) if isinstance(bruto, dict) else bruto
        combos_ok = [f"{uf}-{m}" for uf in cfg["ufs"] for m in cfg["modalidades"]]
        combos_falha, erros = [], []
    else:
        registros, combos_ok, combos_falha, erros = coletar(cfg, cliente, args.max_paginas)

    if not registros and not combos_ok:
        log("Nenhuma combinação UF/modalidade respondeu. Mantendo os dados anteriores.")
        anterior.setdefault("execucao", {})
        anterior["execucao"].update({"ultima_tentativa": inicio.isoformat(timespec="seconds"),
                                     "status": "falha", "erros": erros[:50]})
        saida.write_text(json.dumps(anterior, ensure_ascii=False, indent=1), encoding="utf-8")
        return 1

    itens = {}
    for reg in registros:
        enc = parse_data(reg.get("dataEncerramentoProposta"))
        if enc and enc < inicio:
            continue
        c = cls.classificar(reg.get("objetoCompra"))
        if not c["relevante"]:
            continue
        item = montar_item(reg, c, hoje_iso)
        if not item["id"]:
            continue
        antigo = prev.get(item["id"])
        if antigo:
            item["primeiro_visto"] = antigo.get("primeiro_visto", hoje_iso)
            item["beneficio"] = antigo.get("beneficio")
        itens[item["id"]] = item

    # combinações que falharam nesta rodada: preserva o que já se conhecia delas
    falhas = set(combos_falha)
    for pid, antigo in prev.items():
        if pid in itens:
            continue
        combo = f"{antigo.get('uf')}-{antigo.get('modalidade_id')}"
        enc = parse_data(antigo.get("encerramento"))
        if combo in falhas and (not enc or enc >= inicio):
            itens[pid] = antigo

    # benefício ME/EPP: só para quem ainda não tem
    if cfg.get("buscar_beneficio_me_epp", True) and not args.offline:
        pendentes = [i for i in itens.values() if i.get("beneficio") is None]
        log(f"Consultando benefício ME/EPP de {len(pendentes)} contratações")
        for n, item in enumerate(pendentes, 1):
            item["beneficio"] = buscar_beneficio(cliente, item)
            if n % 25 == 0:
                log(f"  {n}/{len(pendentes)}")

    lista = sorted(itens.values(), key=lambda i: (i.get("encerramento") or "9999"))
    fim = agora()
    resultado = {
        "atualizado_em": fim.isoformat(timespec="seconds"),
        "ufs": cfg["ufs"],
        "modalidades": list(cfg["modalidades"].values()),
        "valor_limite_me_epp": cfg.get("valor_limite_me_epp", 80000),
        "execucao": {
            "status": "ok" if not combos_falha else "parcial",
            "inicio": inicio.isoformat(timespec="seconds"),
            "duracao_min": round((fim - inicio).total_seconds() / 60, 1),
            "chamadas_api": cliente.chamadas,
            "registros_lidos": len(registros),
            "relevantes": len(lista),
            "combinacoes_ok": len(combos_ok),
            "combinacoes_falha": combos_falha,
            "erros": erros[:50],
        },
        "itens": lista,
    }
    saida.parent.mkdir(parents=True, exist_ok=True)
    saida.write_text(json.dumps(resultado, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"Concluído: {len(registros)} lidos, {len(lista)} de TI, {cliente.chamadas} chamadas, "
        f"{len(combos_falha)} combinações com falha")
    return 0


if __name__ == "__main__":
    sys.exit(main())
