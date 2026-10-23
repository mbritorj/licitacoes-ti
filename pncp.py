"""
Biblioteca comum dos robôs de licitação (coletor e painéis).

Usa só a biblioteca padrão do Python (3.9+). O mesmo arquivo é usado nos
repositórios licitacoes-ti (ISAKMP) e licitacoes-bluebelt; ao alterar aqui,
copie para o outro repositório.
"""

import gzip
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("America/Sao_Paulo")
except Exception:  # pragma: no cover
    TZ = None

API = "https://pncp.gov.br/api/consulta/v1"
API_ITENS = "https://pncp.gov.br/api/pncp/v1/orgaos/{cnpj}/compras/{ano}/{seq}/itens"
USER_AGENT = "robo-licitacoes/2.0 (consulta publica; github.com/mbritorj)"

REGIOES = {
    "Norte": ["AC", "AM", "AP", "PA", "RO", "RR", "TO"],
    "Nordeste": ["AL", "BA", "CE", "MA", "PB", "PE", "PI", "RN", "SE"],
    "Centro-Oeste": ["DF", "GO", "MS", "MT"],
    "Sudeste": ["ES", "MG", "RJ", "SP"],
    "Sul": ["PR", "RS", "SC"],
}
TODAS_UFS = sorted(uf for ufs in REGIOES.values() for uf in ufs)

# situacaoCompraId do PNCP: 1 divulgada, 2 revogada, 3 anulada, 4 suspensa
SITUACOES_ENCERRADAS = {2, 3, 4}


# --------------------------------------------------------------------------
# utilitários
# --------------------------------------------------------------------------

def agora():
    return datetime.now(TZ).replace(tzinfo=None) if TZ else datetime.now()


def log(msg):
    print(f"[{agora():%H:%M:%S}] {msg}", flush=True)


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


def ler_json(origem, timeout=60):
    """Lê JSON (ou JSON gzip) de um caminho local ou de uma URL.
    Devolve None se não existir ou não puder ser lido."""
    if not origem:
        return None
    try:
        if str(origem).startswith(("http://", "https://")):
            sep = "&" if "?" in origem else "?"
            req = urllib.request.Request(f"{origem}{sep}t={int(time.time())}",
                                         headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                dados = r.read()
        else:
            p = Path(origem)
            if not p.exists():
                return None
            dados = p.read_bytes()
        if dados[:2] == b"\x1f\x8b":
            dados = gzip.decompress(dados)
        return json.loads(dados.decode("utf-8"))
    except Exception as e:
        log(f"  não foi possível ler {origem}: {e}")
        return None


def gravar_json(caminho, dados, comprimir=False):
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    corpo = json.dumps(dados, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if comprimir:
        corpo = gzip.compress(corpo, mtime=0)
    caminho.write_bytes(corpo)
    return len(corpo)


# --------------------------------------------------------------------------
# cliente da API
# --------------------------------------------------------------------------

class LimiteExcedido(Exception):
    pass


class ParametroRejeitado(RuntimeError):
    pass


class ClientePNCP:
    """Faz GET com pausa mínima entre chamadas e novas tentativas.
    A pausa existe para respeitar o limite do PNCP (cerca de 12 chamadas por minuto)."""

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
                if e.code in (400, 404, 405, 422):
                    raise ParametroRejeitado(f"HTTP {e.code} em {url}: {e.read()[:300]!r}")
                raise RuntimeError(f"HTTP {e.code} em {url}")
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                log(f"  erro de rede ({e}), nova tentativa {tentativa}/{self.tentativas}")
                time.sleep(15 * tentativa)
            except json.JSONDecodeError:
                log(f"  resposta não-JSON, nova tentativa {tentativa}/{self.tentativas}")
                time.sleep(30)
        raise LimiteExcedido(f"falhou após {self.tentativas} tentativas: {url}")

    def paginar(self, endpoint, params, max_paginas=None):
        """Percorre todas as páginas de um endpoint de consulta e devolve a lista de registros."""
        pagina, saida = 1, []
        while True:
            resp = self.get(f"{API}/{endpoint}", {**params, "pagina": pagina, "tamanhoPagina": 50})
            if not resp:
                break
            dados = resp.get("data") if isinstance(resp, dict) else resp
            if not dados:
                break
            saida.extend(dados)
            total = resp.get("totalPaginas") if isinstance(resp, dict) else None
            restantes = resp.get("paginasRestantes") if isinstance(resp, dict) else None
            if restantes == 0 or (total and pagina >= total):
                break
            if max_paginas and pagina >= max_paginas:
                break
            pagina += 1
        return saida


# campos do registro do PNCP que o painel usa; o resto é descartado para o arquivo ficar leve
CAMPOS = ["numeroControlePNCP", "objetoCompra", "informacaoComplementar", "anoCompra", "sequencialCompra",
          "modalidadeNome", "modalidadeId", "modoDisputaNome", "numeroCompra", "processo", "srp",
          "valorTotalEstimado", "dataAberturaProposta", "dataEncerramentoProposta", "dataPublicacaoPncp",
          "dataAtualizacaoGlobal", "dataAtualizacao", "linkSistemaOrigem", "situacaoCompraId"]


def enxugar(reg):
    r = {k: reg.get(k) for k in CAMPOS if reg.get(k) not in (None, "")}
    if r.get("informacaoComplementar"):
        r["informacaoComplementar"] = str(r["informacaoComplementar"])[:600]
    org = reg.get("orgaoEntidade") or {}
    uni = reg.get("unidadeOrgao") or {}
    r["orgaoEntidade"] = {"cnpj": org.get("cnpj"), "razaoSocial": org.get("razaoSocial")}
    r["unidadeOrgao"] = {"ufSigla": uni.get("ufSigla"), "municipioNome": uni.get("municipioNome"),
                         "nomeUnidade": uni.get("nomeUnidade")}
    return r


def ainda_aberta(reg, referencia):
    if reg.get("situacaoCompraId") in SITUACOES_ENCERRADAS:
        return False
    enc = parse_data(reg.get("dataEncerramentoProposta"))
    return not (enc and enc < referencia)


# --------------------------------------------------------------------------
# classificação
# --------------------------------------------------------------------------

class Classificador:
    def __init__(self, cfg):
        self.cfg = cfg
        self.minimo = cfg.get("pontuacao_minima", 3)
        self.categorias = {
            nome: [(re.compile(p), int(w)) for p, w in termos]
            for nome, termos in cfg["categorias"].items()
        }
        self.exclusoes = [(re.compile(p), int(w)) for p, w in cfg.get("exclusoes", [])]
        fabs = cfg.get("fabricantes_destaque", [])
        self.fabricantes = [(f, re.compile(r"\b" + re.escape(normalizar(f)) + r"\b")) for f in fabs]
        self.rx_servico = re.compile(cfg["padrao_servico"]) if cfg.get("padrao_servico") else None
        self.rx_fornecimento = re.compile(cfg["padrao_fornecimento"]) if cfg.get("padrao_fornecimento") else None
        self.cats_material = set(cfg.get("categorias_material", []))

    def classificar(self, objeto, uf=None):
        """Pontua o objeto. Com `uf`, aplica também as regras de abrangência por UF
        (ufs_por_categoria, ufs_foco) e a regra de serviço x fornecimento."""
        texto = normalizar(objeto)
        cats, pontos_cat, termos = {}, {}, []
        for nome, regras in self.categorias.items():
            for rx, peso in regras:
                m = rx.search(texto)
                if m:
                    termos.append(m.group(0))
                    cats[nome] = max(cats.get(nome, 0), peso)
                    pontos_cat[nome] = pontos_cat.get(nome, 0) + peso
        vetos, pontos_veto = [], 0
        for rx, peso in self.exclusoes:
            m = rx.search(texto)
            if m:
                pontos_veto += peso
                vetos.append(m.group(0))
        fabricantes = [f for f, rx in self.fabricantes if rx.search(texto)]

        # objeto de prestação de serviço: categorias de material não contam,
        # a menos que o texto também fale em fornecimento/aquisição de bens
        servico = bool(self.rx_servico and self.rx_servico.search(texto)
                       and not (self.rx_fornecimento and self.rx_fornecimento.search(texto)))
        if servico and self.cats_material:
            for c in list(cats):
                if c in self.cats_material:
                    cats.pop(c)
                    pontos_cat.pop(c)

        fora_uf = []
        if uf:
            regras = self.cfg.get("ufs_por_categoria", {})
            for c in list(cats):
                if c in regras and uf not in regras[c]:
                    fora_uf.append(c)
                    cats.pop(c)
                    pontos_cat.pop(c)
            foco = self.cfg.get("ufs_foco")
            if fabricantes and not cats and foco and uf not in foco:
                fabricantes = []

        pontos = sum(pontos_cat.values()) + pontos_veto + (3 if fabricantes else 0)
        if cats:
            topo = max(cats.values())
            categorias = [c for c, w in cats.items() if w == topo]
        else:
            categorias = []
        return {
            "relevante": pontos >= self.minimo and bool(categorias or fabricantes),
            "pontos": pontos,
            "categorias": categorias,
            "todas": sorted(cats, key=lambda c: -cats[c]),
            "termos": sorted(set(termos)),
            "vetos": vetos,
            "fabricantes": fabricantes,
            "servico": servico,
            "fora_uf": fora_uf,
        }


def alertas_do_item(item, cfg):
    alertas = []
    for cat, texto in cfg.get("alertas_por_categoria", {}).items():
        if cat in item["categorias"] and texto not in alertas:
            alertas.append(texto)
    texto_obj = normalizar(item["objeto"])
    for padrao, texto in cfg.get("alertas_por_termo", []):
        if re.search(padrao, texto_obj) and texto not in alertas:
            alertas.append(texto)
    base = cfg.get("uf_sede")
    if base and item.get("uf") and item["uf"] != base:
        alertas.append(f"Entrega fora do {base}: considere frete e prazo")
    return alertas


def montar_item(reg, cls, hoje_iso):
    org = reg.get("orgaoEntidade") or {}
    uni = reg.get("unidadeOrgao") or {}
    cnpj = org.get("cnpj") or ""
    ano = reg.get("anoCompra")
    seq = reg.get("sequencialCompra")
    link_pncp = f"https://pncp.gov.br/app/editais/{cnpj}/{ano}/{seq}" if cnpj and ano and seq else None
    texto_me = normalizar(reg.get("objetoCompra")) + " " + normalizar(reg.get("informacaoComplementar"))
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
        "orcamento_sigiloso": None,
        "link_pncp": link_pncp,
        "link_origem": reg.get("linkSistemaOrigem"),
        "_cnpj": cnpj,
        "_ano": ano,
        "_seq": seq,
        "primeiro_visto": hoje_iso,
    }


def consultar_itens(cliente, item):
    """Lê os itens da contratação no PNCP e resume:
    - benefício de ME/EPP (LC 123): "Exclusiva ME/EPP", "Parcial ME/EPP" ou "Ampla";
    - orçamento sigiloso (art. 24 da Lei 14.133): "total", "parcial" ou "nao".
    Devolve (beneficio, sigiloso); None em cada um quando não foi possível ler."""
    if not (item.get("_cnpj") and item.get("_ano") and item.get("_seq")):
        return None, None
    url = API_ITENS.format(cnpj=item["_cnpj"], ano=item["_ano"], seq=item["_seq"])
    try:
        resp = cliente.get(url, {"pagina": 1, "tamanhoPagina": 500})
    except Exception as e:
        log(f"  itens de {item['id']}: {e}")
        return None, None
    itens = resp.get("data") if isinstance(resp, dict) else resp
    if not itens:
        return None, None
    exclusivos = cota = outros = 0
    sig_sim = sig_nao = 0
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
        if it.get("orcamentoSigiloso") is True:
            sig_sim += 1
        else:
            sig_nao += 1
    if exclusivos and not (cota or outros):
        beneficio = "Exclusiva ME/EPP"
    elif exclusivos or cota:
        beneficio = "Parcial ME/EPP"
    else:
        beneficio = "Ampla"
    sigiloso = "total" if sig_sim and not sig_nao else ("parcial" if sig_sim else "nao")
    return beneficio, sigiloso
