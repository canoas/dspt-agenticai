#!/usr/bin/env python3
"""
Gera cache/painel_partidas.html: as sessões de cinema do Porto num painel de
partidas de aeroporto. Lê cache/cartaz_porto.json, cache/cinemas_porto.json e,
se existir, cache/ratings_filmes.json, e mete os dados dentro do HTML (o painel
abre sem servidor e calcula o estado de cada sessão pela hora de Lisboa).

Uso:
    python cartaz_porto.py && python ratings_filmes.py && python painel_partidas.py
    open cache/painel_partidas.html

Por defeito escreve um documento HTML completo (é o que o GitHub Pages serve em /fly/).
Com --fragmento escreve só o conteúdo, para publicar como Claude Artifact, que acrescenta
o <!doctype> e o <head>.
"""

import argparse
import json
import re
import sys
from pathlib import Path

from cartaz_porto import CACHE, CACHE_DIR, CINEMAS, ler_json

MODELO = Path(__file__).with_name("painel_partidas.template.html")
SAIDA = CACHE_DIR / "painel_partidas.html"
RATINGS = CACHE_DIR / "ratings_filmes.json"

# Grupo exibidor (o ícone ao lado do cinema) e nome curto. O grupo vem do início do id;
# cinemas sem grupo conhecido usam as iniciais do nome.
GRUPOS = [
    ("cinemas-nos-", "NOS"),
    ("uci-", "UCI"),
    ("castello-lopes-", "CL"),
    ("cinema-trindade", "TRI"),
    ("teatro-municipal-campo-alegre", "MED"),   # Medeia Filmes
    ("cinemax-", "CMX"),
    ("rivoli-", "TMP"),                          # Teatro Municipal do Porto
    ("casa-das-artes", "CDA"),
    ("passos-manuel", "PM"),
]
NOMES_CURTOS = {
    "cinemas-nos-norteshopping-matosinhos": "NorteShopping",
    "uci-cinemas-arrabida-20-gaia": "Arrábida 20",
    "cinema-trindade": "Trindade",
    "cinemas-nos-alameda-shop-e-spot-porto": "Alameda",
    "cinemas-nos-parque-nascente-rio-tinto": "Parque Nascente",
    "cinemas-nos-gaia-shopping": "GaiaShopping",
    "cinemas-nos-mar-shopping-matosinhos": "MarShopping",
    "castello-lopes-mira-maia-shopping": "Mira Maia",
    "cinemas-nos-ferrara-plaza-pacos-de-ferreira": "Ferrara Plaza",
    "teatro-municipal-campo-alegre-porto": "Campo Alegre",
    "cinemax-penafiel": "Penafiel",
    "casa-das-artes-do-porto": "Casa das Artes",
    "passos-manuel-porto": "Passos Manuel",
    "rivoli-teatro-municipal-porto": "Rivoli",
}


def grupo_e_nome(c: dict) -> tuple:
    grupo = next((g for prefixo, g in GRUPOS if c["id"].startswith(prefixo)), None)
    if grupo is None:
        palavras = [p for p in re.split(r"[\s-]+", c["nome"]) if p[:1].isupper()]
        grupo = "".join(p[0] for p in palavras)[:3].upper() or "?"
    return grupo, NOMES_CURTOS.get(c["id"], c["nome"])


DOCUMENTO = """<!doctype html>
<html lang="pt-PT">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
</head>
<body>
{conteudo}
</body>
</html>
"""


def main() -> None:
    p = argparse.ArgumentParser(description="Painel de partidas do cartaz do Porto.")
    p.add_argument("--fragmento", action="store_true", help="só o conteúdo, sem <!doctype> nem <head>")
    args = p.parse_args()

    cartaz = ler_json(CACHE)
    if not cartaz.get("dias"):
        raise SystemExit(f"Sem sessões em {CACHE}; corre primeiro cartaz_porto.py.")
    cinemas = {c["id"]: c for c in ler_json(CINEMAS).get("cinemas", [])}
    ratings = ler_json(RATINGS).get("filmes", {})

    usados_c, usados_f, sessoes = set(), set(), []
    for dia, ss in cartaz["dias"].items():
        for s in ss:
            usados_c.add(s["cinema_id"])
            usados_f.add(s["filme_id"])
            sessoes.append([dia, s["hora"], s["cinema_id"], s["filme_id"], s["sala"] or "",
                            " ".join(s["formatos"])])

    dados = {
        "gerado_em": cartaz.get("gerado_em"),
        "fontes": [f["nome"] for f in cartaz.get("fontes", []) if not f.get("erro")],
        "cinemas": {},
        "filmes": {},
        "sessoes": sessoes,
    }
    for cid in usados_c:
        c = cinemas.get(cid, {"id": cid, "nome": cid})
        grupo, curto = grupo_e_nome(c)
        dados["cinemas"][cid] = {"grupo": grupo, "nome": curto, "completo": c["nome"],
                                 "localidade": c.get("localidade")}
    for fid in usados_f:
        f = cartaz["filmes"].get(fid, {"titulo": fid})
        r = ratings.get(fid, {})
        imdb = (r.get("imdb") or {}).get("rating")
        dados["filmes"][fid] = {"titulo": f["titulo"], "classificacao": f.get("classificacao"),
                                "imdb": imdb, "imdb_url": (r.get("imdb") or {}).get("url")}

    js = json.dumps(dados, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = MODELO.read_text(encoding="utf-8").replace("__DADOS__", js)
    if not args.fragmento:
        html = DOCUMENTO.format(conteudo=html)
    SAIDA.parent.mkdir(parents=True, exist_ok=True)
    SAIDA.write_text(html, encoding="utf-8")
    print(f"{SAIDA} ({len(sessoes)} sessões, {len(usados_c)} cinemas).", file=sys.stderr)


if __name__ == "__main__":
    main()
