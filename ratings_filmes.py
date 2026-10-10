#!/usr/bin/env python3
"""
Ratings dos filmes do cartaz, guardados em cache/ratings_filmes.json.

Lê os filmes de cache/cartaz_porto.json (gerado por cartaz_porto.py) e só trata
os que ainda não estão em cache/ratings_filmes.json; os outros ficam como estão.

Para cada filme novo:
  - IMDb: o ID vem da página do filme no filmspot; a nota e o número de votos vêm
    do dataset oficial do IMDb (https://datasets.imdbws.com/title.ratings.tsv.gz,
    atualizado diariamente, uso pessoal e não comercial). O site do IMDb proíbe
    scraping no robots.txt, por isso não se pedem páginas de imdb.com.
    O dataset só se descarrega se houver filmes novos com ID do IMDb.
  - Ípsilon: estrelas (0 a 5) das críticas do Público, da página do filme no Cinecartaz.

Uso:
    python ratings_filmes.py              # completa os filmes que faltam
    python ratings_filmes.py --refazer    # volta a tratar todos os filmes do cartaz
"""

import argparse
import gzip
import io
import re
import sys
from datetime import datetime
from typing import Dict, Optional, Set

import requests

from cartaz_porto import (CACHE, CACHE_DIR, CINECARTAZ, FILMSPOT, TZ, USER_AGENT, Cliente,
                          Proibido, aviso, escrever_json, ler_json, sopa, texto)

RATINGS = CACHE_DIR / "ratings_filmes.json"
IMDB_DATASET = "https://datasets.imdbws.com/title.ratings.tsv.gz"
IMDB_ID_RE = re.compile(r"imdb\.com/title/(tt\d+)")


def imdb_id_filmspot(cli: Cliente, url: str) -> Optional[str]:
    m = IMDB_ID_RE.search(cli.get(url))
    return m.group(1) if m else None


def criticas_ipsilon(cli: Cliente, url: str) -> list:
    s = sopa(cli.get(url))
    res = []
    for c in s.select("section.section_reviews .reader_review_detail__inner"):
        estrelas = c.select_one(".movie-detail__rating-select")
        link = c.select_one("a[href]")
        res.append({
            "autor": texto(c.select_one(".ipsilon_name_author")) or None,
            "titulo": texto(c.select_one(".title_review")) or None,
            "estrelas": len(estrelas.select("i.i-cc-star--filled")) if estrelas else None,
            "url": link["href"] if link else None,
        })
    return res


def ratings_imdb(ids: Set[str]) -> Dict[str, dict]:
    """Procura os IDs no dataset do IMDb, lido em streaming (não fica em disco)."""
    aviso("imdb", f"a descarregar {IMDB_DATASET} para {len(ids)} filmes...")
    r = requests.get(IMDB_DATASET, headers={"User-Agent": USER_AGENT}, stream=True, timeout=60)
    r.raise_for_status()
    res = {}
    with gzip.GzipFile(fileobj=r.raw) as gz:
        for linha in io.TextIOWrapper(gz, encoding="utf-8"):
            tconst, _, resto = linha.partition("\t")
            if tconst in ids:
                nota, votos = resto.strip().split("\t")
                res[tconst] = {"rating": float(nota), "votos": int(votos)}
                if len(res) == len(ids):
                    break
    return res


def main() -> None:
    p = argparse.ArgumentParser(description="Ratings dos filmes do cartaz do Porto.")
    p.add_argument("--refazer", action="store_true", help="voltar a tratar todos os filmes do cartaz")
    args = p.parse_args()

    filmes = ler_json(CACHE).get("filmes", {})
    if not filmes:
        raise SystemExit(f"Sem filmes em {CACHE}; corre primeiro cartaz_porto.py.")
    dados = ler_json(RATINGS)
    guardados: Dict[str, dict] = {} if args.refazer else dados.get("filmes", {})
    novos = {fid: f for fid, f in filmes.items() if fid not in guardados}
    print(f"{len(filmes)} filmes no cartaz, {len(novos)} por tratar.", file=sys.stderr)
    if not novos:
        return

    clientes: Dict[str, Cliente] = {}

    def cliente(base: str) -> Cliente:
        if base not in clientes:
            clientes[base] = Cliente(base)
        return clientes[base]

    agora = datetime.now(TZ).isoformat(timespec="seconds")
    resultado: Dict[str, dict] = {}
    for fid, f in novos.items():
        urls = f.get("urls", {})
        r = {"titulo": f["titulo"], "ano": f.get("ano"), "imdb": None, "ipsilon": None,
             "obtido_em": agora}
        try:
            if urls.get("filmspot"):
                tid = imdb_id_filmspot(cliente(FILMSPOT), urls["filmspot"])
                if tid:
                    r["imdb"] = {"id": tid, "url": f"https://www.imdb.com/title/{tid}/",
                                 "rating": None, "votos": None}
            if urls.get("cinecartaz"):
                criticas = criticas_ipsilon(cliente(CINECARTAZ), urls["cinecartaz"])
                notas = [c["estrelas"] for c in criticas if c["estrelas"] is not None]
                r["ipsilon"] = {"media": round(sum(notas) / len(notas), 1) if notas else None,
                                "escala": 5, "criticas": criticas}
        except (requests.RequestException, Proibido) as e:
            # fica de fora para ser tentado na próxima execução
            aviso("ratings", f"{f['titulo']}: {e}")
            continue
        resultado[fid] = r

    ids = {r["imdb"]["id"] for r in resultado.values() if r["imdb"]}
    if ids:
        try:
            notas = ratings_imdb(ids)
        except requests.RequestException as e:
            aviso("imdb", f"dataset indisponível ({e}); estes filmes ficam para a próxima execução.")
            resultado = {k: v for k, v in resultado.items() if not v["imdb"]}
            notas = {}
        for r in resultado.values():
            if r["imdb"]:
                r["imdb"].update(notas.get(r["imdb"]["id"], {}))

    guardados.update(resultado)
    escrever_json(RATINGS, {
        "atualizado_em": agora,
        "notas": [
            f"imdb: nota de 0 a 10 e votos do dataset {IMDB_DATASET} (uso pessoal e não comercial);"
            " rating null = filme ainda sem votos no IMDb.",
            "ipsilon: estrelas de 0 a 5 das críticas do Público, via Cinecartaz.",
            "Cada filme é tratado uma só vez; usa --refazer para atualizar notas.",
        ],
        "filmes": dict(sorted(guardados.items(), key=lambda kv: kv[1]["titulo"].casefold())),
    })
    print(f"{len(resultado)} filmes acrescentados a {RATINGS}.", file=sys.stderr)


if __name__ == "__main__":
    main()
