import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from robo import Classificador
cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text())
c = Classificador(cfg)
DEVE_ENTRAR = [
 ("Contratação de empresa especializada para prestação de serviços de suporte técnico e manutenção dos switches Cisco", "Serviço"),
 ("Registro de preços para aquisição de switches, roteadores e access points", "Equipamento"),
 ("Aquisição de licenças de uso do Microsoft 365 E3", "Licença/Software"),
 ("Contratação de serviços de cabeamento estruturado e rede lógica no prédio sede", "Serviço"),
 ("Aquisição de computadores para a rede municipal de ensino", "Equipamento"),
 ("Prestação de serviço de link de internet dedicado com fibra óptica", "Serviço"),
 ("Renovação de subscrição de firewall Fortinet", None),
 ("Aquisição de material permanente de informática (notebooks e desktops)", "Equipamento"),
 ("Contratação de solução de backup em nuvem", None),
 ("Serviços de manutenção preventiva e corretiva de equipamentos de informática", "Serviço"),
 ("Aquisição de servidores de rack e storage para o data center", None),
 ("Contratação de empresa para prestação de serviços de telefonia IP (VoIP)", "Serviço"),
 ("Aquisição de nobreaks e racks para a sala de servidores e switch de núcleo", None),
 ("Prestação de serviços de suporte técnico aos usuários de Tecnologia da Informação", "Serviço"),
 ("Contratação de licença da plataforma Burp Suite Professional por 60 meses", "Licença/Software"),
 ("Aquisição de licenças perpétuas dos softwares ZENworks", "Licença/Software"),
]
NAO_DEVE = [
 "Aquisição de medicamentos para a rede municipal de saúde",
 "Capacitação de servidores públicos municipais em gestão",
 "Obras de pavimentação e rede de esgoto no bairro",
 "Aquisição de gêneros alimentícios para merenda escolar",
 "Manutenção de rede elétrica e iluminação pública",
 "Contratação de serviços de limpeza e conservação",
 "Aquisição de pneus para a frota municipal",
 "Credenciamento de rede credenciada de farmácias",
 "Aquisição de ar condicionado para salas administrativas",
 "Manutenção preventiva de elevadores",
 "Aquisição de mobiliário escolar",
 "Serviço de transporte escolar",
 "Aquisição de relógio de ponto eletrônico com reconhecimento facial e software",
 "Locação de contêineres com suporte técnico e transporte",
 "Manutenção preventiva, corretiva e suporte técnico dos sistemas de áudio e vídeo do Plenário",
 "Monitoramento e rastreamento de veículos com software próprio",
]
falhas = 0
for txt, cat in DEVE_ENTRAR:
    r = c.classificar(txt)
    ok = r["relevante"] and (cat is None or cat in r["categorias"])
    falhas += not ok
    print(("OK  " if ok else "FALHA"), r["pontos"], r["categorias"], r["fabricantes"], "|", txt[:70])
for txt in NAO_DEVE:
    r = c.classificar(txt)
    ok = not r["relevante"]
    falhas += not ok
    print(("OK  " if ok else "FALHA"), r["pontos"], r["termos"], "|", txt[:70])
print("falhas:", falhas); sys.exit(1 if falhas else 0)
