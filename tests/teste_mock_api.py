"""Simula a API do PNCP (paginação, 204, 429, itens) para testar o fluxo de coleta."""
import json, sys, threading, tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import robo
amostra = json.load(open(Path(__file__).parent / "amostra_pncp.json"))["data"]
from datetime import datetime, timedelta
for _k, _r in enumerate(amostra):  # datas sempre no futuro, para o teste não envelhecer
    _r["dataEncerramentoProposta"] = (datetime.now() + timedelta(days=2 + _k)).strftime("%Y-%m-%dT10:00:00")
cont = {"429": 0}
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        if u.path.endswith("/itens"):
            seq = int(u.path.split("/")[-2])
            nome = "Participação exclusiva para ME/EPP" if seq % 3 == 0 else "Sem benefício"
            body = [{"numeroItem": 1, "tipoBeneficio": 1 if seq % 3 == 0 else 5, "tipoBeneficioNome": nome,
                     "orcamentoSigiloso": seq % 4 == 0, "valorUnitarioEstimado": 0 if seq % 4 == 0 else 10}]
            return self._json(body)
        uf, mod, pag = q["uf"][0], q["codigoModalidadeContratacao"][0], int(q["pagina"][0])
        assert "dataFinal" in q
        if uf == "ES" and mod == "7":
            self.send_response(500); self.end_headers(); return
        if uf == "MG" and mod == "6" and cont["429"] == 0:
            cont["429"] += 1; self.send_response(429); self.send_header("Content-Type","text/html"); self.end_headers(); self.wfile.write(b"<html>"); return
        regs = [r for r in amostra if r["unidadeOrgao"]["ufSigla"] == uf and str(r["modalidadeId"]) == mod]
        if not regs: self.send_response(204); self.end_headers(); return
        tam = 1; pags = len(regs)
        page = regs[(pag-1)*tam: pag*tam]
        self._json({"data": page, "totalRegistros": len(regs), "totalPaginas": pags, "numeroPagina": pag, "paginasRestantes": pags-pag, "empty": False})
    def _json(self, b):
        d = json.dumps(b).encode(); self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers(); self.wfile.write(d)
srv = HTTPServer(("127.0.0.1", 0), H); port = srv.server_port
threading.Thread(target=srv.serve_forever, daemon=True).start()
robo.API_CONSULTA = f"http://127.0.0.1:{port}/api/consulta/v1/contratacoes/proposta"
robo.API_ITENS = f"http://127.0.0.1:{port}/api/pncp/v1/orgaos/{{cnpj}}/compras/{{ano}}/{{seq}}/itens"
robo.time.sleep = lambda s: None  # acelera retries
saida = Path(tempfile.mkdtemp()) / "data.json"
sys.argv = ["robo.py", "--saida", str(saida)]
cfg = json.loads((Path(robo.BASE) / "config.json").read_text()); 
rc = robo.main()
d = json.loads(saida.read_text())
e = d["execucao"]
print(json.dumps(e, ensure_ascii=False, indent=1))
print({i["id"][-11:]: i["beneficio"] for i in d["itens"]})
assert rc == 0 and e["relevantes"] == 10 and e["registros_lidos"] == 12, "contagem"
assert e["combinacoes_falha"] == ["ES-7"], "falha ES-7 esperada"
assert all(i["beneficio"] in ("Exclusiva ME/EPP", "Ampla") for i in d["itens"])
sig = {i["id"]: i["orcamento_sigiloso"] for i in d["itens"]}
assert all(v in ("total", "nao") for v in sig.values()), sig
assert any(v == "total" for v in sig.values())
print("MOCK OK")
