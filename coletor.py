#!/usr/bin/env python3
"""
Coletor compartilhado do PNCP, por grupo de UFs.

Grava <saida>/<grupo>.json.gz com todas as contratações com propostas em aberto
das UFs do grupo (sem filtro de assunto). Os painéis classificam a partir desses
arquivos.

Modos:
- completa: lista tudo que está com proposta em aberto (endpoint /contratacoes/proposta).
- incremental: parte do arquivo anterior e busca só o que foi atualizado ou publicado
  desde a última sincronização. Se o PNCP recusar o endpoint incremental, cai para completa.

Uso:
    python coletor.py --grupo SP --base https://usuario.github.io/licitacoes-ti/bruto/ --saida bruto/
    python coletor.py --grupo SP --completa --max-paginas 2      # teste rápido
"""

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

import pncp
from pncp import log, agora, parse_data

BASE = Path(__file__).resolve().parent


def chave(reg):
    return reg.get("numeroControlePNCP")


def combo_de(reg):
    return f"{(reg.get('unidadeOrgao') or {}).get('ufSigla')}-{reg.get('modalidadeId')}"


def coletar_completa(cfg, ufs, cliente, max_paginas=None):
    data_final = (agora() + timedelta(days=int(cfg.get("dias_a_frente", 365)))).strftime("%Y%m%d")
    registros, falhas, erros = {}, [], []
    for uf in ufs:
        for cod, nome in cfg["modalidades"].items():
            log(f"{uf} / {nome} (completa)")
            try:
                dados = cliente.paginar("contratacoes/proposta",
                                        {"dataFinal": data_final, "codigoModalidadeContratacao": cod, "uf": uf},
                                        max_paginas)
                for reg in dados:
                    if chave(reg):
                        registros[chave(reg)] = pncp.enxugar(reg)
                log(f"  {len(dados)} registros")
            except Exception as e:
                log(f"  FALHA: {e}")
                falhas.append(f"{uf}-{cod}")
                erros.append(f"{uf} / {nome}: {e}")
    return registros, falhas, erros


def coletar_incremental(cfg, ufs, cliente, desde, max_paginas=None):
    """Devolve (novos, falhas, erros) ou levanta pncp.ParametroRejeitado se nenhum
    endpoint incremental for aceito."""
    endpoints = list(cfg.get("endpoints_incrementais", ["contratacoes/atualizacao", "contratacoes/publicacao"]))
    hoje = agora()
    params_data = {"dataInicial": desde.strftime("%Y%m%d"), "dataFinal": hoje.strftime("%Y%m%d")}
    novos, falhas, erros = {}, [], []
    for uf in ufs:
        for cod, nome in cfg["modalidades"].items():
            log(f"{uf} / {nome} (desde {desde:%d/%m})")
            while True:
                try:
                    dados = cliente.paginar(endpoints[0], {**params_data, "codigoModalidadeContratacao": cod, "uf": uf},
                                            max_paginas)
                    abertos = 0
                    for reg in dados:
                        enc = parse_data(reg.get("dataEncerramentoProposta"))
                        if chave(reg) and enc:
                            novos[chave(reg)] = pncp.enxugar(reg)
                            abertos += enc >= hoje
                    log(f"  {len(dados)} atualizados, {abertos} com proposta em aberto ({endpoints[0]})")
                    break
                except pncp.ParametroRejeitado as e:
                    log(f"  {endpoints[0]} recusado: {e}")
                    endpoints.pop(0)
                    if not endpoints:
                        raise
                except Exception as e:
                    log(f"  FALHA: {e}")
                    falhas.append(f"{uf}-{cod}")
                    erros.append(f"{uf} / {nome}: {e}")
                    break
    return novos, falhas, erros, endpoints[0]


def main():
    ap = argparse.ArgumentParser(description="Coletor do PNCP por grupo de UFs")
    ap.add_argument("--grupo", required=True)
    ap.add_argument("--config", default=str(BASE / "coleta.json"))
    ap.add_argument("--base", help="pasta ou URL com os arquivos da coleta anterior (termina em /)")
    ap.add_argument("--saida", default=str(BASE / "bruto"))
    ap.add_argument("--completa", action="store_true", help="força coleta completa")
    ap.add_argument("--max-paginas", type=int, default=None)
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    grupo = args.grupo
    ufs = cfg["grupos"][grupo]
    inicio = agora()
    cliente = pncp.ClientePNCP(pausa=float(cfg.get("pausa_entre_chamadas_seg", 5)))

    anterior = pncp.ler_json(f"{args.base}{grupo}.json.gz") if args.base else None
    if anterior and sorted(anterior.get("ufs", [])) != sorted(ufs):
        log("UFs do grupo mudaram desde a última coleta; refazendo completa.")
        anterior = None

    modo = "completa"
    motivo = "forçada" if args.completa else "sem coleta anterior"
    ultima_completa = parse_data(anterior.get("ultima_completa")) if anterior else None
    ultima_sinc = parse_data(anterior.get("ultima_sincronizacao")) if anterior else None
    if anterior and not args.completa and ultima_completa and ultima_sinc:
        if (inicio - ultima_completa).days >= int(cfg.get("dias_entre_coletas_completas", 7)):
            motivo = f"última completa em {ultima_completa:%d/%m}"
        elif (inicio - ultima_sinc).days > 10:
            motivo = f"última sincronização em {ultima_sinc:%d/%m}"
        else:
            modo = "incremental"
    log(f"Grupo {grupo} ({', '.join(ufs)}): coleta {modo}" + ("" if modo == "incremental" else f" ({motivo})"))

    endpoint = "contratacoes/proposta"
    if modo == "incremental":
        desde = ultima_sinc - timedelta(days=int(cfg.get("dias_sobreposicao_incremental", 1)))
        try:
            novos, falhas, erros, endpoint = coletar_incremental(cfg, ufs, cliente, desde, args.max_paginas)
            registros = {chave(r): r for r in anterior.get("registros", [])}
            registros.update(novos)
        except pncp.ParametroRejeitado:
            log("Nenhum endpoint incremental aceito; fazendo coleta completa.")
            modo, motivo = "completa", "incremental indisponível"
    if modo == "completa":
        registros, falhas, erros = coletar_completa(cfg, ufs, cliente, args.max_paginas)
        if anterior and falhas:
            # o que não foi possível coletar agora fica com os dados anteriores
            for r in anterior.get("registros", []):
                if combo_de(r) in falhas and chave(r) not in registros:
                    registros[chave(r)] = r

    total_combos = len(ufs) * len(cfg["modalidades"])
    if len(falhas) == total_combos:
        log("Todas as consultas falharam.")
        if anterior:
            anterior.setdefault("historico_falhas", []).append(inicio.isoformat(timespec="seconds"))
            anterior["status"] = "falha"
            anterior["erros"] = erros[:30]
            pncp.gravar_json(Path(args.saida) / f"{grupo}.json.gz", anterior, comprimir=True)
        return 1

    lista = [r for r in registros.values() if pncp.ainda_aberta(r, inicio)]
    fim = agora()
    resultado = {
        "grupo": grupo,
        "ufs": ufs,
        "modo": modo,
        "motivo": None if modo == "incremental" else motivo,
        "endpoint": endpoint,
        "status": "ok" if not falhas else "parcial",
        "gerado_em": fim.isoformat(timespec="seconds"),
        # numa coleta com falha a janela não avança, para a próxima cobrir o que faltou
        "ultima_sincronizacao": (inicio if not falhas else (ultima_sinc or inicio)).isoformat(timespec="seconds"),
        "ultima_completa": (inicio if modo == "completa" and not falhas
                            else (ultima_completa or inicio)).isoformat(timespec="seconds"),
        "duracao_min": round((fim - inicio).total_seconds() / 60, 1),
        "chamadas_api": cliente.chamadas,
        "combinacoes_falha": falhas,
        "erros": erros[:30],
        "registros": lista,
    }
    tam = pncp.gravar_json(Path(args.saida) / f"{grupo}.json.gz", resultado, comprimir=True)
    log(f"Concluído: {len(lista)} contratações abertas, {cliente.chamadas} chamadas, "
        f"{len(falhas)} falhas, {tam / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
