"""Simula a API do PNCP para testar coleta completa, coleta incremental,
queda para o endpoint de publicação e montagem do painel, sem acessar a rede."""
import json, sys, tempfile, threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
import pncp, coletor, painel

pncp.time.sleep = lambda s: None
FUT = lambda d: (datetime.now() + timedelta(days=d)).strftime("%Y-%m-%dT10:00:00")


def reg(n, uf, mod, objeto, enc_dias=5, situacao=1):
    return {"numeroControlePNCP": f"00000000000{n:03d}-1-{n:06d}/2026", "objetoCompra": objeto,
            "orgaoEntidade": {"cnpj": f"00000000000{n:03d}", "razaoSocial": f"Órgão {n}"},
            "unidadeOrgao": {"ufSigla": uf, "municipioNome": "Cidade", "nomeUnidade": "Unidade"},
            "anoCompra": 2026, "sequencialCompra": n, "modalidadeId": mod, "modalidadeNome": "Pregão - Eletrônico",
            "valorTotalEstimado": 1000 * n, "dataEncerramentoProposta": FUT(enc_dias),
            "dataPublicacaoPncp": FUT(-1), "situacaoCompraId": situacao, "ruido": "x" * 50}


ESTADO = {
    "proposta": [
        reg(1, "RJ", 6, "Aquisição de switches Cisco para a rede"),
        reg(2, "RJ", 6, "Aquisição de medicamentos"),
        reg(3, "ES", 8, "Aquisição de licenças Microsoft 365"),
        reg(4, "ES", 6, "Serviço de cabeamento estruturado"),
    ],
    "atualizacao": [],
    "publicacao": [],
    "recusar_atualizacao": False,
}
CHAMADAS = []


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        CHAMADAS.append(u.path)
        if u.path.endswith("/itens"):
            seq = int(u.path.split("/")[-2])
            return self._json([{"tipoBeneficioId": 1 if seq % 2 else 5,
                                "tipoBeneficioNome": "Exclusiva" if seq % 2 else "Sem benefício",
                                "orcamentoSigiloso": False}])
        ep = u.path.rsplit("/", 1)[-1]
        if ep == "atualizacao" and ESTADO["recusar_atualizacao"]:
            self.send_response(404); self.end_headers(); return
        if ep in ("atualizacao", "publicacao"):
            assert "dataInicial" in q and "dataFinal" in q
        uf, mod, pag = q["uf"][0], int(q["codigoModalidadeContratacao"][0]), int(q["pagina"][0])
        regs = [r for r in ESTADO[ep] if r["unidadeOrgao"]["ufSigla"] == uf and r["modalidadeId"] == mod]
        if not regs:
            self.send_response(204); self.end_headers(); return
        page = regs[pag - 1:pag]  # uma por página, para testar a paginação
        self._json({"data": page, "totalPaginas": len(regs), "paginasRestantes": len(regs) - pag})

    def _json(self, b):
        d = json.dumps(b).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(d)


srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
pncp.API = f"http://127.0.0.1:{srv.server_port}/api/consulta/v1"
pncp.API_ITENS = f"http://127.0.0.1:{srv.server_port}/api/pncp/v1/orgaos/{{cnpj}}/compras/{{ano}}/{{seq}}/itens"

tmp = Path(tempfile.mkdtemp())
cfg_coleta = tmp / "coleta.json"
cfg_coleta.write_text(json.dumps({"grupos": {"RJ-ES": ["RJ", "ES"]},
                                  "modalidades": {"6": "Pregão Eletrônico", "8": "Dispensa"},
                                  "pausa_entre_chamadas_seg": 0}))


def rodar(mod, *argv):
    sys.argv = ["x", *argv]
    return mod.main()


def ler(p):
    return pncp.ler_json(p)


# 1. primeira coleta: completa
assert rodar(coletor, "--grupo", "RJ-ES", "--config", str(cfg_coleta), "--base", f"{tmp}/b0/", "--saida", f"{tmp}/b1") == 0
b1 = ler(tmp / "b1" / "RJ-ES.json.gz")
assert b1["modo"] == "completa" and len(b1["registros"]) == 4, b1["modo"]
assert "ruido" not in b1["registros"][0], "registro deveria ser enxugado"

# 2. incremental: um novo, um revogado, um encerrado
ESTADO["atualizacao"] = [reg(5, "RJ", 6, "Aquisição de licenças Veeam Backup"),
                         reg(1, "RJ", 6, "Aquisição de switches Cisco para a rede", situacao=2),
                         reg(6, "ES", 8, "Aquisição de notebooks", enc_dias=-2)]
assert rodar(coletor, "--grupo", "RJ-ES", "--config", str(cfg_coleta), "--base", f"{tmp}/b1/", "--saida", f"{tmp}/b2") == 0
b2 = ler(tmp / "b2" / "RJ-ES.json.gz")
ids = {r["sequencialCompra"] for r in b2["registros"]}
assert b2["modo"] == "incremental" and b2["endpoint"] == "contratacoes/atualizacao", (b2["modo"], b2["endpoint"])
assert ids == {2, 3, 4, 5}, ids

# 3. endpoint de atualização recusado: usa o de publicação
ESTADO["recusar_atualizacao"] = True
ESTADO["publicacao"] = [reg(7, "ES", 6, "Aquisição de toner e material de expediente")]
assert rodar(coletor, "--grupo", "RJ-ES", "--config", str(cfg_coleta), "--base", f"{tmp}/b2/", "--saida", f"{tmp}/b3") == 0
b3 = ler(tmp / "b3" / "RJ-ES.json.gz")
assert b3["endpoint"] == "contratacoes/publicacao" and 7 in {r["sequencialCompra"] for r in b3["registros"]}

# 4. painel da ISAKMP com consulta de itens e publicação dos brutos
assert rodar(painel, "--bruto", f"{tmp}/b3", "--grupos", "RJ-ES", "--site", f"{tmp}/site", "--publicar-bruto") == 0
d = json.loads((tmp / "site" / "data.json").read_text())
seqs = {i["_seq"]: i for i in d["itens"]}
assert set(seqs) == {3, 4, 5}, set(seqs)          # medicamentos e toner ficam de fora
assert seqs[3]["beneficio"] == "Exclusiva ME/EPP" and seqs[4]["beneficio"] == "Ampla"
assert (tmp / "site" / "index.html").exists() and (tmp / "site" / "bruto" / "RJ-ES.json.gz").exists()
assert ler(tmp / "site" / "bruto" / "indice.json")["grupos"][0]["grupo"] == "RJ-ES"

# 5. painel lendo só da URL publicada (como o painel da Blue Belt faz), sem consultar itens
srv_site = HTTPServer(("127.0.0.1", 0), type("S", (BaseHTTPRequestHandler,), {
    "log_message": lambda *a: None,
    "do_GET": lambda self: (lambda p: (self.send_response(200 if p.exists() else 404), self.end_headers(),
                                       p.exists() and self.wfile.write(p.read_bytes())))(
        tmp / "site" / urlparse(self.path).path.lstrip("/"))}))
threading.Thread(target=srv_site.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv_site.server_port}/"
assert rodar(painel, "--bruto-url", f"{url}bruto/", "--grupos", "RJ-ES", "--anterior", f"{url}data.json",
             "--site", f"{tmp}/site2", "--offline") == 0
d2 = json.loads((tmp / "site2" / "data.json").read_text())
assert {i["_seq"] for i in d2["itens"]} == {3, 4, 5}
assert d2["itens"][0]["beneficio"] is not None, "benefício já consultado deveria ser mantido"
assert d2["coleta"][0]["origem"] == "remota"

print("FLUXO OK")
