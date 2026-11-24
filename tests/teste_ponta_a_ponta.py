"""Teste de ponta a ponta com fotos e videos sinteticos (nenhuma foto real).

Uso:  python tests/teste_ponta_a_ponta.py
"""
import csv
import hashlib
import importlib.util
import json
import random
import shutil
import struct
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image
import pillow_heif

pillow_heif.register_heif_opener()

SCRIPT = Path(__file__).resolve().parents[1] / "migrar_fotos.py"
_spec = importlib.util.spec_from_file_location("migrar_fotos", SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
EXIFTOOL = _mod.achar_exiftool()

BASE = Path(tempfile.mkdtemp(prefix="teste_migracao_"))
GF = BASE / "takeout" / "Takeout" / "Google Fotos"
TK, ALB, LIX = GF / "Photos from 2019", GF / "Viagem", GF / "Lixeira"
IC, SAIDA, DEST = BASE / "icloud", BASE / "saida", BASE / "destino"
for p in (TK, ALB, LIX, IC, DEST):
    p.mkdir(parents=True)
TZ = timezone(timedelta(hours=-3))
F = "%Y:%m:%d %H:%M:%S"


def foto(seed, size=(800, 600)):
    r = random.Random(seed)
    im = Image.new("RGB", (16, 12))
    im.putdata([(r.randrange(256), r.randrange(256), r.randrange(256)) for _ in range(192)])
    return im.resize(size, Image.Resampling.BICUBIC)


def exif(*args):
    entrada = "\n".join(["-charset", "filename=utf8", *map(str, args)]).encode("utf-8")
    return subprocess.run([EXIFTOOL, "-@", "-"], input=entrada, capture_output=True, check=True).stdout


def data_exif(path, dt):
    exif("-q", "-overwrite_original", f"-DateTimeOriginal={dt:{F}}", Path(path).as_posix())


def sidecar(media, ts, nome=None, titulo=None, geo=None, criada=None, lixeira=False):
    d = {"title": titulo or media.name, "photoTakenTime": {"timestamp": str(int(ts))},
         "creationTime": {"timestamp": str(int(criada or ts + 86400))}}
    if geo:
        d["geoDataExif"] = {"latitude": geo[0], "longitude": geo[1], "altitude": 0}
    if lixeira:
        d["trashed"] = True
    (media.parent / (nome or media.name + ".supplemental-metadata.json")).write_text(json.dumps(d), encoding="utf-8")


def mp4(path, segundos, dt=None, recheio=b"a"):
    """MP4 minimo e valido, suficiente para o ExifTool ler e gravar metadados."""
    epoca = int(dt.timestamp()) + 2082844800 if dt else 0
    mvhd = struct.pack(">IIIII", 0, epoca, epoca, 1000, segundos * 1000) + struct.pack(">IH", 0x10000, 0x100) + bytes(10)
    mvhd += struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000) + bytes(24) + struct.pack(">I", 2)
    caixa = lambda t, c: struct.pack(">I", len(c) + 8) + t + c
    path.write_bytes(caixa(b"ftyp", b"isom\0\0\2\0isommp42") + caixa(b"moov", caixa(b"mvhd", mvhd))
                     + caixa(b"mdat", recheio * 2000))


# ------------------------------------------------------------ cenarios
D = datetime(2019, 5, 10, 14, 30, 0, tzinfo=TZ)
dia = lambda n: D + timedelta(days=n)

foto(1).save(IC / "identica.jpg", quality=95); data_exif(IC / "identica.jpg", D)
shutil.copy(IC / "identica.jpg", TK / "identica.jpg"); sidecar(TK / "identica.jpg", D.timestamp())

foto(2).save(IC / "IMG_2.heic", quality=90); data_exif(IC / "IMG_2.heic", D + timedelta(hours=1))
foto(2, (400, 300)).save(TK / "comprimida.jpg", quality=55); data_exif(TK / "comprimida.jpg", D + timedelta(hours=1))
sidecar(TK / "comprimida.jpg", (D + timedelta(hours=1)).timestamp())

foto(3).save(TK / "nova_json.jpg", quality=90); sidecar(TK / "nova_json.jpg", dia(1).timestamp(), geo=(-23.55, -46.63))

LONGO = "uma_foto_com_nome_bem_comprido_mesmo_para_testar.jpg"
foto(4).save(TK / LONGO, quality=90); sidecar(TK / LONGO, dia(2).timestamp(), nome=LONGO[:38] + ".json", titulo=LONGO)

foto(5).save(TK / "IMG.jpg", quality=90); sidecar(TK / "IMG.jpg", dia(3).timestamp())
foto(6).save(TK / "IMG(1).jpg", quality=90)
sidecar(TK / "IMG(1).jpg", dia(4).timestamp(), nome="IMG.jpg.supplemental-metadata(1).json", titulo="IMG.jpg")

foto(7).save(TK / "album.jpg", quality=90); sidecar(TK / "album.jpg", dia(5).timestamp())
shutil.copy(TK / "album.jpg", ALB / "album.jpg"); sidecar(ALB / "album.jpg", dia(5).timestamp())

foto(8).save(IC / "burst.jpg", quality=95); data_exif(IC / "burst.jpg", dia(6))
for n, q in (("burst_a.jpg", 70), ("burst_b.jpg", 60)):
    foto(8).save(TK / n, quality=q); data_exif(TK / n, dia(6)); sidecar(TK / n, dia(6).timestamp())

foto(9).save(TK / "sem_data.png")

foto(10).save(TK / "IMG_20150704_101500.jpg", quality=90)
upload = datetime(2020, 1, 1, tzinfo=TZ).timestamp()
sidecar(TK / "IMG_20150704_101500.jpg", upload, criada=upload)

foto(11).save(IC / "outra.jpg", quality=90); data_exif(IC / "outra.jpg", dia(8))
foto(12).save(TK / "diferente.jpg", quality=90); sidecar(TK / "diferente.jpg", dia(8).timestamp())

foto(13).save(TK / "viagem.jpg", quality=90); sidecar(TK / "viagem.jpg", dia(9).timestamp())
foto(14).save(TK / "viagem-editado.jpg", quality=90)

foto(15).save(TK / "São João.jpg", quality=90); sidecar(TK / "São João.jpg", dia(10).timestamp())

foto(16, (600, 800)).save(TK / "novo_iphone.heic", quality=85); sidecar(TK / "novo_iphone.heic", dia(11).timestamp())

mp4(TK / "video_novo.mp4", 10); sidecar(TK / "video_novo.mp4", dia(12).timestamp(), geo=(-22.9, -43.2))
mp4(IC / "video_ic.mov", 20, dia(13), b"b"); mp4(TK / "video_dup.mp4", 20, dia(13), b"c")
sidecar(TK / "video_dup.mp4", dia(13).timestamp())

foto(17).save(LIX / "apagada.jpg", quality=90); sidecar(LIX / "apagada.jpg", dia(14).timestamp(), lixeira=True)
foto(18).save(TK / "marcada_lixeira.jpg", quality=90)
sidecar(TK / "marcada_lixeira.jpg", dia(15).timestamp(), lixeira=True)


def hashes(raiz):
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in raiz.rglob("*") if p.is_file()}


antes_takeout, antes_icloud = hashes(BASE / "takeout"), hashes(IC)
falhas = []


def chk(ok, msg):
    print("OK   " if ok else "FALHA", msg)
    if not ok:
        falhas.append(msg)


def rodar(*args):
    r = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode:
        print(r.stdout[-1500:], r.stderr[-1500:])
        sys.exit(f"comando falhou: {args[0]}")
    return r.stdout


def lido(caminho):
    saida = exif("-j", "-n", "-api", "QuickTimeUTC=1", "-DateTimeOriginal", "-CreationDate", "-GPSLatitude",
                 "-GPSLongitude", "-Orientation", "-ImageSize", Path(caminho).as_posix())
    return json.loads(saida.decode("utf-8"))[0]


# ------------------------------------------------------------ analisar
out = rodar("analisar", "--takeout", str(BASE / "takeout"), "--icloud", str(IC), "--saida", str(SAIDA), "--fuso", "-03:00")
with open(SAIDA / "relatorio.csv", encoding="utf-8-sig", newline="") as f:
    linhas = list(csv.DictReader(f, delimiter=";"))
rel = {Path(l["caminho"]).name: l for l in linhas}
cat = lambda n: rel.get(n, {}).get("categoria")

chk(cat("identica.jpg") == "duplicada", "arquivo identico detectado")
chk(cat("comprimida.jpg") == "duplicada", "versao comprimida detectada")
for n in ("nova_json.jpg", LONGO, "IMG.jpg", "IMG(1).jpg", "album.jpg", "IMG_20150704_101500.jpg",
          "diferente.jpg", "viagem-editado.jpg", "São João.jpg", "novo_iphone.heic", "video_novo.mp4"):
    chk(cat(n) == "novo", f"{n} e novo ({cat(n)})")
chk(sorted([cat("burst_a.jpg"), cat("burst_b.jpg")]) == ["duplicada", "revisar"], "foto em sequencia nao descartada")
chk(cat("sem_data.png") == "revisar", "sem data vai para revisao")
chk(cat("viagem.jpg") == "revisar", "original de foto editada vai para revisao")
chk(cat("video_dup.mp4") == "duplicada", "video duplicado detectado")
chk(sum(1 for l in linhas if Path(l["caminho"]).name == "album.jpg") == 1, "copia em album unificada")
chk("apagada.jpg" not in rel and "marcada_lixeira.jpg" not in rel, "itens da Lixeira ignorados")
chk("2 item(ns) da Lixeira ignorados" in out, "Lixeira informada na saida")

# ------------------------------------------------------------ preparar
out = rodar("preparar", "--saida", str(SAIDA))
man = json.loads((SAIDA / "manifesto.json").read_text(encoding="utf-8"))
arquivos = {Path(m["arquivo"]).name: Path(m["arquivo"]) for m in man.values()}
pasta = SAIDA / "para_enviar"
chk(all(p.parent == pasta for p in arquivos.values()), "tudo numa unica pasta")
chk(len(list(pasta.iterdir())) == len(man) == 11, f"11 arquivos preparados ({len(man)})")
chk("novo_iphone.jpg" in arquivos and not any(n.lower().endswith(".heic") for n in arquivos), "HEIC convertido em JPEG")
chk("deve ter 16 itens (5 de hoje + 11 novos)" in out, "total esperado no iCloud informado")
for n, t in (("nova_json.jpg", dia(1)), (LONGO, dia(2)), ("IMG.jpg", dia(3)), ("IMG(1).jpg", dia(4)),
             ("album.jpg", dia(5)), ("São João.jpg", dia(10)), ("novo_iphone.jpg", dia(11)),
             ("IMG_20150704_101500.jpg", datetime(2015, 7, 4, 10, 15, tzinfo=TZ))):
    valor = lido(arquivos[n]).get("DateTimeOriginal") if n in arquivos else None
    chk(valor == t.strftime(F), f"data de {n}: {valor}")
g = lido(arquivos["nova_json.jpg"])
chk(abs(g.get("GPSLatitude", 0) + 23.55) < .001 and abs(g.get("GPSLongitude", 0) + 46.63) < .001, "GPS negativo gravado")
h = lido(arquivos["novo_iphone.jpg"])
chk(h.get("ImageSize") == "600 800" and h.get("Orientation") in (None, 1), f"JPEG convertido em pe: {h.get('ImageSize')}")
v = lido(arquivos["video_novo.mp4"])
chk(str(v.get("CreationDate", "")).startswith(dia(12).strftime(F)), f"data do video: {v.get('CreationDate')}")
chk(abs(v.get("GPSLatitude", 0) + 22.9) < .001, "GPS do video gravado")
for n in ("identica.jpg", "comprimida.jpg", "sem_data.png", "viagem.jpg", "video_dup.mp4", "apagada.jpg"):
    chk(n not in arquivos, f"{n} fora do envio")

# ------------------------------------------------------------ enviar (pasta do iCloud para Windows)
rodar("enviar", "--saida", str(SAIDA), "--destino", str(DEST))
chk(not any(DEST.iterdir()), "sem --confirmar nada e copiado")
rodar("enviar", "--saida", str(SAIDA), "--destino", str(DEST), "--confirmar")
chk(len(list(DEST.iterdir())) == len(man), "todos copiados com --confirmar")
rodar("enviar", "--saida", str(SAIDA), "--destino", str(DEST), "--confirmar")
chk(len(list(DEST.iterdir())) == len(man), "repetir o envio nao duplica")
rodar("conferir", "--saida", str(SAIDA))

# ------------------------------------------------------------ seguranca
rodar("analisar", "--takeout", str(BASE / "takeout"), "--icloud", str(IC), "--saida", str(SAIDA), "--fuso", "-03:00")
chk(hashes(BASE / "takeout") == antes_takeout, "Takeout intacto")
chk(hashes(IC) == antes_icloud, "iCloud intacto")

shutil.rmtree(BASE, ignore_errors=True)
print("\nRESULTADO:", "TUDO OK" if not falhas else f"{len(falhas)} FALHA(S)")
for x in falhas:
    print(" -", x)
sys.exit(1 if falhas else 0)
