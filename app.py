# app.py
import os
import re
import time
import threading
import requests
from flask import Flask, Response, request, abort, url_for
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, quote, unquote

app = Flask(__name__)

# === URLs y Configuración ===
BASE_URL = "https://rereyano.ru/player/2/{}"  # Página con el iframe
IFRAME_URL = "https://hoca6.com/footy.php?player=desktop&live=ufeed{}"  # El iframe real
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Referer": "https://hoca6.com/",
    "Origin": "https://hoca6.com"
}

STREAM_CACHE = {}
CACHE_TTL = 300  # 5 minutos
LOCK = threading.Lock()

# === Cargar canales ===
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

# === Extraer m3u8 desde el iframe de footy.php ===
def extract_m3u8_url(canal):
    url = IFRAME_URL.format(canal)
    print(f"🔍 Iniciando extracción para canal {canal} desde: {url}")

    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        response = session.get(url, timeout=15)
        print(f"🌐 Respuesta HTTP: {response.status_code} | Tamaño: {len(response.text)} bytes")

        if response.status_code != 200:
            print(f"❌ Estado inesperado: {response.status_code}")
            return None

        html = response.text

        # 🔽 Guardar HTML para análisis manual (útil en Render)
        try:
            with open(f"debug_footy_{canal}.html", "w", encoding="utf-8") as f:
                f.write(f"<!-- URL: {url} -->\n<!-- Time: {time.strftime('%Y-%m-%d %H:%M:%S')} -->\n{html}")
            print(f"💾 HTML guardado localmente como debug_footy_{canal}.html")
        except Exception as e:
            print(f"⚠️  No se pudo guardar HTML: {e}")

        # 🔍 1. Buscar cualquier cosa que parezca un .m3u8
        patterns = [
            # Patrón 1: m3u8 con md5 y expires (tu caso original)
            r'(https?://[^\s\'"\\<>]+\.m3u8\?[^\'"\\<>]*md5=[^\'"\\<>]*&expires=[^\'"\\<>]*)',
            # Patrón 2: m3u8 con token genérico
            r'(https?://[^\s\'"\\]+\.m3u8\?[^\'"\\]+)',
            # Patrón 3: m3u8 sin query (pero con ruta)
            r'(https?://[^\s\'"\\]+\.m3u8)',
            # Patrón 4: entre comillas, posiblemente en JS
            r'["\'](https?://[^\s\'"\\]+\.m3u8[^\s\'"\\]*)["\']',
            # Patrón 5: posiblemente codificado o en variable
            r'(hls|playlist|source|file)[^=]*=[^=]*["\'](https?://[^\s\'"\\]+\.m3u8[^\s\'"\\]*)["\']',
            # Patrón 6: en texto ofuscado o base64 (buscar pistas)
            r'(base64[^\'"]*\.m3u8|decode[^\'"]*\.m3u8)',
        ]

        for i, pattern in enumerate(patterns):
            print(f"🔎 Buscando con patrón {i+1}: {pattern[:50]}...")
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                found = match.group(1) if i < 4 else match.group(2)
                print(f"✅ ¡ENCONTRADO! Con patrón {i+1}: {found}")
                return found

        # 🧩 2. Si no encontró nada, analizar scripts
        soup = BeautifulSoup(html, 'html.parser')
        scripts = soup.find_all('script')
        print(f"📜 {len(scripts)} scripts encontrados. Analizando...")

        for idx, script in enumerate(scripts):
            if not script.string or len(script.string.strip()) < 50:
                continue

            script_text = script.string.strip()
            print(f"📝 Analizando script {idx} (longitud={len(script_text)}): {script_text[:200]}...")

            for i, pattern in enumerate(patterns):
                match = re.search(pattern, script_text, re.IGNORECASE)
                if match:
                    found = match.group(1) if i < 4 else match.group(2)
                    print(f"✅ ¡ENCONTRADO en script {idx}! Con patrón {i+1}: {found}")
                    return found

        # 📊 3. Si sigue sin encontrar, mostrar fragmentos clave
        print("🔍 Fragmentos importantes del HTML:")
        for fragment in ['m3u8', 'hls', 'source', 'player', 'video', 'manifest', 'stream']:
            lines = [line.strip() for line in html.splitlines() if fragment in line.lower()]
            if lines:
                print(f"  🔎 '{fragment}': {lines[:3]}")

        print("❌ No se encontró ninguna URL .m3u8 en el HTML ni scripts")
        return None

    except requests.exceptions.RequestException as e:
        print(f"📡 Error de red: {e}")
    except Exception as e:
        print(f"💥 Error inesperado: {e}")
        import traceback
        traceback.print_exc()
    return None

# === Reescribir M3U8 para pasar por proxy ===
def rewrite_m3u8(content, base_url, canal):
    lines = content.splitlines()
    rewritten = []
    in_segment = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('#EXTINF:'):
            rewritten.append(line)
            in_segment = True
        elif in_segment and stripped and not stripped.startswith('#'):
            abs_url = urljoin(base_url, stripped) if not stripped.startswith('http') else stripped
            encoded = quote(abs_url, safe='')
            proxy_url = url_for('proxy_segment', canal=canal, real_url=encoded, _external=True)
            rewritten.append(proxy_url)
            in_segment = False
        elif '#EXT-X-KEY' in stripped and 'URI="' in stripped:
            new_line = re.sub(
                r'(URI=")([^"]+)"',
                lambda m: f'{m.group(1)}{url_for("proxy_segment", canal=canal, real_url=quote(m.group(2), safe=""), _external=True)}"',
                stripped
            )
            rewritten.append(new_line)
        else:
            rewritten.append(line)
    return "\n".join(rewritten)

# === Ruta: /stream/canal.m3u8 ===
@app.route('/stream/<canal>.m3u8')
def proxy_playlist(canal):
    if canal not in CHANNELS:
        abort(404, "Canal no encontrado")

    cached = STREAM_CACHE.get(canal)
    now = time.time()

    if cached and now < cached.get('expires', 0):
        m3u8_url = cached['m3u8_url']
        base_url = cached['base_url']
        print(f"🎯 Usando caché para {canal}.m3u8")
    else:
        with LOCK:
            m3u8_url = extract_m3u8_url(canal)
            if m3u8_url:
                parsed = urlparse(m3u8_url)
                base_url = f"{parsed.scheme}://{parsed.netloc}{os.path.dirname(parsed.path)}/"
                STREAM_CACHE[canal] = {
                    'm3u8_url': m3u8_url,
                    'base_url': base_url,
                    'expires': now + CACHE_TTL
                }
            else:
                abort(500, "No se pudo obtener el stream")

    try:
        r = requests.get(m3u8_url, headers={**HEADERS, "Referer": "https://hoca6.com/"}, timeout=10)
        r.raise_for_status()
        content = r.text
        rewritten = rewrite_m3u8(content, base_url, canal)

        response = Response(rewritten, mimetype="application/x-mpegurl")
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Cache-Control'] = 'no-cache'
        return response
    except Exception as e:
        print(f"❌ Error descargando .m3u8: {e}")
        abort(502, "Error al obtener el stream original")

# === Ruta: /proxy/segment/canal ===
@app.route('/proxy/segment/<canal>')
def proxy_segment(canal):
    if canal not in CHANNELS:
        abort(404, "Canal no encontrado")
    encoded_url = request.args.get("real_url")
    if not encoded_url:
        abort(400, "URL no especificada")
    real_url = unquote(encoded_url)
    try:
        r = requests.get(real_url, headers=HEADERS, stream=True, timeout=15, verify=False)
        r.raise_for_status()
        headers = {k: v for k, v in r.headers.items() if k.lower() not in ['content-length', 'transfer-encoding']}
        headers['Access-Control-Allow-Origin'] = '*'
        return Response(r.iter_content(chunk_size=8192), status=r.status_code, headers=headers)
    except Exception as e:
        print(f"❌ Error proxyeando: {real_url} → {e}")
        abort(502, "Error al obtener el segmento")

# === Ruta: /m3u ===
@app.route('/m3u')
def generate_m3u():
    base = request.host_url.rstrip("/")
    lines = ["#EXTM3U x-tvg-url=\"https://iptv-org.github.io/epg/guides/tvplus.com.epg.xml\""]
    for c in CHANNELS:
        lines.append(f'#EXTINF:-1 tvg-id="{c}" tvg-name="{c}" group-title="hoca",{c}\n{base}/stream/{c}.m3u8')
    resp = Response("\n".join(lines), mimetype="application/x-mpegurl")
    resp.headers['Content-Disposition'] = 'attachment; filename="playlist.m3u"'
    return resp

# === Ruta: / ===
@app.route('/')
def home():
    base = request.host_url.rstrip("/")
    links = '<h1>📡 Proxy HLS HOCA</h1><ul>'
    for c in CHANNELS:
        links += f'<li><a href="/stream/{c}.m3u8">{c}</a> | <a href="{base}/stream/{c}.m3u8" target="_blank">🔗</a></li>'
    links += '</ul><p><a href="/m3u">📥 Descargar M3U</a></p>'
    return links

# === Refresco automático en segundo plano ===
def background_refresh():
    while True:
        time.sleep(240)
        for canal in CHANNELS:
            threading.Thread(target=extract_m3u8_url, args=(canal,), daemon=True).start()

if __name__ == '__main__':
    threading.Thread(target=background_refresh, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
