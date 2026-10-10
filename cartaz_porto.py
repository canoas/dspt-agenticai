#!/usr/bin/env python3
"""
Cache diária da programação de cinema no distrito do Porto, juntando vários sites.

Fontes (cada uma num módulo `fonte_*`; uma fonte que falhe não impede as outras):
  - filmspot     https://filmspot.pt/salas/            sessões com número da sala,
                                                       morada e GPS de cada cinema
  - cinecartaz   https://cinecartaz.publico.pt/todassalas/localizacao?selectedLocation=Porto
                                                       realização, elenco, classificação,
                                                       formatos (ATMOS, XLVISION...)
  - magazine-hd  https://www.magazine-hd.com/filme/    sessões e formato (VP/VO/3D...)

As sessões das várias fontes são fundidas: o mesmo filme, no mesmo cinema, dia e hora,
fica numa só sessão, com a lista de fontes que a confirmam.

Ficheiros gerados:
  - cache/cartaz_porto.json   sessões por dia e ficha de cada filme
  - cache/cinemas_porto.json  cinemas: nome, morada, GPS, salas vistas nas sessões
                              e lotação, quando conhecida (ver salas_porto_manual.json)

Boas maneiras:
  - Lê o robots.txt de cada site com o MESMO user-agent dos pedidos e não pede
    páginas que o robots proíba.
  - Respeita o Crawl-delay de cada site (ou 1 s por defeito). Os sites são
    visitados em paralelo, mas cada site recebe um pedido de cada vez.
  - Se a cache for de hoje (Europe/Lisbon), não volta a pedir nada, a menos que
    uses --forcar.

Uso:
    pip install requests beautifulsoup4
    export CARTAZ_UA="o-teu-user-agent (contacto: tu@exemplo.pt)"
    python cartaz_porto.py                        # atualiza se a cache não for de hoje
    python cartaz_porto.py --forcar               # ignora a cache e volta a pedir
    python cartaz_porto.py --fontes filmspot,cinecartaz
    python cartaz_porto.py --dia 2026-10-10       # mostra o dia pedido a partir da cache

Para correr uma vez por dia (cron, por exemplo às 06:00):
    0 6 * * * cd /caminho/do/projeto && python3 cartaz_porto.py
"""

import argparse
import difflib
import json
import os
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib import robotparser
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo

DISTRITO_ALVO = "Porto"
TZ = ZoneInfo("Europe/Lisbon")
CACHE_DIR = Path(os.environ.get("CARTAZ_CACHE_DIR", "cache"))
CACHE = CACHE_DIR / "cartaz_porto.json"
CINEMAS = CACHE_DIR / "cinemas_porto.json"
# Lotação das salas: nenhuma das fontes a publica, por isso vem de um ficheiro
# mantido à mão (com a fonte de cada número).
LOTACAO = Path(__file__).with_name("salas_porto_manual.json")
# O user-agent é escolhido por ti. Podes definir CARTAZ_UA.
USER_AGENT = os.environ.get(
    "CARTAZ_UA", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36 Edg/154.0.0.0"
)

MESES = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4, "maio": 5,
    "junho": 6, "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10,
    "novembro": 11, "dezembro": 12,
}
MESES_ABREV = {
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
}
HORA_RE = re.compile(r"^\d{1,2}:\d{2}$")


# ---------------------------------------------------------------------------
# Modelo
# ---------------------------------------------------------------------------

@dataclass
class Cinema:
    nome: str
    fonte: str
    url: str
    complemento: Optional[str] = None   # p.ex. "Centro Comercial NorteShopping"
    morada: Optional[str] = None
    codigo_postal: Optional[str] = None
    localidade: Optional[str] = None
    distrito: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None


@dataclass
class Filme:
    titulo: str
    fonte: str
    url: Optional[str] = None
    titulos: List[str] = field(default_factory=list)   # títulos alternativos
    ano: Optional[int] = None
    realizacao: Optional[str] = None
    elenco: Optional[str] = None
    classificacao: Optional[str] = None


@dataclass
class Sessao:
    dia: date
    hora: str          # "HH:MM"
    cinema: str        # nome do cinema tal como a fonte o escreve
    filme: str         # título tal como a fonte o escreve
    fonte: str
    sala: Optional[str] = None                     # "Sala 5"; só o filmspot indica
    formatos: Tuple[str, ...] = ()                 # VP, VO, 3D, IMAX, ATMOS...


@dataclass
class Recolha:
    fonte: str
    url: str
    cinemas: List[Cinema] = field(default_factory=list)
    filmes: List[Filme] = field(default_factory=list)
    sessoes: List[Sessao] = field(default_factory=list)
    erro: Optional[str] = None
    avisos: int = 0


def aviso(fonte: str, msg: str) -> None:
    print(f"[{fonte}] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# HTTP: um cliente por site, com robots.txt e Crawl-delay
# ---------------------------------------------------------------------------

class Proibido(Exception):
    pass


class Cliente:
    def __init__(self, base: str):
        self.sess = requests.Session()
        self.sess.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "pt-PT,pt;q=0.9"})
        self.rp = robotparser.RobotFileParser()
        r = self.sess.get(urljoin(base, "/robots.txt"), timeout=20)
        if r.status_code == 200:
            self.rp.parse(r.text.splitlines())
        elif r.status_code in (401, 403):
            # acesso ao robots.txt negado: não assumimos permissão
            self.rp.disallow_all = True
        else:
            self.rp.allow_all = True
        cd = self.rp.crawl_delay(USER_AGENT)
        self.pausa = float(cd) if cd else 1.0
        self._ultimo = 0.0

    def get(self, url: str, tentativas: int = 2) -> str:
        if not self.rp.can_fetch(USER_AGENT, url):
            raise Proibido(f"o robots.txt proíbe {url}")
        for n in range(tentativas):
            espera = self._ultimo + self.pausa - time.monotonic()
            if espera > 0:
                time.sleep(espera)
            try:
                r = self.sess.get(url, timeout=20)
            except requests.RequestException:
                if n + 1 == tentativas:
                    raise
                continue
            finally:
                self._ultimo = time.monotonic()
            if r.status_code >= 500 and n + 1 < tentativas:
                continue
            r.raise_for_status()
            # Sem charset no Content-Type, o requests assume ISO-8859-1 e estraga
            # "sábado", "Évora"... Estes sites são todos UTF-8.
            charset = "charset=" in r.headers.get("Content-Type", "").lower()
            return r.content.decode(r.encoding if charset else "utf-8", errors="replace")
        raise RuntimeError("inalcançável")


def sopa(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def texto(tag) -> str:
    return re.sub(r"\s+", " ", tag.get_text(" ")).strip() if tag else ""


def inferir_ano(dia: int, mes: int, hoje: date) -> Optional[date]:
    """A data com esse dia/mês mais próxima de hoje (resolve a passagem de ano)."""
    candidatos = []
    for ano in (hoje.year - 1, hoje.year, hoje.year + 1):
        try:
            candidatos.append(date(ano, mes, dia))
        except ValueError:
            pass
    return min(candidatos, key=lambda d: abs((d - hoje).days)) if candidatos else None


def hora_hhmm(h: str, m: Optional[str] = None) -> str:
    return f"{int(h):02d}:{int(m or 0):02d}"


# ---------------------------------------------------------------------------
# Fonte: filmspot.pt
# ---------------------------------------------------------------------------

FILMSPOT = "https://filmspot.pt"
# A página /salas/ agrupa por região, não por distrito. Cinemas do distrito do
# Porto como Paços de Ferreira ou Penafiel aparecem em "Norte", por isso
# visitamos as três regiões e filtramos pelo distrito da morada.
FILMSPOT_REGIOES = ("Porto", "Grande Porto", "Norte")


def _titulo_filmspot(titulo: str) -> Tuple[List[str], Optional[int]]:
    """'A Ilha Esquecida / Forgotten Island (2026)' -> (['A Ilha Esquecida', 'Forgotten Island'], 2026)"""
    ano = None
    m = re.search(r"\s*\((\d{4})\)\s*$", titulo)
    if m:
        ano, titulo = int(m.group(1)), titulo[: m.start()]
    partes = [re.sub(r"\s+", " ", p).strip() for p in titulo.split(" / ")]
    return [p for p in partes if p], ano


def filmspot_cinema(html: str, url: str, hoje: date) -> Tuple[Cinema, List[Filme], List[Sessao]]:
    s = sopa(html)
    cinema = Cinema(nome=texto(s.select_one(".vcard .fn")) or texto(s.h1), fonte="filmspot", url=url)
    for bloco in s.find_all("script", type="application/ld+json"):
        try:
            g = json.loads(bloco.string or "").get("@graph", {})
        except (json.JSONDecodeError, AttributeError):
            continue
        if isinstance(g, dict) and g.get("@type") == "MovieTheater":
            cinema.nome = g.get("name") or cinema.nome
            geo, adr = g.get("geo") or {}, g.get("address") or {}
            cinema.lat, cinema.lon = geo.get("latitude"), geo.get("longitude")
            cinema.distrito = adr.get("addressRegion") or None
            cinema.morada = adr.get("streetAddress") or None
            cinema.codigo_postal = adr.get("postalCode") or None
            break
    adr = s.select_one(".vcard .adr")
    if adr:
        cinema.morada = texto(adr.select_one(".street-address")) or cinema.morada
        cinema.codigo_postal = texto(adr.select_one(".postal-code")) or cinema.codigo_postal
        cinema.localidade = texto(adr.select_one(".locality")) or None
        primeiro = adr.find("div")
        if primeiro and not primeiro.get("class"):
            cinema.complemento = texto(primeiro) or None

    filmes, sessoes = [], []
    for h3 in s.select("h3.cinemaFilme"):
        a = h3.find("a", href=True)
        titulos, ano = _titulo_filmspot(texto(h3))
        if not titulos:
            continue
        filmes.append(Filme(titulo=titulos[0], titulos=titulos[1:], ano=ano, fonte="filmspot",
                            url=urljoin(FILMSPOT, a["href"]) if a else None))
        div = h3.find_next_sibling("div", class_="cinemaTabelaSessoesDiv")
        if not div:
            continue
        for tabela in div.select("table.cinemaTabelaSessoes"):
            sala = texto(tabela.select_one("th.sala")) or None
            for tr in tabela.find_all("tr"):
                m = re.match(r"(\d{1,2})\s+(\w{3})", texto(tr.select_one(".cinemaCartazDia")))
                mes = MESES_ABREV.get(m.group(2).lower()) if m else None
                dia = inferir_ano(int(m.group(1)), mes, hoje) if mes else None
                if not dia:
                    continue
                for td in tr.find_all("td"):
                    mh = re.search(r"\b(\d{1,2}):(\d{2})\b", td.find(string=True, recursive=False) or "")
                    if not mh:
                        continue
                    formatos = tuple(texto(ab).upper() for ab in td.find_all("abbr"))
                    sessoes.append(Sessao(dia=dia, hora=hora_hhmm(*mh.groups()), cinema=cinema.nome,
                                          filme=titulos[0], fonte="filmspot", sala=sala,
                                          formatos=formatos))
    return cinema, filmes, sessoes


def fonte_filmspot(cli: Cliente, hoje: date, rec: Recolha) -> None:
    s = sopa(cli.get(f"{FILMSPOT}/salas/"))
    urls = []
    for h2 in s.find_all("h2"):
        if texto(h2) in FILMSPOT_REGIOES:
            ul = h2.find_next_sibling("ul")
            urls += [urljoin(FILMSPOT, a["href"]) for a in (ul.find_all("a", href=True) if ul else [])]
    aviso(rec.fonte, f"{len(urls)} cinemas nas regiões {', '.join(FILMSPOT_REGIOES)}.")
    for url in urls:
        try:
            cinema, filmes, sessoes = filmspot_cinema(cli.get(url), url, hoje)
        except (requests.RequestException, Proibido) as e:
            aviso(rec.fonte, f"{url}: {e}")
            rec.avisos += 1
            continue
        if cinema.distrito != DISTRITO_ALVO:
            continue
        rec.cinemas.append(cinema)
        rec.filmes += filmes
        rec.sessoes += sessoes


# ---------------------------------------------------------------------------
# Fonte: cinecartaz.publico.pt
# ---------------------------------------------------------------------------

CINECARTAZ = "https://cinecartaz.publico.pt"
CINECARTAZ_INDICE = f"{CINECARTAZ}/todassalas/localizacao?selectedLocation={DISTRITO_ALVO}"
# O Cinecartaz mostra a semana de cinema (5ª a 4ª) sem datas: deslocamento de cada
# dia em relação à quinta-feira.
CC_DIAS = {"5ª": 0, "6ª": 1, "sab.": 2, "dom.": 3, "2ª": 4, "3ª": 5, "4ª": 6}
CC_GRUPO_RE = re.compile(r"((?:(?:[2-6]ª|Sab\.|Dom\.)\s*)+):\s*(.*?)(?=(?:(?:[2-6]ª|Sab\.|Dom\.)\s*)+:|$)")


def cc_sessoes(txt: str, quinta: date) -> List[Tuple[date, str, Tuple[str, ...]]]:
    """'5ª 6ª : 13h30, 19h (VP ATMOS) | 21h Sab. Dom. : 10h50' -> [(dia, 'HH:MM', formatos)]"""
    txt = re.sub(r"^\s*Sessões:\s*", "", txt)
    res = []
    for grupo in CC_GRUPO_RE.finditer(txt):
        dias = [quinta + timedelta(days=CC_DIAS[d.lower()])
                for d in re.findall(r"[2-6]ª|Sab\.|Dom\.", grupo.group(1))]
        for troco in grupo.group(2).split("|"):
            etiquetas = re.findall(r"\(([^)]*)\)", troco)
            formatos: List[str] = []
            for e in etiquetas:
                tokens = e.split()
                # "VP ATMOS" são formatos; "Legendas em Inglês" fica como está
                formatos += [t.upper() for t in tokens] if all(t.isupper() for t in tokens) else [e.strip()]
            horas = [hora_hhmm(h, m) for h, m in re.findall(r"\b(\d{1,2})h(\d{2})?", re.sub(r"\([^)]*\)", "", troco))]
            res += [(d, h, tuple(formatos)) for d in dias for h in horas]
    return res


def cinecartaz_cinema(html: str, nome: str, quinta: date) -> Tuple[List[Filme], List[Sessao]]:
    s = sopa(html)
    filmes, sessoes = [], []
    for card in s.select("div.movie-card"):
        conteudo = card.select_one(".movie-card__content")
        titulo = texto(conteudo.select_one(".movie-card__title")) if conteudo else ""
        if not titulo:
            continue
        a = card.select_one("a.button[href^='/filme/']") or card.select_one("a[href^='/filme/']")
        filme = Filme(titulo=titulo, fonte="cinecartaz", url=urljoin(CINECARTAZ, a["href"]) if a else None)
        for p in conteudo.find_all("p"):
            t = texto(p)
            if t.startswith("De:"):
                filme.realizacao = t[3:].strip() or None
            elif t.startswith("Com:"):
                filme.elenco = t[4:].strip() or None
            elif re.fullmatch(r"M/\d+", t):
                filme.classificacao = t
            elif t.startswith("Sessões:"):
                for dia, hora, formatos in cc_sessoes(t, quinta):
                    sessoes.append(Sessao(dia=dia, hora=hora, cinema=nome, filme=titulo,
                                          fonte="cinecartaz", formatos=formatos))
        filmes.append(filme)
    return filmes, sessoes


def fonte_cinecartaz(cli: Cliente, hoje: date, rec: Recolha) -> None:
    quinta = hoje - timedelta(days=(hoje.weekday() - 3) % 7)
    s = sopa(cli.get(CINECARTAZ_INDICE))
    vistos = {}
    for bloco in s.select(".name_address"):
        a = bloco.select_one("a[href^='/cinema/']")
        if a and a["href"] not in vistos:
            vistos[a["href"]] = (texto(a), texto(bloco.select_one(".label-cc")) or None)
    aviso(rec.fonte, f"{len(vistos)} cinemas no distrito.")
    for href, (nome, morada) in vistos.items():
        url = urljoin(CINECARTAZ, href)
        rec.cinemas.append(Cinema(nome=nome, fonte="cinecartaz", url=url, morada=morada,
                                  distrito=DISTRITO_ALVO))
        try:
            filmes, sessoes = cinecartaz_cinema(cli.get(url), nome, quinta)
        except (requests.RequestException, Proibido) as e:
            aviso(rec.fonte, f"{url}: {e}")
            rec.avisos += 1
            continue
        rec.filmes += filmes
        rec.sessoes += sessoes


# ---------------------------------------------------------------------------
# Fonte: magazine-hd.com
# ---------------------------------------------------------------------------

MAGAZINE_HD = "https://www.magazine-hd.com"
MHD_INDICE = f"{MAGAZINE_HD}/filme/"
DISTRITOS = {
    "Açores", "Aveiro", "Beja", "Braga", "Bragança", "Castelo Branco", "Coimbra",
    "Évora", "Faro", "Guarda", "Leiria", "Lisboa", "Madeira", "Portalegre", "Porto",
    "Santarém", "Setúbal", "Viana do Castelo", "Vila Real", "Viseu",
}
MHD_FORMATO_RE = re.compile(r"^(VP|VO|VOS|VOSE|VPT|2D|3D|4DX|IMAX|XPLUS|VIP|D-BOX|SCREENX|ATMOS)$", re.I)
MHD_DIA_RE = re.compile(
    r"^(segunda|terça|quarta|quinta|sexta|sábado|domingo),?\s+(\d{1,2}) de ([a-zçãé]+)$", re.I
)
MHD_FIM_RE = re.compile(r"^(SEGUE-NOS|MHD\b)", re.I)


def mhd_formatos(linha: str) -> Optional[Tuple[str, ...]]:
    """'4DX 3D' -> ('4DX', '3D'); None se a linha não for só formatos."""
    tokens = linha.split()
    return tuple(t.upper() for t in tokens) if tokens and all(MHD_FORMATO_RE.match(t) for t in tokens) else None


def mhd_sessoes(linhas: List[str], filme: str, hoje: date) -> List[Sessao]:
    """Sessões do distrito alvo, para todos os dias do bloco de sessões."""
    res: List[Sessao] = []
    dia = distrito = cinema = None
    formatos: Tuple[str, ...] = ()
    for linha in linhas:
        m = MHD_DIA_RE.match(linha)
        if m:
            mes = MESES.get(m.group(3).lower())
            dia = inferir_ano(int(m.group(2)), mes, hoje) if mes else None
            distrito = cinema = None
            continue
        if not dia:
            continue
        if MHD_FIM_RE.match(linha):
            dia = None
        elif linha in DISTRITOS:
            distrito, cinema, formatos = linha, None, ()
        elif distrito != DISTRITO_ALVO:
            pass
        elif HORA_RE.match(linha):
            if cinema:
                res.append(Sessao(dia=dia, hora=linha.zfill(5), cinema=cinema, filme=filme,
                                  fonte="magazine-hd", formatos=formatos))
        elif mhd_formatos(linha):
            formatos = mhd_formatos(linha)
        else:
            cinema, formatos = linha, ()
    return res


def fonte_magazine_hd(cli: Cliente, hoje: date, rec: Recolha) -> None:
    s = sopa(cli.get(MHD_INDICE))
    urls = set()
    for a in s.find_all("a", href=True):
        m = re.match(r"^(?:https://www\.magazine-hd\.com)?/filme/([^/?#]+)/?$", a["href"])
        if m:
            urls.add(f"{MAGAZINE_HD}/filme/{m.group(1)}")
    aviso(rec.fonte, f"{len(urls)} filmes no índice.")
    for url in sorted(urls):
        try:
            s = sopa(cli.get(url))
        except (requests.RequestException, Proibido) as e:
            aviso(rec.fonte, f"{url}: {e}")
            rec.avisos += 1
            continue
        titulo = texto(s.h1) or url.rstrip("/").rsplit("/", 1)[-1]
        for tag in s(["script", "style", "noscript"]):
            tag.decompose()
        linhas = [l.strip() for l in s.get_text("\n").splitlines() if l.strip()]
        sessoes = mhd_sessoes(linhas, titulo, hoje)
        if sessoes:
            rec.filmes.append(Filme(titulo=titulo, fonte="magazine-hd", url=url))
            rec.sessoes += sessoes
            rec.cinemas += [Cinema(nome=n, fonte="magazine-hd", url=MHD_INDICE, distrito=DISTRITO_ALVO)
                            for n in {x.cinema for x in sessoes}]


# Ordem = prioridade na fusão: a primeira fonte dá o nome canónico de cinemas e filmes.
FONTES: Dict[str, Tuple[str, Callable[[Cliente, date, Recolha], None]]] = {
    "filmspot": (FILMSPOT, fonte_filmspot),
    "cinecartaz": (CINECARTAZ, fonte_cinecartaz),
    "magazine-hd": (MAGAZINE_HD, fonte_magazine_hd),
}


def correr_fonte(nome: str, hoje: date) -> Recolha:
    base, fn = FONTES[nome]
    rec = Recolha(fonte=nome, url=base)
    try:
        fn(Cliente(base), hoje, rec)
    except (requests.RequestException, Proibido) as e:
        rec.erro = str(e)
        aviso(nome, f"falhou: {e}")
    aviso(nome, f"{len(rec.sessoes)} sessões, {len(rec.cinemas)} cinemas.")
    return rec


# ---------------------------------------------------------------------------
# Fusão das fontes
# ---------------------------------------------------------------------------

def normalizar(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.replace("&", " e "))
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def slug(s: str) -> str:
    return normalizar(s).replace(" ", "-")


# Palavras que não distinguem um cinema de outro (marcas, artigos).
GENERICAS = {"cinema", "cinemas", "nos", "zon", "lusomundo", "uci", "castello", "lopes",
             "medeia", "de", "do", "da", "dos", "das", "e", "o", "a"}


def palavras_cinema(nome: str) -> set:
    return set(normalizar(nome).split()) - GENERICAS


class Cinemas:
    """Junta o mesmo cinema escrito de formas diferentes pelas várias fontes."""

    def __init__(self):
        self.lista: List[dict] = []
        self._por_nome: Dict[str, dict] = {}

    def encontrar(self, nome: str) -> Optional[dict]:
        if nome in self._por_nome:
            return self._por_nome[nome]
        p = palavras_cinema(nome)
        melhores, melhor = [], 0
        for c in self.lista:
            q = c["_palavras"]
            comuns = len(p & q)
            # todas as palavras distintivas do nome mais curto estão no outro
            if comuns and comuns == min(len(p), len(q)):
                if comuns > melhor:
                    melhores, melhor = [c], comuns
                elif comuns == melhor:
                    melhores.append(c)
        return melhores[0] if len(melhores) == 1 else None

    def juntar(self, cin: Cinema) -> dict:
        c = self.encontrar(cin.nome)
        if c is None:
            c = {"id": slug(cin.nome), "nome": cin.nome, "aliases": [], "fontes": {},
                 "_palavras": palavras_cinema(cin.nome)}
            self.lista.append(c)
        if cin.nome != c["nome"] and cin.nome not in c["aliases"]:
            c["aliases"].append(cin.nome)
        c["fontes"].setdefault(cin.fonte, cin.url)
        for k in ("complemento", "morada", "codigo_postal", "localidade", "distrito", "lat", "lon"):
            if c.get(k) is None and getattr(cin, k) is not None:
                c[k] = getattr(cin, k)
        self._por_nome[cin.nome] = c
        return c


def variantes_titulo(f: Filme) -> List[str]:
    vs = []
    for t in [f.titulo, *f.titulos]:
        t = re.sub(r"\s*\(\d{4}\)\s*$", "", t)
        vs.append(normalizar(t))
    return [v for v in vs if v]


def mesmo_titulo(a: str, b: str) -> bool:
    if a == b:
        return True
    # "Resident Evil" e "Resident Evil 2" não são o mesmo filme
    if re.findall(r"\d+", a) != re.findall(r"\d+", b):
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.88


class Filmes:
    """Junta o mesmo filme vindo de fontes diferentes (títulos com grafias diferentes)."""

    def __init__(self):
        self.lista: List[dict] = []
        self._por_titulo: Dict[str, dict] = {}

    def encontrar(self, titulo: str) -> Optional[dict]:
        return self._por_titulo.get(titulo)

    def juntar(self, f: Filme) -> dict:
        vs = variantes_titulo(f)
        alvo = self._por_titulo.get(f.titulo)
        if alvo is None:
            alvo = next((x for x in self.lista
                         if any(mesmo_titulo(a, b) for a in vs for b in x["_variantes"])), None)
        if alvo is None:
            alvo = {"id": slug(f.titulo) + (f"-{f.ano}" if f.ano else ""), "titulo": f.titulo,
                    "titulos": [], "ano": None, "realizacao": None, "elenco": None,
                    "classificacao": None, "urls": {}, "_variantes": []}
            self.lista.append(alvo)
        for t in [f.titulo, *f.titulos]:
            if t != alvo["titulo"] and t not in alvo["titulos"]:
                alvo["titulos"].append(t)
        alvo["_variantes"] += [v for v in vs if v not in alvo["_variantes"]]
        for k in ("ano", "realizacao", "elenco", "classificacao"):
            if alvo[k] is None and getattr(f, k) is not None:
                alvo[k] = getattr(f, k)
        if f.url:
            alvo["urls"].setdefault(f.fonte, f.url)
        self._por_titulo[f.titulo] = alvo
        return alvo


def fundir(recolhas: List[Recolha], hoje: date) -> Tuple[Cinemas, Filmes, Dict[tuple, dict]]:
    cinemas, filmes = Cinemas(), Filmes()
    for rec in recolhas:              # ordem de FONTES = prioridade
        for c in rec.cinemas:
            cinemas.juntar(c)
        for f in rec.filmes:
            filmes.juntar(f)
    sessoes: Dict[tuple, dict] = {}
    for rec in recolhas:
        for s in rec.sessoes:
            if s.dia < hoje:
                continue
            c = cinemas.encontrar(s.cinema) or cinemas.juntar(Cinema(nome=s.cinema, fonte=s.fonte, url=rec.url))
            f = filmes.encontrar(s.filme) or filmes.juntar(Filme(titulo=s.filme, fonte=s.fonte))
            chave = (s.dia, s.hora, c["id"], f["id"])
            x = sessoes.setdefault(chave, {"hora": s.hora, "cinema": c["nome"], "cinema_id": c["id"],
                                           "sala": None, "filme": f["titulo"], "filme_id": f["id"],
                                           "formatos": [], "fontes": []})
            x["sala"] = x["sala"] or s.sala
            x["formatos"] += [t for t in s.formatos if t not in x["formatos"]]
            if s.fonte not in x["fontes"]:
                x["fontes"].append(s.fonte)
    return cinemas, filmes, sessoes


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def cache_de_hoje(cache: Path, hoje: date) -> bool:
    try:
        dados = json.loads(cache.read_text(encoding="utf-8"))
        return dados.get("gerado_em", "")[:10] == hoje.isoformat()
    except (json.JSONDecodeError, OSError):
        return False


def ler_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def escrever_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def publico(d: dict) -> dict:
    return {k: v for k, v in d.items() if not k.startswith("_")}


def ordem_sala(nome: str) -> tuple:
    m = re.search(r"\d+", nome)
    return (int(m.group()) if m else 10**6, nome)


def guardar_cartaz(recolhas: List[Recolha], filmes: Filmes, sessoes: Dict[tuple, dict]) -> None:
    dias: Dict[str, list] = {}
    for (d, *_), s in sorted(sessoes.items(), key=lambda kv: (kv[0][0], kv[1]["hora"],
                                                              kv[1]["cinema"].casefold(),
                                                              kv[1]["filme"].casefold())):
        dias.setdefault(d.isoformat(), []).append(s)
    usados = {s["filme_id"] for s in sessoes.values()}
    escrever_json(CACHE, {
        "gerado_em": datetime.now(TZ).isoformat(timespec="seconds"),
        "distrito": DISTRITO_ALVO,
        "user_agent": USER_AGENT,
        "fontes": [{"nome": r.fonte, "url": r.url, "sessoes": len(r.sessoes),
                    "avisos": r.avisos, "erro": r.erro} for r in recolhas],
        "filmes": {f["id"]: publico(f) for f in sorted(filmes.lista, key=lambda f: f["titulo"].casefold())
                   if f["id"] in usados},
        "dias": dias,
    })


def guardar_cinemas(cinemas: Cinemas, sessoes: Dict[tuple, dict]) -> None:
    """Cinemas com as salas vistas nas sessões. As salas de execuções anteriores
    mantêm-se (uma sala sem sessões esta semana continua a existir)."""
    anterior = {c["id"]: c for c in ler_json(CINEMAS).get("cinemas", [])}
    # o ficheiro manual usa nomes; casam com os cinemas como os das fontes
    lotacao = {}
    for nome, info in ler_json(LOTACAO).get("cinemas", {}).items():
        c = cinemas.encontrar(nome)
        if c:
            lotacao[c["id"]] = info
        else:
            print(f"[lotação] cinema desconhecido em {LOTACAO.name}: {nome}", file=sys.stderr)

    salas: Dict[str, Dict[str, dict]] = {}
    for (d, *_), s in sessoes.items():
        if not s["sala"]:
            continue
        sala = salas.setdefault(s["cinema_id"], {}).setdefault(
            s["sala"], {"nome": s["sala"], "lugares": None, "formatos": [], "visto_em": d.isoformat()})
        sala["formatos"] += [t for t in s["formatos"] if t not in sala["formatos"]]
        sala["visto_em"] = max(sala["visto_em"], d.isoformat())

    lista = []
    for c in sorted(cinemas.lista, key=lambda c: c["nome"].casefold()):
        if c.get("distrito") not in (None, DISTRITO_ALVO):
            continue
        ant = anterior.get(c["id"], {})
        por_nome = {s["nome"]: s for s in ant.get("salas", [])}
        for nome, s in salas.get(c["id"], {}).items():
            velho = por_nome.get(nome, {})
            s["formatos"] += [t for t in velho.get("formatos", []) if t not in s["formatos"]]
            por_nome[nome] = s
        manual = lotacao.get(c["id"], {})
        for nome, lugares in manual.get("salas", {}).items():
            por_nome.setdefault(nome, {"nome": nome, "lugares": None, "formatos": [], "visto_em": None})
            por_nome[nome]["lugares"] = lugares
        lista_salas = sorted(por_nome.values(), key=lambda s: ordem_sala(s["nome"]))
        lugares = [s["lugares"] for s in lista_salas]
        out = {
            "id": c["id"], "nome": c["nome"], "aliases": c["aliases"],
            "complemento": c.get("complemento"), "morada": c.get("morada"),
            "codigo_postal": c.get("codigo_postal"), "localidade": c.get("localidade"),
            "distrito": c.get("distrito") or DISTRITO_ALVO,
            "gps": ({"lat": c["lat"], "lon": c["lon"]} if c.get("lat") is not None
                    else ant.get("gps")),
            "n_salas": manual.get("n_salas") or (len(lista_salas) or None),
            "lugares_total": manual.get("lugares_total")
                             or (sum(lugares) if lugares and None not in lugares else None),
            "salas": lista_salas,
            "fontes": c["fontes"],
        }
        if manual.get("nota") or manual.get("fonte"):
            out["lotacao_nota"] = {k: manual[k] for k in ("nota", "fonte") if k in manual}
        lista.append(out)

    escrever_json(CINEMAS, {
        "gerado_em": datetime.now(TZ).isoformat(timespec="seconds"),
        "distrito": DISTRITO_ALVO,
        "notas": [
            "salas: as que aparecem nas sessões do filmspot (e de execuções anteriores);"
            " uma sala sem sessões pode faltar. n_salas é o total conhecido quando há"
            " fonte para isso, senão o número de salas vistas.",
            f"lugares/lugares_total: de {LOTACAO.name}, mantido à mão; null = desconhecido.",
        ],
        "cinemas": lista,
    })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def recolher(nomes: Iterable[str], hoje: date) -> List[Recolha]:
    nomes = list(nomes)
    # um fio por site; dentro de cada site os pedidos são sequenciais
    with ThreadPoolExecutor(max_workers=len(nomes)) as ex:
        return list(ex.map(lambda n: correr_fonte(n, hoje), nomes))


def mostrar_dia(dados: dict, dia_iso: str) -> None:
    ss = dados.get("dias", {}).get(dia_iso)
    if not ss:
        print(f"Sem sessões no {DISTRITO_ALVO} para {dia_iso} na cache.")
        return
    print(f"== {DISTRITO_ALVO} · {dia_iso} ==")
    for s in ss:
        print(f"{s['hora']}  {s['cinema']:<36.36}  {(s['sala'] or '—'):<8.8}  "
              f"{'/'.join(s['formatos']) or '—':<10.10}  {s['filme']}")


def main() -> None:
    p = argparse.ArgumentParser(description="Cache diária do cartaz de cinemas no Porto.")
    p.add_argument("--forcar", action="store_true", help="voltar a pedir mesmo com cache de hoje")
    p.add_argument("--dia", help="mostrar um dia (AAAA-MM-DD) a partir da cache")
    p.add_argument("--fontes", default=",".join(FONTES),
                   help=f"fontes separadas por vírgulas (por defeito: {','.join(FONTES)})")
    args = p.parse_args()

    nomes = [n.strip() for n in args.fontes.split(",") if n.strip()]
    desconhecidas = [n for n in nomes if n not in FONTES]
    if desconhecidas or not nomes:
        p.error(f"fontes desconhecidas: {', '.join(desconhecidas)}; disponíveis: {', '.join(FONTES)}")
    nomes = [n for n in FONTES if n in nomes]   # manter a ordem de prioridade

    hoje = datetime.now(TZ).date()
    if not args.forcar and cache_de_hoje(CACHE, hoje):
        print(f"Cache de hoje já existe em {CACHE}; não se volta a pedir.", file=sys.stderr)
    else:
        recolhas = recolher(nomes, hoje)
        if all(r.erro or not r.sessoes for r in recolhas):
            raise SystemExit("Nenhuma fonte devolveu sessões; a cache anterior fica como estava.")
        cinemas, filmes, sessoes = fundir(recolhas, hoje)
        guardar_cartaz(recolhas, filmes, sessoes)
        guardar_cinemas(cinemas, sessoes)
        print(f"Cache atualizada em {CACHE} e {CINEMAS} "
              f"({len(sessoes)} sessões, {len(cinemas.lista)} cinemas).", file=sys.stderr)

    mostrar_dia(ler_json(CACHE), args.dia or hoje.isoformat())


if __name__ == "__main__":
    main()
