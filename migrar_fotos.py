#!/usr/bin/env python3
"""Migra fotos do Google Fotos (Takeout) para o iCloud Fotos no Windows.

Etapas:
  analisar  Le o Takeout e o iCloud, encontra duplicatas e gera o relatorio.
            Itens da Lixeira do Google sao ignorados.
  preparar  Cria numa unica pasta COPIAS das fotos novas, com data, hora e local
            corrigidos. HEIC vira JPEG, porque o site icloud.com nao aceita HEIC.
            Depois, arraste a pasta inteira para icloud.com/photos.
  enviar    Alternativa ao site: copia para a pasta do iCloud para Windows
            (exige --confirmar). Use 'preparar --manter-heic' antes.
  conferir  Confere os arquivos copiados pela etapa 'enviar'.

Os arquivos do Takeout e do iCloud nunca sao alterados nem apagados.
"""
import argparse
import csv
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageOps
import pillow_heif

pillow_heif.register_heif_opener()
Image.MAX_IMAGE_PIXELS = 400_000_000

IMAGENS = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif", ".webp", ".tif", ".tiff", ".bmp"}
VIDEOS = {".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mkv", ".wmv", ".mpg", ".mpeg"}
RAW = {".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf"}
MIDIA = IMAGENS | VIDEOS | RAW | {".mp"}
COM_EXIF = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".webp", ".tif", ".tiff", ".dng"}
HEIC = {".heic", ".heif"}
QUICKTIME = {".mp4", ".mov", ".m4v", ".3gp"}

SUFIXOS_EDITADA = ("-edited", "-editado", "-editada", "-editar", "-bearbeitet", "-modifié", "-editato", "-edité")
SUPLEMENTO = "supplemental-metadata"

DIST_DUPLICATA = 6     # diferenca visual maxima para duplicata com a mesma data
DIST_PARECIDA = 12     # acima disso, com a mesma data, sao fotos diferentes
DIST_SEM_DATA = 3      # visual quase identico com datas diferentes: vai para revisao
TOLERANCIA_S = 2
FORMATO = "%Y:%m:%d %H:%M:%S"


# ---------------------------------------------------------------- ExifTool

def achar_exiftool():
    candidatos = [os.environ.get("EXIFTOOL"), shutil.which("exiftool"),
                  str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "ExifTool" / "exiftool.exe")]
    for c in candidatos:
        if c and Path(c).exists():
            return c
    sys.exit("ExifTool nao encontrado.")


class ExifTool:
    def __enter__(self):
        self.p = subprocess.Popen(
            [achar_exiftool(), "-stay_open", "True", "-@", "-", "-common_args",
             "-charset", "filename=utf8", "-charset", "utf8", "-api", "QuickTimeUTC=1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return self

    def __exit__(self, *_):
        self.p.stdin.write(b"-stay_open\nFalse\n")
        self.p.stdin.flush()
        self.p.wait(timeout=30)

    def rodar(self, args):
        texto = "\n".join(str(a).replace("\r", " ").replace("\n", " ") for a in args) + "\n-execute\n"
        self.p.stdin.write(texto.encode("utf-8"))
        self.p.stdin.flush()
        saida = []
        while True:
            linha = self.p.stdout.readline()
            if not linha:
                raise RuntimeError("ExifTool encerrou inesperadamente")
            if linha.strip() == b"{ready}":
                return b"".join(saida).decode("utf-8", "replace")
            saida.append(linha)

    def ler(self, caminhos):
        tags = ["-j", "-n", "-DateTimeOriginal", "-CreationDate", "-CreateDate", "-OffsetTimeOriginal",
                "-GPSLatitude", "-GPSLongitude", "-Duration"]
        bruto = self.rodar(tags + [Path(c).as_posix() for c in caminhos])
        try:
            # strict=False aceita caracteres de controle gravados por algumas cameras.
            itens = json.loads(bruto.strip(), strict=False)
            if not all(isinstance(i, dict) and "SourceFile" in i for i in itens):
                raise ValueError("resposta incompleta")
        except ValueError:
            if len(caminhos) == 1:
                print(f"  Aviso: metadados ilegiveis em {caminhos[0]}", flush=True)
                return {}
            meio = len(caminhos) // 2
            return self.ler(caminhos[:meio]) | self.ler(caminhos[meio:])
        return {os.path.normcase(os.path.normpath(i["SourceFile"])): i for i in itens}


def interpretar_data(valor, offset_extra, fuso):
    if not isinstance(valor, str):
        return None
    m = re.match(r"(\d{4}):(\d\d):(\d\d) (\d\d):(\d\d):(\d\d)(?:\.\d+)?\s*(Z|[+-]\d\d:?\d\d)?", valor)
    if not m:
        return None
    try:
        dt = datetime(*map(int, m.groups()[:6]))
    except ValueError:
        return None
    if dt.year < 1900:
        return None
    return int(dt.replace(tzinfo=ler_offset(m.group(7) or offset_extra) or fuso).timestamp())


def ler_offset(txt):
    if not isinstance(txt, str):
        return None
    if txt == "Z":
        return timezone.utc
    m = re.fullmatch(r"([+-])(\d\d):?(\d\d)", txt.strip())
    if not m:
        return None
    delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
    return timezone(-delta if m.group(1) == "-" else delta)


def data_dos_metadados(meta, fuso):
    offset = meta.get("OffsetTimeOriginal")
    for tag in ("DateTimeOriginal", "CreationDate", "CreateDate"):
        t = interpretar_data(meta.get(tag), offset, fuso)
        if t:
            return t
    return None


def data_do_nome(nome, fuso):
    m = re.search(r"(?<!\d)((?:19|20)\d{2})[-_.]?(\d{2})[-_.]?(\d{2})[-_. ]?(\d{2})[-_.]?(\d{2})[-_.]?(\d{2})(?!\d)", nome)
    partes = m.groups() if m else None
    if not partes:
        m = re.search(r"(?<!\d)((?:19|20)\d{2})[-_]?(\d{2})[-_]?(\d{2})(?=-WA|[-_ ]|$)", nome)
        partes = (*m.groups(), "12", "0", "0") if m else None
    if not partes:
        return None
    try:
        dt = datetime(*map(int, partes), tzinfo=fuso)
    except ValueError:
        return None
    return int(dt.timestamp()) if dt <= datetime.now(fuso) else None


# ---------------------------------------------------------------- Indice

def abrir_banco(saida):
    db = sqlite3.connect(saida / "indice.sqlite")
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS arquivos (
        caminho TEXT PRIMARY KEY, tamanho INTEGER, mtime INTEGER, sha TEXT, dhash TEXT,
        data INTEGER, duracao REAL, largura INTEGER, altura INTEGER, tem_gps INTEGER)""")
    return db


def sha256(caminho):
    h = hashlib.sha256()
    with open(caminho, "rb") as f:
        while bloco := f.read(1024 * 1024):
            h.update(bloco)
    return h.hexdigest()


def hash_visual(caminho):
    """Assinatura visual (dHash de 64 bits). Fotos sem detalhe retornam None."""
    try:
        with Image.open(caminho) as im:
            im = ImageOps.exif_transpose(im)
            largura, altura = im.size
            px = im.convert("L").resize((9, 8), Image.Resampling.BOX).tobytes()
    except Exception:
        return None, None, None
    valor = 0
    for y in range(8):
        for x in range(8):
            valor = (valor << 1) | (px[y * 9 + x] > px[y * 9 + x + 1])
    bits = valor.bit_count()
    return (f"{valor:016x}" if 8 <= bits <= 56 else None), largura, altura


def listar_midia(raiz):
    for pasta, _, nomes in os.walk(raiz):
        for nome in nomes:
            if Path(nome).suffix.lower() in MIDIA:
                yield Path(pasta) / nome


def somente_na_nuvem(caminho):
    atributos = getattr(os.stat(caminho), "st_file_attributes", 0)
    return bool(atributos & (0x1000 | 0x40000 | 0x400000))


def indexar(db, et, raiz, fuso, rotulo, ignorar=frozenset()):
    arquivos = [a for a in listar_midia(raiz) if a not in ignorar]
    faltando = [a for a in arquivos if somente_na_nuvem(a)]
    if faltando:
        sys.exit(f"\n{len(faltando)} arquivo(s) de {rotulo} estao somente na nuvem. Marque a pasta como "
                 "'Sempre manter neste dispositivo' e rode de novo.\nSem isso a comparacao ficaria "
                 "incompleta e poderia criar duplicatas.")
    pendentes = []
    for a in arquivos:
        st = a.stat()
        r = db.execute("SELECT tamanho, mtime FROM arquivos WHERE caminho=?", (str(a),)).fetchone()
        if not r or r["tamanho"] != st.st_size or r["mtime"] != st.st_mtime_ns:
            pendentes.append(a)
    for i in range(0, len(pendentes), 100):
        lote = pendentes[i:i + 100]
        metas = et.ler(lote)
        for a in lote:
            meta = metas.get(os.path.normcase(os.path.normpath(a)), {})
            st = a.stat()
            dh, w, h = hash_visual(a) if a.suffix.lower() in IMAGENS else (None, None, None)
            dur = meta.get("Duration")
            db.execute("INSERT OR REPLACE INTO arquivos VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (str(a), st.st_size, st.st_mtime_ns, sha256(a), dh, data_dos_metadados(meta, fuso),
                        dur if isinstance(dur, (int, float)) else None, w, h,
                        int(bool(meta.get("GPSLatitude")))))
        db.commit()
        print(f"  {rotulo}: {min(i + 100, len(pendentes))}/{len(pendentes)} lidos", flush=True)
    itens = []
    for a in arquivos:
        r = dict(db.execute("SELECT * FROM arquivos WHERE caminho=?", (str(a),)).fetchone())
        itens.append(r)
    return itens


# ---------------------------------------------------------------- JSON do Takeout

def analisar_nome_json(nome):
    s = nome[:-5]
    contador = 0
    m = re.search(r"\((\d+)\)$", s)
    if m:
        contador, s = int(m.group(1)), s[:m.start()]
    if "." in s:
        base, ultimo = s.rsplit(".", 1)
        if ultimo and SUPLEMENTO.startswith(ultimo.lower()) and "." in base:
            s = base
    return s.rstrip("."), contador


def carregar_jsons(raiz):
    por_pasta = defaultdict(list)
    for pasta, _, nomes in os.walk(raiz):
        for nome in nomes:
            if not nome.lower().endswith(".json"):
                continue
            try:
                with open(Path(pasta) / nome, encoding="utf-8") as f:
                    dados = json.load(f)
            except (OSError, ValueError):
                continue
            if not isinstance(dados, dict) or "photoTakenTime" not in dados:
                continue
            base, contador = analisar_nome_json(nome)
            por_pasta[pasta].append({"base": base.lower(), "contador": contador,
                                     "titulo": str(dados.get("title", "")).lower(), "dados": dados})
    return por_pasta


def separar_nome(nome):
    p = Path(nome)
    stem, contador = p.stem, 0
    m = re.search(r"\((\d+)\)$", stem)
    if m:
        contador, stem = int(m.group(1)), stem[:m.start()]
    original, editada = stem, False
    for suf in SUFIXOS_EDITADA:
        if stem.lower().endswith(suf):
            original, editada = stem[:-len(suf)], True
            break
    return stem, original, p.suffix, contador, editada


def achar_json(caminho, entradas):
    stem, original, ext, contador, editada = separar_nome(Path(caminho).name)
    nomes = [(stem + ext).lower()] + ([(original + ext).lower()] if editada else [])
    mesmos = [e for e in entradas if e["contador"] == contador]
    for nome in nomes:
        for campo in ("base", "titulo"):
            for e in mesmos:
                if e[campo] == nome:
                    return e["dados"]
    for nome in nomes:  # nomes cortados pelo Takeout
        achados = [e for e in mesmos if len(e["base"]) >= 20 and nome.startswith(e["base"])]
        if len(achados) == 1:
            return achados[0]["dados"]
    alvo = original.lower()
    if len(alvo) >= 20:
        achados = [e for e in mesmos if e["titulo"].startswith(alvo) and e["titulo"].endswith(ext.lower())]
        if len(achados) == 1:
            return achados[0]["dados"]
    if ext.lower() in VIDEOS | {".mp"}:  # video de Live/Motion Photo usa o JSON da foto
        achados = [e for e in mesmos if Path(e["titulo"] or e["base"]).stem == alvo]
        if len(achados) == 1:
            return achados[0]["dados"]
    return None


def resolver_data(item, js, fuso):
    """Retorna (instante, fonte, precisa_gravar)."""
    exif = item["data"]
    nome = data_do_nome(Path(item["caminho"]).name, fuso)
    tirada = criada = None
    if js:
        try:
            tirada = int(js["photoTakenTime"]["timestamp"]) or None
            criada = int(js.get("creationTime", {}).get("timestamp", 0)) or None
        except (KeyError, TypeError, ValueError):
            tirada = None
    if exif and tirada:
        # Diferenca grande indica data corrigida pelo usuario no Google Fotos.
        return (exif, "exif", False) if abs(exif - tirada) <= 15 * 3600 else (tirada, "json", True)
    if exif:
        return exif, "exif", False
    if tirada:
        # Sem EXIF, o Google costuma usar a data de upload; a data do nome e melhor.
        if nome and criada and abs(tirada - criada) <= 60:
            return nome, "nome", True
        return tirada, "json", True
    if nome:
        return nome, "nome", True
    return None, "", False


def extrair_gps(js):
    if not js:
        return None
    for campo in ("geoDataExif", "geoData"):
        g = js.get(campo) or {}
        lat, lon = g.get("latitude") or 0, g.get("longitude") or 0
        if lat or lon:
            return {"lat": lat, "lon": lon, "alt": g.get("altitude") or 0}
    return None


# ---------------------------------------------------------------- Comparacao

def distancia(a, b):
    return (int(a, 16) ^ int(b, 16)).bit_count()


def proporcao_ok(a, b):
    if not (a["largura"] and a["altura"] and b["largura"] and b["altura"]):
        return True
    return abs(a["largura"] / a["altura"] - b["largura"] / b["altura"]) <= 0.02 * (b["largura"] / b["altura"])


def candidatos_por_data(t, por_data):
    """Mesmo instante, tolerando erro de fuso em horas inteiras."""
    if not t:
        return
    for horas in range(-14, 15):
        for d in range(-TOLERANCIA_S, TOLERANCIA_S + 1):
            yield from por_data.get(t + horas * 3600 + d, ())


def classificar(google, icloud):
    shas = {i["sha"]: i for i in icloud}
    por_data = defaultdict(list)
    faixas = defaultdict(list)
    for i in icloud:
        if i["data"]:
            por_data[i["data"]].append(i)
        if i["dhash"]:
            for n in range(4):
                faixas[(n, i["dhash"][n * 4:n * 4 + 4])].append(i)

    reivindicados = defaultdict(list)
    for g in google:
        g.update(categoria="novo", motivo="", par="", dist="")
        if g["sha"] in shas:
            g.update(categoria="duplicada", motivo="arquivo identico no iCloud", par=shas[g["sha"]]["caminho"])
            continue
        ext = Path(g["caminho"]).suffix.lower()
        if ext in RAW or ext == ".mp":
            g.update(categoria="revisar", motivo="formato que o iCloud pode nao aceitar")
            continue
        if ext in VIDEOS:
            achados = [i for i in candidatos_por_data(g["instante"], por_data)
                       if Path(i["caminho"]).suffix.lower() in VIDEOS]
            com_duracao = [i for i in achados if g["duracao"] and i["duracao"]
                           and abs(g["duracao"] - i["duracao"]) <= 1]
            if com_duracao:
                reivindicados[com_duracao[0]["caminho"]].append((0, g))
                g.update(categoria="duplicada", motivo="video com mesma data e duracao", par=com_duracao[0]["caminho"])
            elif achados:
                g.update(categoria="revisar", motivo="video com a mesma data", par=achados[0]["caminho"])
            continue
        if not g["dhash"]:
            continue
        melhor, menor = None, 65
        for i in candidatos_por_data(g["instante"], por_data):
            if i["dhash"] and proporcao_ok(g, i):
                d = distancia(g["dhash"], i["dhash"])
                if d < menor:
                    melhor, menor = i, d
        if melhor and menor <= DIST_DUPLICATA:
            reivindicados[melhor["caminho"]].append((menor, g))
            g.update(categoria="duplicada", motivo="mesma foto e mesma data, em outra qualidade",
                     par=melhor["caminho"], dist=menor)
        elif melhor and menor <= DIST_PARECIDA:
            g.update(categoria="revisar", motivo="foto parecida com a mesma data", par=melhor["caminho"], dist=menor)
        else:
            vistos = {}
            for n in range(4):
                for i in faixas.get((n, g["dhash"][n * 4:n * 4 + 4]), ()):
                    vistos[i["caminho"]] = i
            parecidas = [(distancia(g["dhash"], i["dhash"]), i["caminho"]) for i in vistos.values() if proporcao_ok(g, i)]
            parecidas = [p for p in parecidas if p[0] <= DIST_SEM_DATA]
            if parecidas:
                d, caminho = min(parecidas)
                g.update(categoria="revisar", motivo="foto muito parecida com data diferente", par=caminho, dist=d)

    # Fotos em sequencia: um item do iCloud so pode ser duplicata de um item do Google.
    for caminho, lista in reivindicados.items():
        lista.sort(key=lambda par: par[0])
        for _, g in lista[1:]:
            g.update(categoria="revisar", motivo="possivel foto em sequencia", par=caminho)

    for g in google:
        if g["categoria"] == "novo" and not g["instante"]:
            g.update(categoria="revisar", motivo="sem data confiavel")


def marcar_possiveis_videos_na_nuvem(google, nuvem):
    """Videos do iCloud nao baixados nao podem ser comparados: na duvida, vao para revisao."""
    dados = [(p.name.lower(), p.stat().st_size, int(p.stat().st_mtime), str(p)) for p in nuvem]
    for g in google:
        p = Path(g["caminho"])
        if g["categoria"] != "novo" or p.suffix.lower() not in VIDEOS:
            continue
        for nome, tamanho, mtime, caminho in dados:
            if p.name.lower() == nome or g["tamanho"] == tamanho or abs((g["instante"] or 0) - mtime) <= 86400:
                g.update(categoria="revisar", motivo="pode ser um video do iCloud que nao foi baixado", par=caminho)
                break


def remover_copias_internas(itens):
    grupos = defaultdict(list)
    for it in itens:
        grupos[it["sha"]].append(it)
    unicos, copias = [], 0
    for grupo in grupos.values():
        grupo.sort(key=lambda x: (x["js"] is None,
                                  not re.search(r"(Photos from|Fotos de) \d{4}", x["caminho"]), len(x["caminho"])))
        melhor = grupo[0]
        if melhor["js"] is None:
            melhor["js"] = next((x["js"] for x in grupo if x["js"]), None)
        unicos.append(melhor)
        copias += len(grupo) - 1
    return unicos, copias


def marcar_originais_de_editadas(itens):
    por_nome = {(str(Path(i["caminho"]).parent).lower(), Path(i["caminho"]).name.lower()): i for i in itens}
    for it in itens:
        p = Path(it["caminho"])
        _, original, ext, contador, editada = separar_nome(p.name)
        if not editada:
            continue
        nome = f"{original}({contador}){ext}" if contador else original + ext
        o = por_nome.get((str(p.parent).lower(), nome.lower()))
        if o and o["categoria"] == "novo":
            o.update(categoria="revisar", motivo="original de uma foto editada (a editada sera enviada)", par=it["caminho"])


# ---------------------------------------------------------------- Comandos

def fuso_do_sistema(txt):
    if txt:
        f = ler_offset(txt)
        if not f:
            sys.exit("Use o fuso no formato -03:00")
        return f
    return timezone(datetime.now().astimezone().utcoffset())


def extrair_zips(takeout, saida):
    zips = sorted(takeout.glob("*.zip"))
    if not zips:
        return takeout
    destino = saida / "takeout_extraido"
    destino.mkdir(exist_ok=True)
    total = sum(i.file_size for z in zips for i in zipfile.ZipFile(z).infolist())
    if shutil.disk_usage(saida).free < total * 1.1:
        sys.exit(f"Espaco insuficiente: a extracao precisa de {total / 1e9:.1f} GB.")
    for z in zips:
        marca = destino / f".{z.name}.ok"
        if marca.exists():
            continue
        print(f"  Extraindo {z.name}...", flush=True)
        with zipfile.ZipFile(z) as arq:
            arq.extractall(destino)
        marca.touch()
    return destino


def carregar_decisoes(saida):
    arq = saida / "revisar.csv"
    if not arq.exists():
        return {}
    with open(arq, encoding="utf-8-sig", newline="") as f:
        return {l["sha256"]: l["decisao"].strip().lower() for l in csv.DictReader(f, delimiter=";")}


def miniatura(origem, destino):
    try:
        with Image.open(origem) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((220, 220))
            im.save(destino, quality=75)
        return destino.name
    except Exception:
        return ""


def gerar_relatorio(saida, itens, copias):
    campos = ["categoria", "motivo", "caminho", "par", "dist", "data", "fonte_data", "sha"]
    with open(saida / "relatorio.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, campos, delimiter=";", extrasaction="ignore")
        w.writeheader()
        w.writerows(itens)
    decisoes = carregar_decisoes(saida)
    revisar = [i for i in itens if i["categoria"] == "revisar"]
    with open(saida / "revisar.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["decisao", "motivo", "arquivo", "icloud", "data", "sha256"])
        for i in revisar:
            w.writerow([decisoes.get(i["sha"], ""), i["motivo"], i["caminho"], i["par"], i["data"], i["sha"]])

    pasta = saida / "miniaturas"
    pasta.mkdir(exist_ok=True)
    cont = Counter(i["categoria"] for i in itens)
    blocos = []
    for n, i in enumerate(revisar[:500]):
        a = miniatura(i["caminho"], pasta / f"{n}_g.jpg")
        b = miniatura(i["par"], pasta / f"{n}_i.jpg") if i["par"] else ""
        img = lambda x: f'<img src="miniaturas/{x}">' if x else "<span>sem previa</span>"
        blocos.append(f'<div class="c"><div class="f">{img(a)}{img(b)}</div><b>{html.escape(i["motivo"])}</b>'
                      f'<small>{html.escape(Path(i["caminho"]).name)} · {html.escape(i["data"])}</small></div>')
    pagina = f"""<!doctype html><html lang="pt-BR"><meta charset="utf-8"><title>Migração de fotos</title>
<style>body{{font:15px system-ui;margin:32px;color:#222}}.k{{display:flex;gap:12px;flex-wrap:wrap}}
.k div{{border:1px solid #ddd;border-radius:8px;padding:12px 18px}}.k b{{display:block;font-size:24px}}
.g{{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:12px}}
.c{{border:1px solid #ddd;border-radius:8px;padding:10px}}.f{{display:flex;gap:6px}}.f img{{width:150px;height:150px;object-fit:cover}}
small{{display:block;color:#666;overflow-wrap:anywhere}}</style>
<h1>Migração de fotos</h1><div class="k">
<div><b>{cont['novo']}</b>novas, serão enviadas</div><div><b>{cont['duplicada']}</b>já estão no iCloud</div>
<div><b>{cont['revisar']}</b>para revisar</div><div><b>{copias}</b>cópias repetidas no Takeout</div></div>
<h2>Para revisar</h2><p>À esquerda, Google Fotos. À direita, iCloud. Para enviar um item, escreva
<code>enviar</code> na coluna <code>decisao</code> de <code>revisar.csv</code>. Itens em branco não são enviados.</p>
<div class="g">{''.join(blocos)}</div></html>"""
    (saida / "relatorio.html").write_text(pagina, encoding="utf-8")
    return cont


def cmd_analisar(a):
    takeout, icloud, saida = Path(a.takeout).resolve(), Path(a.icloud).resolve(), Path(a.saida).resolve()
    for p in (takeout, icloud):
        if not p.is_dir():
            sys.exit(f"Pasta nao encontrada: {p}")
        if saida == p or p in saida.parents:
            sys.exit("A pasta de saida precisa ficar fora das pastas do Takeout e do iCloud.")
    saida.mkdir(parents=True, exist_ok=True)
    fuso = fuso_do_sistema(a.fuso)
    raiz = extrair_zips(takeout, saida)
    db = abrir_banco(saida)
    with ExifTool() as et:
        print("Lendo o iCloud...", flush=True)
        nuvem = []
        if a.aceitar_videos_na_nuvem:
            nuvem = [p for p in listar_midia(icloud) if somente_na_nuvem(p)]
            if any(p.suffix.lower() not in VIDEOS for p in nuvem):
                sys.exit("Ha fotos somente na nuvem. Esta opcao aceita apenas videos.")
            print(f"  {len(nuvem)} video(s) do iCloud nao baixados: possiveis pares irao para revisao.", flush=True)
        ic = indexar(db, et, icloud, fuso, "iCloud", ignorar=frozenset(nuvem))
        print("Lendo o Takeout...", flush=True)
        go = indexar(db, et, raiz, fuso, "Takeout")
    for rotulo, lista in (("iCloud", ic), ("Takeout", go)):
        if len(lista) >= 20 and sum(1 for i in lista if i["data"]) < len(lista) * 0.2:
            sys.exit(f"Leitura suspeita: menos de 20% dos arquivos de {rotulo} tem data interna. Analise interrompida.")
    jsons = carregar_jsons(raiz)
    for g in go:
        g["js"] = achar_json(g["caminho"], jsons.get(str(Path(g["caminho"]).parent), []))
    # Itens apagados no Google Fotos nao fazem parte da migracao.
    lixeira = [g for g in go if (g["js"] or {}).get("trashed") or
               {"lixeira", "trash"} & {x.lower() for x in Path(g["caminho"]).relative_to(raiz).parts[:-1]}]
    ids_lixeira = {id(g) for g in lixeira}
    go = [g for g in go if id(g) not in ids_lixeira]
    print(f"  {len(lixeira)} item(ns) da Lixeira ignorados.", flush=True)
    go, copias = remover_copias_internas(go)
    for g in go:
        g["instante"], g["fonte_data"], g["gravar"] = resolver_data(g, g["js"], fuso)
        g["data"] = (datetime.fromtimestamp(g["instante"], fuso).strftime("%Y-%m-%d %H:%M:%S") if g["instante"] else "")
    classificar(go, ic)
    marcar_possiveis_videos_na_nuvem(go, nuvem)
    marcar_originais_de_editadas(go)
    cont = gerar_relatorio(saida, go, copias)
    estado = [{k: g.get(k) for k in ("caminho", "sha", "categoria", "instante", "gravar", "tem_gps")}
              | {"gps": extrair_gps(g["js"]), "descricao": (g["js"] or {}).get("description") or ""} for g in go]
    (saida / "analise.json").write_text(json.dumps({"fuso": a.fuso or fuso.tzname(None).replace("UTC", "") or "+00:00",
                                                    "total_icloud": len(ic) + len(nuvem),
                                                    "itens": estado}, ensure_ascii=False), encoding="utf-8")
    print(f"\nNovas: {cont['novo']} | Ja no iCloud: {cont['duplicada']} | Revisar: {cont['revisar']} | "
          f"Copias no Takeout: {copias}\nRelatorio: {saida / 'relatorio.html'}")


def args_metadados(it, ext, dt):
    local = dt.strftime(FORMATO)
    off = dt.strftime("%z")
    off = f"{off[:3]}:{off[3:5]}"
    args = []
    if it["gravar"]:
        if ext in COM_EXIF:
            args += [f"-EXIF:DateTimeOriginal={local}", f"-EXIF:CreateDate={local}", f"-EXIF:OffsetTimeOriginal={off}"]
        if ext in COM_EXIF or ext == ".gif":
            args += [f"-XMP-exif:DateTimeOriginal={local}{off}"]
        if ext in QUICKTIME:
            args += [f"-QuickTime:CreateDate={local}{off}", f"-Keys:CreationDate={local}{off}"]
    g = it.get("gps")
    if g and not it.get("tem_gps"):
        if ext in COM_EXIF:
            args += [f"-GPSLatitude={abs(g['lat'])}", f"-GPSLatitudeRef={'N' if g['lat'] >= 0 else 'S'}",
                     f"-GPSLongitude={abs(g['lon'])}", f"-GPSLongitudeRef={'E' if g['lon'] >= 0 else 'W'}"]
        elif ext in QUICKTIME:
            args += [f"-Keys:GPSCoordinates#={g['lat']} {g['lon']} {g['alt']}"]
    if it.get("descricao") and (ext in COM_EXIF or ext == ".gif"):
        args += [f"-XMP-dc:Description={it['descricao']}"]
    return args


def carregar_manifesto(saida):
    arq = saida / "manifesto.json"
    return json.loads(arq.read_text(encoding="utf-8")) if arq.exists() else {}


def salvar_manifesto(saida, m):
    tmp = saida / "manifesto.tmp"
    tmp.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, saida / "manifesto.json")


def cmd_preparar(a):
    saida = Path(a.saida).resolve()
    if not (saida / "analise.json").exists():
        sys.exit("Rode a etapa 'analisar' primeiro.")
    estado = json.loads((saida / "analise.json").read_text(encoding="utf-8"))
    fuso = ler_offset(estado["fuso"]) or timezone.utc
    decisoes = carregar_decisoes(saida)
    fila = [i for i in estado["itens"] if i["instante"] and
            (i["categoria"] == "novo" or (i["categoria"] == "revisar" and decisoes.get(i["sha"]) == "enviar"))]
    pasta = saida / "para_enviar"
    pasta.mkdir(parents=True, exist_ok=True)
    manifesto = carregar_manifesto(saida)
    usados = {Path(m["arquivo"]).name.lower() for m in manifesto.values()}
    erros = []
    with ExifTool() as et:
        for n, it in enumerate(fila, 1):
            if it["sha"] in manifesto and Path(manifesto[it["sha"]]["arquivo"]).exists():
                continue
            origem = Path(it["caminho"])
            converter = origem.suffix.lower() in HEIC and not a.manter_heic
            sufixo = ".jpg" if converter else origem.suffix
            ext = sufixo.lower()
            dt = datetime.fromtimestamp(it["instante"], fuso)
            nome = origem.stem + sufixo
            k = 1
            while nome.lower() in usados:
                nome = f"{origem.stem}_{k}{sufixo}"
                k += 1
            destino = pasta / nome
            try:
                if converter:
                    converter_heic(et, origem, destino)
                else:
                    shutil.copyfile(origem, destino)
                meta_args = args_metadados(it, ext, dt)
                if meta_args:
                    r = et.rodar(["-overwrite_original", "-m"] + meta_args + [destino.as_posix()])
                    if "1 image files updated" not in r:
                        raise RuntimeError(r.strip()[:200])
                stamp = dt.strftime(FORMATO) + dt.strftime("%z")
                et.rodar(["-overwrite_original", f"-FileModifyDate={stamp}", f"-FileCreateDate={stamp}", destino.as_posix()])
                os.utime(destino, (it["instante"], it["instante"]))
                if ext in COM_EXIF | QUICKTIME | {".gif"}:
                    lido = next(iter(et.ler([destino]).values()), {})
                    t = data_dos_metadados(lido, fuso)
                    if ext == ".gif" and t is None:
                        t = it["instante"]
                    if t is None or abs(t - it["instante"]) > TOLERANCIA_S:
                        raise RuntimeError(f"data gravada nao confere: {t} != {it['instante']}")
                usados.add(nome.lower())
                manifesto[it["sha"]] = {"origem": str(origem), "arquivo": str(destino), "sha": sha256(destino), "enviado": ""}
            except Exception as e:
                destino.unlink(missing_ok=True)
                erros.append((str(origem), str(e)))
            if n % 50 == 0:
                salvar_manifesto(saida, manifesto)
                print(f"  {n}/{len(fila)} preparados", flush=True)
    salvar_manifesto(saida, manifesto)
    with open(saida / "erros_preparo.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["arquivo", "erro"])
        w.writerows(erros)
    print(f"Preparados: {len(manifesto)} | Erros: {len(erros)}\nPasta: {pasta}")
    if estado.get("total_icloud") is not None:
        print(f"Depois do envio, o iCloud deve ter {estado['total_icloud'] + len(manifesto)} itens "
              f"({estado['total_icloud']} de hoje + {len(manifesto)} novos).")
    if not a.manter_heic:
        print("Arraste todos os arquivos dessa pasta para https://www.icloud.com/photos")


def converter_heic(et, origem, destino):
    """HEIC -> JPEG de alta qualidade, com todos os metadados e conferencia visual."""
    with Image.open(origem) as im:
        icc = im.info.get("icc_profile")
        img = ImageOps.exif_transpose(im).convert("RGB")
        img.save(destino, "JPEG", quality=95, subsampling=0, icc_profile=icc)
    r = et.rodar(["-overwrite_original", "-m", "-tagsFromFile", origem.as_posix(), "-all:all", "-unsafe",
                  "-Orientation#=1", "-ThumbnailImage=", destino.as_posix()])
    if "1 image files updated" not in r:
        raise RuntimeError("metadados nao copiados: " + r.strip()[:150])
    h_o, w_o, a_o = hash_visual(origem)
    h_j, w_j, a_j = hash_visual(destino)
    if (w_o, a_o) != (w_j, a_j):
        raise RuntimeError(f"orientacao/dimensoes mudaram na conversao: {(w_o, a_o)} -> {(w_j, a_j)}")
    if h_o and h_j and distancia(h_o, h_j) > 2:
        raise RuntimeError("imagem mudou na conversao")


def cmd_enviar(a):
    saida = Path(a.saida).resolve()
    destino = Path(a.destino).resolve()
    if not destino.is_dir():
        sys.exit(f"Pasta do iCloud nao encontrada: {destino}")
    manifesto = carregar_manifesto(saida)
    pendentes = [m for m in manifesto.values() if not m["enviado"] and not m.get("excluido")]
    lote = pendentes[:a.lote]
    tamanho = sum(Path(m["arquivo"]).stat().st_size for m in lote)
    print(f"Pendentes: {len(pendentes)} | Neste lote: {len(lote)} ({tamanho / 1e9:.2f} GB)")
    if not a.confirmar:
        print("Simulacao: nada foi copiado. Use --confirmar para enviar este lote.")
        return
    if shutil.disk_usage(destino).free < tamanho * 1.1:
        sys.exit("Espaco insuficiente no disco de destino.")
    enviados = 0
    for m in lote:
        origem = Path(m["arquivo"])
        alvo = destino / origem.name
        k = 1
        while alvo.exists():
            alvo = destino / f"{origem.stem}_m{k}{origem.suffix}"
            k += 1
        shutil.copy2(origem, alvo)
        if sha256(alvo) != m["sha"]:
            alvo.unlink()
            print(f"Falha na copia, tente de novo: {origem.name}")
            continue
        m["enviado"] = str(alvo)
        enviados += 1
        if enviados % 50 == 0:
            salvar_manifesto(saida, manifesto)
    salvar_manifesto(saida, manifesto)
    print(f"Copiados para a pasta do iCloud: {enviados} | Ainda pendentes: {len(pendentes) - enviados}")


def cmd_conferir(a):
    saida = Path(a.saida).resolve()
    manifesto = carregar_manifesto(saida)
    ok, faltando, pendentes = 0, [], 0
    for m in manifesto.values():
        if m.get("excluido") or m["enviado"].startswith("web:"):
            continue  # fora da migracao, ou enviado pelo site e conferido no total do iCloud
        if not m["enviado"]:
            pendentes += 1
        elif Path(m["enviado"]).exists() and sha256(m["enviado"]) == m["sha"]:
            ok += 1
        else:
            faltando.append(m["enviado"])
    print(f"Integros: {ok} | Com problema: {len(faltando)} | Ainda nao enviados: {pendentes}")
    for f in faltando[:20]:
        print("  ", f)
    print("Isto confere a pasta local. Compare tambem o total no app Fotos antes de apagar algo do Google.")
    if faltando:
        sys.exit(1)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    padrao = Path.home() / "Pictures" / "iCloud Photos" / "Photos"
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="comando", required=True)
    p = sub.add_parser("analisar")
    p.add_argument("--takeout", required=True)
    p.add_argument("--icloud", default=str(padrao))
    p.add_argument("--saida", required=True)
    p.add_argument("--fuso", help="ex.: -03:00 (padrao: fuso do Windows)")
    p.add_argument("--aceitar-videos-na-nuvem", action="store_true",
                   help="segue sem os videos nao baixados; possiveis pares vao para revisao")
    p.set_defaults(f=cmd_analisar)
    p = sub.add_parser("preparar")
    p.add_argument("--saida", required=True)
    p.add_argument("--manter-heic", action="store_true",
                   help="nao converte HEIC (somente para a etapa 'enviar'; o site nao aceita HEIC)")
    p.set_defaults(f=cmd_preparar)
    p = sub.add_parser("enviar")
    p.add_argument("--saida", required=True)
    p.add_argument("--destino", default=str(padrao))
    p.add_argument("--lote", type=int, default=500)
    p.add_argument("--confirmar", action="store_true")
    p.set_defaults(f=cmd_enviar)
    p = sub.add_parser("conferir")
    p.add_argument("--saida", required=True)
    p.set_defaults(f=cmd_conferir)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
