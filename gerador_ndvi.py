# -*- coding: utf-8 -*-
"""
Gerador de NDVI (Sentinel-2) para os talhoes do Supabase.
Grava imagem (base64) + data real da cena do satelite.
Roda no GitHub Actions via .github/workflows/atualiza-ndvi.yml
"""
import io
import os
import json
import base64

import requests
from PIL import Image, ImageDraw
from supabase import create_client

# ================== CONFIGURACAO ==================
SUPABASE_URL       = "https://keiydgyountsuzjybsng.supabase.co"
SUPABASE_KEY       = os.environ["SUPABASE_KEY"]
CDSE_CLIENT_ID     = os.environ["CDSE_CLIENT_ID"]
CDSE_CLIENT_SECRET = os.environ["CDSE_CLIENT_SECRET"]
CLOUD_COVER_MAX = 30      # % maximo de nuvem aceito
IMG_SIZE = 512            # resolucao do PNG gerado
# ==================================================


def get_token():
    """Autentica no Copernicus Data Space e retorna o token de acesso."""
    r = requests.post(
        "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": CDSE_CLIENT_ID,
            "client_secret": CDSE_CLIENT_SECRET,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def extrair_poligono(g):
    """Recebe geom (dict GeoJSON) e retorna os aneis do poligono principal."""
    if g.get("type") == "FeatureCollection":
        g = g["features"][0]
    if g.get("type") == "Feature":
        g = g["geometry"]
    if g["type"] == "Polygon":
        return g["coordinates"]
    if g["type"] == "MultiPolygon":
        return g["coordinates"][0]
    raise ValueError("Geometria nao suportada: " + str(g.get("type")))


def bbox_do_anel(anel):
    xs = [p[0] for p in anel]
    ys = [p[1] for p in anel]
    return (min(xs), min(ys), max(xs), max(ys))


def bbox_wkt(bbox):
    """Converte o bbox (retangulo envolvente) em WKT com apenas 4 pontos.
    Usado na busca do catalogo para nao estourar o limite de tamanho da URL."""
    minx, miny, maxx, maxy = bbox
    return (f"POLYGON(({minx} {miny}, {maxx} {miny}, "
            f"{maxx} {maxy}, {minx} {maxy}, {minx} {miny}))")


def ultima_cena(wkt, depois_de):
    """Busca no catalogo Copernicus a cena Sentinel-2 L2A mais recente
    sobre a area, com nuvem abaixo do limite, posterior a depois_de."""
    filtros = [
        "Collection/Name eq 'SENTINEL-2'",
        "contains(Name,'MSIL2A')",
        f"OData.CSC.Intersects(area=geography'SRID=4326;{wkt}')",
        (f"Attributes/OData.CSC.DoubleAttribute/any("
         f"att:att/Name eq 'CloudCover' and "
         f"att/OData.CSC.DoubleAttribute/Value lt {CLOUD_COVER_MAX})"),
        f"ContentDate/Start gt {depois_de}T00:00:00.000Z",
    ]
    r = requests.get(
        "https://catalogue.dataspace.copernicus.eu/odata/v1/Products",
        params={
            "$filter": " and ".join(filtros),
            "$orderby": "ContentDate/Start desc",
            "$top": "1",
        },
        timeout=60,
    )
    r.raise_for_status()
    v = r.json().get("value", [])
    return v[0]["ContentDate"]["Start"][:10] if v else None


EVALSCRIPT = """
//VERSION=3
function setup() {
  return { input: ["B04","B08","dataMask"],
           output: { bands: 4, sampleType: "UINT8" } };
}
function evaluatePixel(s) {
  var ndvi = (s.B08 - s.B04) / (s.B08 + s.B04 + 0.000001);
  var r, g, b;
  if      (ndvi < 0.00) { r = 110; g = 70;  b = 40; }
  else if (ndvi < 0.15) { r = 215; g = 30;  b = 25; }
  else if (ndvi < 0.30) { r = 240; g = 140; b = 40; }
  else if (ndvi < 0.45) { r = 250; g = 220; b = 60; }
  else if (ndvi < 0.60) { r = 130; g = 200; b = 60; }
  else                  { r = 20;  g = 130; b = 40; }
  return [r, g, b, Math.round(s.dataMask * 255)];
}
"""


def gerar_png_ndvi(token, bbox, data_cena):
    """Chama a Process API do Copernicus e retorna os bytes do PNG colorido."""
    body = {
        "input": {
            "bounds": {
                "bbox": list(bbox),
                "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"},
            },
            "data": [{
                "type": "sentinel-2-l2a",
                "dataFilter": {
                    "timeRange": {
                        "from": f"{data_cena}T00:00:00Z",
                        "to": f"{data_cena}T23:59:59Z",
                    },
                    "maxCloudCoverage": CLOUD_COVER_MAX,
                    "mosaickingOrder": "leastCC",
                },
            }],
        },
        "output": {
            "width": IMG_SIZE,
            "height": IMG_SIZE,
            "responses": [{
                "identifier": "default",
                "format": {"type": "image/png"},
            }],
        },
        "evalscript": EVALSCRIPT,
    }
    r = requests.post(
        "https://sh.dataspace.copernicus.eu/api/v1/process",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=180,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Process API {r.status_code}: {r.text[:200]}")
    return r.content


def recortar_no_poligono(png_bytes, aneis, bbox):
    """Aplica mascara alpha deixando visivel apenas a area do poligono.
    Usa o poligono COMPLETO (com todos os vertices) — o recorte continua preciso."""
    minx, miny, maxx, maxy = bbox
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    w, h = img.size

    def px(pontos):
        return [((x - minx) / (maxx - minx) * w,
                 (maxy - y) / (maxy - miny) * h) for x, y in pontos]

    mascara = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mascara)
    d.polygon(px(aneis[0]), fill=255)
    for furo in aneis[1:]:
        d.polygon(px(furo), fill=0)
    img.putalpha(mascara)
    return img


def salvar(sb, talhao_id, img, data_cena):
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    sb.table("talhoes").update({
        "imagem_ndvi": base64.b64encode(buf.getvalue()).decode(),
        "data_ndvi": data_cena,
        "tem_ndvi": True,
    }).eq("id", talhao_id).execute()


def main():
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    print("Autenticando no Copernicus...")
    token = get_token()

    talhoes = sb.table("talhoes").select(
        "id, codigo_talhao, geom, data_ndvi"
    ).execute().data
    print(f"Encontrados {len(talhoes)} talhoes.\n")

    atualizados = 0
    for t in talhoes:
        nome = t["codigo_talhao"]
        try:
            geom = t["geom"]
            if isinstance(geom, str):
                geom = json.loads(geom)

            aneis = extrair_poligono(geom)
            bbox = bbox_do_anel(aneis[0])
            depois = (t.get("data_ndvi") or "2020-01-01")[:10]

            # Busca usa o RETANGULO envolvente (4 pontos) para nao estourar a URL
            cena = ultima_cena(bbox_wkt(bbox), depois)
            if not cena:
                print(f"[{nome}] ja esta em dia.")
                continue

            png = gerar_png_ndvi(token, bbox, cena)
            img = recortar_no_poligono(png, aneis, bbox)
            salvar(sb, t["id"], img, cena)
            atualizados += 1
            print(f"[{nome}] ATUALIZADO com imagem de {cena}")
        except Exception as e:
            print(f"[{nome}] ERRO: {e}")

    print(f"\nConcluido. {atualizados} talhao(ns) atualizado(s).")


if __name__ == "__main__":
    main()
