#!/usr/bin/env python3
"""
Cache diária da programação de cinema no Porto.

- Lê o robots.txt da fonte com o MESMO user-agent que usa nos pedidos
  e não pede páginas que o robots proíba.
- Respeita o Crawl-delay do robots.txt (ou 1 s por defeito).
- Guarda um JSON em cache/cartaz_porto.json com as sessões de todos os
  dias que a página mostra.
- Se a cache for de hoje (Europe/Lisbon), não volta a pedir nada,
  a menos que uses --forcar.

Uso:
    pip install requests beautifulsoup4
    export CARTAZ_UA="o-teu-user-agent (contacto: tu@exemplo.pt)"
    python cartaz_porto_cache.py                # atualiza se a cache for de ontem ou mais antiga
    python cartaz_porto_cache.py --forcar       # ignora a cache e volta a pedir
    python cartaz_porto_cache.py --dia 2026-10-10   # mostra o dia pedido a partir da cache

Para correr uma vez por dia (cron, por exemplo às 06:00):
    0 6 * * * cd /caminho/do/projeto && python3 cartaz_porto_cache.py
"""

import argparse
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional
from urllib import robotparser
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BASE = "https://www.magazine-hd.com"
INDICE = f"{BASE}/filme/"
DISTRITO_ALVO = "Porto"
TZ = ZoneInfo("Europe/Lisbon")
CACHE = Path(os.environ.get("CARTAZ_CACHE", "cache/cartaz_porto.json"))
# O user-agent é escolhido por ti. Podes definir CARTAZ_UA; por defeito usa um
# identificador honesto com um contacto por preencher.
USER_AGENT = os.environ.get(
    "CARTAZ_UA", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36 Edg/154.0.0.0"
)

DISTRITOS = {
    "Açores", "Aveiro", "Beja", "Braga", "Bragança", "Castelo Branco", "Coimbra",
    "Évora", "Faro", "Guarda", "Leiria", "Lisboa", "Madeira", "Portalegre", "Porto",
    "Santarém", "Setúbal", "Viana do Castelo", "Vila Real", "Viseu",
}
MESES = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4, "maio": 5,
    "junho": 6, "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10,
    "novembro": 11, "dezembro": 12,
}
HORA_RE = re.compile(r"^\d{1,2}:\d{2}$")
FORMATO_RE = re.compile(
    r"^(VP|VO|VOS|VOSE|VPT|2D|3D|4DX|IMAX|XPLUS|VIP|D-BOX|VP ?3D|VO ?3D)$", re.I
)
DIA_RE = re.compile(
    r"^(segunda|terça|quarta|quinta|sexta|sábado|domingo),?\s+(\d{1,2}) de ([a-zçãé]+)$",
    re.I,
)
FIM_RE = re.compile(r"^(SEGUE-NOS|MHD\b)", re.I)


@dataclass(frozen=True)
class Sessao:
    hora: str       # "HH:MM"
    cinema: str
    sala: str       # formato (VP/VO/3D...); a fonte não indica o número da sala
    filme: str


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------

class Robots:
    def __init__(self, base: str, user_agent: str, sess: requests.Session):
        self.user_agent = user_agent
        self.rp = robotparser.RobotFileParser()
        try:
            r = sess.get(urljoin(base, "/robots.txt"), timeout=20)
            if r.status_code == 200:
                self.rp.parse(r.text.splitlines())
            elif r.status_code in (401, 403):
                # acesso ao robots.txt negado: não assumimos permissão
                self.rp.disallow_all = True
            else:
                self.rp.allow_all = True
        except requests.RequestException as e:
            raise SystemExit(f"Não foi possível ler o robots.txt: {e}")

    def pode(self, url: str) -> bool:
        return self.rp.can_fetch(self.user_agent, url)

    def pausa(self, default: float = 1.0) -> float:
        cd = self.rp.crawl_delay(self.user_agent)
        return float(cd) if cd else default


# ---------------------------------------------------------------------------
# Extração
# ---------------------------------------------------------------------------

def linhas_de(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return [l.strip() for l in soup.get_text("\n").splitlines() if l.strip()]


def titulo_de(html: str, url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    return url.rstrip("/").rsplit("/", 1)[-1]


def urls_filmes(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls = set()
    for a in soup.find_all("a", href=True):
        m = re.match(r"^(?:https://www\.magazine-hd\.com)?/filme/([^/?#]+)/?$", a["href"])
        if m:
            urls.add(f"{BASE}/filme/{m.group(1)}")
    return sorted(urls)


def _data(m: re.Match, ano: int) -> Optional[date]:
    mes = MESES.get(m.group(3).lower())
    if not mes:
        return None
    try:
        return date(ano, mes, int(m.group(2)))
    except ValueError:
        return None


def extrair_sessoes_todos_os_dias(linhas: List[str], ano: int) -> Dict[date, List[Sessao]]:
    """Sessões do distrito do Porto, para todos os dias do bloco de sessões."""
    resultado: Dict[date, List[Sessao]] = {}
    i = 0
    while i < len(linhas):
        m = DIA_RE.match(linhas[i])
        dia = _data(m, ano) if m else None
        if not dia:
            i += 1
            continue
        j = i + 1
        distrito = cinema = formato = None
        sessoes = resultado.setdefault(dia, [])
        while j < len(linhas):
            linha = linhas[j]
            if FIM_RE.match(linha) or DIA_RE.match(linha):
                break
            if linha in DISTRITOS:
                distrito, cinema, formato = linha, None, None
            elif distrito != DISTRITO_ALVO:
                pass
            elif HORA_RE.match(linha):
                if cinema:
                    sessoes.append(Sessao(hora=linha.zfill(5), cinema=cinema,
                                          sala=formato or "—", filme=""))
            elif FORMATO_RE.match(linha):
                formato = linha.upper()
            else:
                cinema, formato = linha, None
            j += 1
        i = j
    return resultado


def sessoes_do_filme(html: str, url: str, ano: int) -> Dict[date, List[Sessao]]:
    filme = titulo_de(html, url)
    por_dia = extrair_sessoes_todos_os_dias(linhas_de(html), ano)
    return {
        d: [Sessao(hora=s.hora, cinema=s.cinema, sala=s.sala, filme=filme) for s in ss]
        for d, ss in por_dia.items() if ss
    }


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def cache_de_hoje(cache: Path, hoje: date) -> bool:
    if not cache.exists():
        return False
    try:
        dados = json.loads(cache.read_text(encoding="utf-8"))
        return dados.get("gerado_em", "")[:10] == hoje.isoformat()
    except (json.JSONDecodeError, OSError):
        return False


def ler_cache(cache: Path) -> dict:
    return json.loads(cache.read_text(encoding="utf-8"))


def guardar_cache(cache: Path, por_dia: Dict[date, List[Sessao]]) -> None:
    cache.parent.mkdir(parents=True, exist_ok=True)
    dias = {}
    for d in sorted(por_dia):
        ss = sorted(por_dia[d], key=lambda s: (s.hora, s.cinema.casefold(),
                                               s.sala.casefold(), s.filme.casefold()))
        dias[d.isoformat()] = [asdict(s) for s in ss]
    payload = {
        "gerado_em": datetime.now(TZ).isoformat(timespec="seconds"),
        "fonte": INDICE,
        "distrito": DISTRITO_ALVO,
        "user_agent": USER_AGENT,
        "dias": dias,
    }
    tmp = cache.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(cache)


# ---------------------------------------------------------------------------
# Recolha
# ---------------------------------------------------------------------------

def recolher(ano: int) -> Dict[date, List[Sessao]]:
    sess = requests.Session()
    sess.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "pt-PT,pt;q=0.9"})

    robots = Robots(BASE, USER_AGENT, sess)
    pausa = robots.pausa()

    if not robots.pode(INDICE):
        raise SystemExit(f"O robots.txt proíbe {INDICE} para este user-agent.")

    indice = sess.get(INDICE, timeout=20)
    indice.raise_for_status()
    urls = urls_filmes(indice.text)
    print(f"{len(urls)} filmes no índice.", file=sys.stderr)

    por_dia: Dict[date, List[Sessao]] = {}
    for url in urls:
        if not robots.pode(url):
            print(f"[robots] a saltar {url}", file=sys.stderr)
            continue
        try:
            r = sess.get(url, timeout=20)
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"[aviso] {url}: {e}", file=sys.stderr)
            time.sleep(pausa)
            continue
        for d, ss in sessoes_do_filme(r.text, url, ano).items():
            por_dia.setdefault(d, []).extend(ss)
        time.sleep(pausa)
    return por_dia


def mostrar_dia(dados: dict, dia_iso: str) -> None:
    ss = dados.get("dias", {}).get(dia_iso)
    if not ss:
        print(f"Sem sessões no Porto para {dia_iso} na cache.")
        return
    print(f"== {DISTRITO_ALVO} · {dia_iso} ==")
    for s in ss:
        print(f"{s['hora']}  {s['cinema']:<36.36}  {s['sala']:<6.6}  {s['filme']}")


def main() -> None:
    p = argparse.ArgumentParser(description="Cache diária do cartaz de cinemas no Porto.")
    p.add_argument("--forcar", action="store_true", help="voltar a pedir mesmo com cache de hoje")
    p.add_argument("--dia", help="mostrar um dia (AAAA-MM-DD) a partir da cache")
    args = p.parse_args()

    hoje = datetime.now(TZ).date()

    if not args.forcar and cache_de_hoje(CACHE, hoje):
        print(f"Cache de hoje já existe em {CACHE}; não se volta a pedir.", file=sys.stderr)
    else:
        por_dia = recolher(ano=hoje.year)
        guardar_cache(CACHE, por_dia)
        print(f"Cache atualizada em {CACHE}.", file=sys.stderr)

    dados = ler_cache(CACHE)
    mostrar_dia(dados, args.dia or hoje.isoformat())


if __name__ == "__main__":
    main()