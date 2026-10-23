#!/usr/bin/env python3
"""
Monta o painel a partir da coleta compartilhada do PNCP.

Lê os arquivos <grupo>.json.gz (pasta local da coleta que acabou de rodar e/ou a
URL onde a última coleta foi publicada), classifica as contratações com o
config.json deste repositório, consulta os itens no PNCP (ME/EPP e orçamento
sigiloso) e gera a pasta do site: index.html, data.json e, se pedido, a cópia
dos arquivos brutos para outros painéis usarem.

Uso:
    python painel.py --bruto bruto/ --bruto-url https://.../bruto/ --anterior https://.../data.json --site site/
    python painel.py --bruto bruto/ --site site/ --offline        # sem consultar itens
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

import pncp
from pncp import log, agora

BASE = Path(__file__).resolve().parent


def listar_grupos(args):
    if args.grupos:
        return [g.strip() for g in args.grupos.split(",") if g.strip()]
    coleta = BASE / "coleta.json"
    if coleta.exists():
        return list(json.loads(coleta.read_text(encoding="utf-8"))["grupos"])
    if args.bruto_url:
        indice = pncp.ler_json(f"{args.bruto_url}indice.json")
        if indice:
            return [g["grupo"] for g in indice.get("grupos", [])]
    if args.bruto:
        return sorted(p.name.split(".")[0] for p in Path(args.bruto).glob("*.json.gz"))
    return []


def carregar_bases(args, grupos):
    bases, situacao = {}, []
    for g in grupos:
        base, origem = None, "ausente"
        if args.bruto:
            nova = pncp.ler_json(Path(args.bruto) / f"{g}.json.gz")
            if nova and nova.get("status") != "falha":
                base, origem = nova, "nova"
        if base is None and args.bruto_url:
            ant = pncp.ler_json(f"{args.bruto_url}{g}.json.gz")
            if ant:
                base, origem = ant, ("remota" if not args.bruto else "anterior")
        if base:
            bases[g] = base
        situacao.append({
            "grupo": g,
            "ufs": (base or {}).get("ufs", []),
            "origem": origem,
            "modo": (base or {}).get("modo"),
            "status": (base or {}).get("status", "falha"),
            "gerado_em": (base or {}).get("gerado_em"),
            "ultima_sincronizacao": (base or {}).get("ultima_sincronizacao"),
            "registros": len((base or {}).get("registros", [])),
            "erros": (base or {}).get("erros", [])[:10],
        })
        log(f"{g}: {origem}, {situacao[-1]['registros']} registros, coletado em {situacao[-1]['gerado_em']}")
    return bases, situacao


def main():
    ap = argparse.ArgumentParser(description="Gera o painel de licitações a partir da coleta compartilhada")
    ap.add_argument("--config", default=str(BASE / "config.json"))
    ap.add_argument("--bruto", help="pasta com os arquivos da coleta desta execução")
    ap.add_argument("--bruto-url", help="URL (termina em /) dos arquivos publicados da última coleta")
    ap.add_argument("--grupos", help="lista de grupos separada por vírgula (padrão: coleta.json ou indice.json)")
    ap.add_argument("--anterior", help="data.json anterior (caminho ou URL), para manter histórico e itens já consultados")
    ap.add_argument("--site", default=str(BASE / "site"))
    ap.add_argument("--publicar-bruto", action="store_true", help="copia os arquivos brutos para <site>/bruto/")
    ap.add_argument("--offline", action="store_true", help="não consulta os itens no PNCP")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if args.bruto_url and not args.bruto_url.endswith("/"):
        args.bruto_url += "/"
    if not args.bruto_url and cfg.get("fonte_bruto"):
        args.bruto_url = cfg["fonte_bruto"]
    cls = pncp.Classificador(cfg)
    site = Path(args.site)
    inicio = agora()
    hoje_iso = inicio.strftime("%Y-%m-%d")

    anterior = pncp.ler_json(args.anterior) or {"itens": []}
    prev = {i["id"]: i for i in anterior.get("itens", []) if i.get("id")}

    grupos = listar_grupos(args)
    bases, situacao = carregar_bases(args, grupos)
    if not bases:
        log("Nenhuma coleta disponível. Mantendo o painel anterior.")
        anterior.setdefault("execucao", {}).update({"status": "falha", "ultima_tentativa": inicio.isoformat(timespec="seconds")})
        dados = anterior
        itens_lista = anterior.get("itens", [])
    else:
        ufs_cfg = cfg.get("ufs", "todas")
        ufs_validas = set(pncp.TODAS_UFS if ufs_cfg == "todas" else ufs_cfg)
        registros = {}
        for b in bases.values():
            for r in b.get("registros", []):
                if r.get("numeroControlePNCP"):
                    registros[r["numeroControlePNCP"]] = r

        itens, lidos = {}, 0
        for reg in registros.values():
            uf = (reg.get("unidadeOrgao") or {}).get("ufSigla")
            if uf not in ufs_validas or not pncp.ainda_aberta(reg, inicio):
                continue
            lidos += 1
            c = cls.classificar(reg.get("objetoCompra"), uf)
            if not c["relevante"]:
                continue
            item = pncp.montar_item(reg, c, hoje_iso)
            item["alertas"] = pncp.alertas_do_item(item, cfg)
            antigo = prev.get(item["id"])
            if antigo:
                item["primeiro_visto"] = antigo.get("primeiro_visto", hoje_iso)
                item["beneficio"] = antigo.get("beneficio")
                item["orcamento_sigiloso"] = antigo.get("orcamento_sigiloso")
            itens[item["id"]] = item

        cliente = pncp.ClientePNCP(pausa=float(cfg.get("pausa_entre_chamadas_seg", 5)))
        if cfg.get("buscar_beneficio_me_epp", True) and not args.offline:
            pendentes = [i for i in itens.values()
                         if i.get("beneficio") is None
                         or (i.get("orcamento_sigiloso") is None and not i.get("valor"))]
            pendentes.sort(key=lambda i: i.get("encerramento") or "9999")
            limite = int(cfg.get("max_consultas_itens", 0) or 0)
            total_pend = len(pendentes)
            if limite and total_pend > limite:
                pendentes = pendentes[:limite]
            log(f"Consultando itens de {len(pendentes)} de {total_pend} contratações pendentes")
            for n, item in enumerate(pendentes, 1):
                beneficio, sigiloso = pncp.consultar_itens(cliente, item)
                if beneficio is not None:
                    item["beneficio"] = beneficio
                if sigiloso is not None:
                    item["orcamento_sigiloso"] = sigiloso
                if n % 50 == 0:
                    log(f"  {n}/{len(pendentes)}")

        itens_lista = sorted(itens.values(), key=lambda i: (i.get("encerramento") or "9999"))
        problemas = [s for s in situacao if s["origem"] not in ("nova", "remota") or s["status"] != "ok"]
        fim = agora()
        dados = {
            "atualizado_em": fim.isoformat(timespec="seconds"),
            "ufs": sorted(ufs_validas),
            "descricao_escopo": cfg.get("descricao_escopo"),
            "modalidades": sorted({i["modalidade"] for i in itens_lista if i.get("modalidade")}),
            "valor_limite_me_epp": cfg.get("valor_limite_me_epp", 80000),
            "categorias": list(cfg["categorias"].keys()),
            "ufs_por_categoria": cfg.get("ufs_por_categoria", {}),
            "coleta": situacao,
            "execucao": {
                "status": "ok" if not problemas else "parcial",
                "inicio": inicio.isoformat(timespec="seconds"),
                "duracao_min": round((fim - inicio).total_seconds() / 60, 1),
                "chamadas_api": cliente.chamadas,
                "registros_lidos": lidos,
                "relevantes": len(itens_lista),
                "grupos_com_problema": [s["grupo"] for s in problemas],
                "erros": [f"{s['grupo']}: coleta {s['origem']}" + (f", {s['status']}" if s["status"] != "ok" else "")
                          + (f" ({'; '.join(s['erros'][:2])})" if s["erros"] else "")
                          for s in problemas][:50],
            },
            "itens": itens_lista,
        }
        log(f"Concluído: {lidos} contratações no escopo, {len(itens_lista)} relevantes, "
            f"{cliente.chamadas} chamadas")

    # site: arquivos estáticos de docs/ + data.json gerado
    site.mkdir(parents=True, exist_ok=True)
    docs = BASE / "docs"
    for p in docs.iterdir():
        if p.is_file() and p.name != "data.json":
            shutil.copy2(p, site / p.name)
    tam = pncp.gravar_json(site / "data.json", dados)
    log(f"data.json: {tam / 1024:.0f} KB")
    if args.publicar_bruto and bases:
        pasta = site / "bruto"
        for g, b in bases.items():
            pncp.gravar_json(pasta / f"{g}.json.gz", b, comprimir=True)
        pncp.gravar_json(pasta / "indice.json", {"atualizado_em": agora().isoformat(timespec="seconds"),
                                                 "grupos": situacao})
    return 0 if (bases or dados.get("itens")) else 1


if __name__ == "__main__":
    sys.exit(main())
