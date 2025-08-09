# app.py - Versión CORREGIDA (sin tokens en ningún lugar)
import os
import re
import time
import threading
import requests
from flask import Flask, Response, request, abort, url_for
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, quote, unquote

app = Flask(__name__)

BASE_URL = "https://rereyano.ru/player/2/{}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Referer": "https://hoca6.com/"
}

STREAM_CACHE = {}
CACHE_TTL = 300
LOCK = threading.Lock()

def load_channels():
    channels = []
    try:
        with open("canales.txt", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    channels.append(line)
    except FileNotFoundError:
        print("⚠️ canales.txt no encontrado")
    return channels

CHANNELS = load_channels()

def extract_m3u8_url(canal):
    url = BASE_URL.format(canal)
    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        response = session.get(url, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, 'html.parser')
        scripts = soup.find_all('script')

        for script in scripts:
            if script.string:
                # Regex mejorado para capturar cualquier m3u8 con token
                match = re.search(r'(https?://[^\s\'"\\<>]+\.m3u8\?md5=[^\s\'"\\<>]+&expires=[^\s\'"\\<>]+)', script.string)
                if match:
                    return match.group(1)
    except Exception as e:
        print(f"❌ Error extrayendo m3u8 para {canal}: {e}")
    return None

def rewrite_m3u8(content, base_url, canal):
    """Reescribe TODO el contenido del M3U8 para que todas las URLs pasen por el proxy"""
    lines = content.splitlines()
    rewritten = []
    in_segment = False

    for line in lines:
        stripped = line.strip()
        
        # 1. Líneas de metadatos (EXTINF, etc.)
        if stripped.startswith('#EXTINF:'):
            rewritten.append(line)
            in_segment = True
            continue
            
        # 2. Líneas de segmentos (las que siguen a EXTINF)
        if in_segment and stripped:
            # Asegurar que la URL sea absoluta
            if not stripped.startswith('http'):
                abs_url = urljoin(base_url, stripped)
            else:
                abs_url = stripped
                
            # Codificar la URL para el proxy
            encoded_url = quote(abs_url, safe='')
            proxy_url = url_for('proxy_segment', canal=canal, real_url=encoded_url, _external=True)
            rewritten.append(proxy_url)
            in_segment = False
            continue
            
        # 3. Líneas de claves (EXT-X-KEY)
        if '#EXT-X-KEY' in stripped and 'URI="' in stripped:
            # Reescribir la URL de la clave
            new_line = re.sub(
                r'(URI=")([^"]+)"', 
                lambda m: f'{m.group(1)}{url_for("proxy_segment", canal=canal, real_url=quote(m.group(2), safe=""), _external=True)}"', 
                stripped
            )
            rewritten.append(new_line)
            continue
            
        # 4. Otras líneas (dejarlas igual)
        rewritten.append(line)

    return "\n".join(rewritten)

@app.route('/stream/<canal>.m3u8')
def proxy_playlist(canal):
    if canal not in CHANNELS:
        abort(404, "Canal no encontrado")

    # Obtener o actualizar caché
    cached = STREAM_CACHE.get(canal)
    if not cached or time.time() > cached.get('expires', 0):
        with LOCK:
            m3u8_url = extract_m3u8_url(canal)
            if m3u8_url:
                # Calcular base_url correctamente
                parsed = urlparse(m3u8_url)
                base_url = f"{parsed.scheme}://{parsed.netloc}{os.path.dirname(parsed.path)}/"
                
                STREAM_CACHE[canal] = {
                    'm3u8_url': m3u8_url,
                    'base_url': base_url,
                    'expires': time.time() + CACHE_TTL
                }
            else:
                abort(500, "No se pudo obtener el stream")

    cache_info = STREAM_CACHE[canal]
    m3u8_url = cache_info['m3u8_url']

    try:
        # Obtener el M3U8 original
        r = requests.get(
            m3u8_url, 
            headers={**HEADERS, "Referer": "https://hoca6.com/"},
            timeout=10
        )
        r.raise_for_status()

        # Reescribir el contenido
        content = r.text
        rewritten_content = rewrite_m3u8(content, cache_info['base_url'], canal)

        # Configurar headers para HLS
        response = Response(rewritten_content, mimetype="application/x-mpegurl")
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        return response

    except Exception as e:
        print(f"❌ Error al obtener .m3u8: {e}")
        abort(502, "Error de conexión con el origen")

@app.route('/proxy/segment/<canal>')
def proxy_segment(canal):
    if canal not in CHANNELS:
        abort(404, "Canal no encontrado")

    encoded_url = request.args.get("real_url")
    if not encoded_url:
        abort(400, "URL no especificada")

    try:
        # Decodificar la URL
        real_url = unquote(encoded_url)
        
        # Descargar el recurso (ts, key, m3u8)
        r = requests.get(
            real_url, 
            headers=HEADERS, 
            stream=True, 
            timeout=15,
            verify=False  # Necesario para algunos servidores con SSL mal configurado
        )
        r.raise_for_status()

        # Configurar headers para HLS
        headers = {}
        for key, value in r.headers.items():
            if key.lower() not in ['content-length', 'connection', 'transfer-encoding']:
                headers[key] = value
        headers['Access-Control-Allow-Origin'] = '*'
        headers['Cache-Control'] = 'no-cache'

        # Devolver el contenido
        return Response(
            r.iter_content(chunk_size=8192),
            status=r.status_code,
            headers=headers
        )

    except Exception as e:
        print(f"❌ Error proxyeando segmento: {e}")
        abort(502, "Error al obtener el segmento")

@app.route('/m3u')
def generate_m3u():
    base = request.host_url.rstrip("/")
    lines = ["#EXTM3U x-tvg-url=\"https://iptv-org.github.io/epg/guides/tvplus.com.epg.xml\""]

    for canal in CHANNELS:
        lines.append(
            f'#EXTINF:-1 tvg-id="{canal}" tvg-name="{canal.title()}" '
            f'group-title="hoca",{canal.title()}\n'
            f'{base}/stream/{canal}.m3u8'
        )

    response = Response("\n".join(lines), mimetype="application/x-mpegurl")
    response.headers['Content-Disposition'] = 'attachment; filename="playlist.m3u"'
    return response

@app.route('/')
def home():
    base = request.host_url.rstrip("/")
    links = '<h1>📡 Proxy HLS HOCA</h1><ul>'
    for canal in CHANNELS:
        links += f'<li><a href="/stream/{canal}.m3u8">{canal}</a> | '
        links += f'<a href="{base}/stream/{canal}.m3u8" target="_blank">🔗 URL</a></li>'
    links += '</ul><p><a href="/m3u">📥 Descargar lista M3U</a></p>'
    return links

# Refresco automático en segundo plano
def background_refresh():
    while True:
        time.sleep(240)
        for canal in CHANNELS:
            threading.Thread(target=extract_m3u8_url, args=(canal,), daemon=True).start()

if __name__ == '__main__':
    # Iniciar hilo de refresco
    threading.Thread(target=background_refresh, daemon=True).start()
    
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
