# app.py - Versión CORREGIDA (sin tokens en ningún lugar)
# app.py - Versión con LOGS detallados
import os
import re
import time
import threading
import requests
import logging
from flask import Flask, Response, request, abort, url_for
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, quote, unquote

# === Configuración de Logging ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

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
        logger.error("❌ canales.txt no encontrado. Asegúrate de que el archivo exista.")
    logger.info(f"✅ Cargados {len(channels)} canales: {channels}")
    return channels

CHANNELS = load_channels()

def extract_m3u8_url(canal):
    url = BASE_URL.format(canal)
    logger.info(f"🔍 Extrayendo m3u8 para canal='{canal}' desde {url}")
    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        response = session.get(url, timeout=10)
        logger.info(f"🌐 GET {url} → Respuesta HTTP {response.status_code} ({len(response.content)} bytes)")

        if response.status_code != 200:
            logger.error(f"❌ HTTP {response.status_code} al cargar la página del canal.")
            return None

        # Verificar si hay protección (como Cloudflare)
        if "cloudflare" in response.text.lower():
            logger.error(f"🛡️  Cloudflare detectado en {url}. El scraping está bloqueado.")
            return None

        soup = BeautifulSoup(response.content, 'html.parser')
        scripts = soup.find_all('script')
        logger.info(f"📄 Página cargada. {len(scripts)} scripts encontrados.")

        # Intentar buscar cualquier .m3u8, incluso sin token
        patterns = [
            r'(https?://[^\s\'"\\<>]+\.m3u8[^\s\'"\\<>]*)',  # Cualquier .m3u8
            r'(https?://[^\s\'"\\<>]+\.m3u8\?[^\'"\\<>]*md5=[^\'"\\<>]*&expires=[^\'"\\<>]*)',  # Con token
        ]

        for i, script in enumerate(scripts):
            if not script.string or len(script.string.strip()) < 50:
                continue  # Saltar scripts vacíos o muy cortos

            script_text = script.string.strip()
            logger.debug(f"📜 Analizando script {i} (longitud={len(script_text)}): {script_text[:200]}...")

            for pattern in patterns:
                match = re.search(pattern, script_text)
                if match:
                    m3u8_url = match.group(1)
                    logger.info(f"✅ m3u8 encontrado con patrón: {pattern}")
                    logger.info(f"🔗 URL extraída: {m3u8_url}")
                    return m3u8_url

        # Si no encontró nada, muestra un resumen útil
        logger.warning(f"❌ No se encontró ninguna URL .m3u8 en los scripts para el canal '{canal}'")
        logger.debug("💡 Para depurar: Prueba abrir manualmente esta URL en el navegador:")
        logger.debug(f"🌐 {url}")
        logger.debug("📌 Busca en las DevTools (Network > Media) qué URL .m3u8 se está cargando realmente.")

        # Opcional: guardar el HTML para analizarlo después
        with open(f"debug_{canal}.html", "w", encoding="utf-8") as f:
            f.write(response.text)
        logger.info(f"💾 HTML guardado en 'debug_{canal}.html' para análisis manual.")

        return None

    except requests.exceptions.RequestException as e:
        logger.error(f"📡 Error de red al obtener la página del canal '{canal}': {e}")
    except Exception as e:
        logger.error(f"💥 Error inesperado extrayendo m3u8 para '{canal}': {e}")
    return None

def rewrite_m3u8(content, base_url, canal):
    """Reescribe el contenido del M3U8 para que todas las URLs pasen por el proxy"""
    logger.info(f"📝 Reescribiendo contenido M3U8 para canal='{canal}' (base_url={base_url})")
    lines = content.splitlines()
    rewritten = []
    in_segment = False
    segment_count = 0
    key_count = 0

    for line in lines:
        stripped = line.strip()
        if stripped.startswith('#EXTINF:'):
            rewritten.append(line)
            in_segment = True
        elif in_segment and stripped and not stripped.startswith('#'):
            if not stripped.startswith('http'):
                abs_url = urljoin(base_url, stripped)
            else:
                abs_url = stripped
            encoded_url = quote(abs_url, safe='')
            proxy_url = url_for('proxy_segment', canal=canal, real_url=encoded_url, _external=True)
            rewritten.append(proxy_url)
            in_segment = False
            segment_count += 1
        elif '#EXT-X-KEY' in stripped and 'URI="' in stripped:
            new_line = re.sub(
                r'(URI=")([^"]+)"',
                lambda m: f'{m.group(1)}{url_for("proxy_segment", canal=canal, real_url=quote(m.group(2), safe=""), _external=True)}"',
                stripped
            )
            rewritten.append(new_line)
            key_count += 1
        else:
            rewritten.append(line)

    logger.info(f"🔄 M3U8 reescrito: {segment_count} segmentos, {key_count} claves reescritas")
    return "\n".join(rewritten)

@app.route('/stream/<canal>.m3u8')
def proxy_playlist(canal):
    if canal not in CHANNELS:
        logger.warning(f"🚫 Acceso denegado: canal '{canal}' no está en la lista permitida.")
        abort(404, "Canal no encontrado")

    cached = STREAM_CACHE.get(canal)
    now = time.time()

    if cached and now < cached.get('expires', 0):
        logger.info(f"🎯 {canal}.m3u8 → ✅ Servido desde caché (queda {int(cached['expires'] - now)}s)")
        m3u8_url = cached['m3u8_url']
        base_url = cached['base_url']
    else:
        if cached:
            logger.info(f"⏳ {canal}.m3u8 → 🕐 Caché caducado (hace {int(now - cached['expires'])}s). Refrescando...")
        else:
            logger.info(f"📥 {canal}.m3u8 → ❌ Sin caché. Extrayendo nueva URL...")

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
                logger.info(f"💾 {canal}.m3u8 → 📦 Guardado en caché hasta {time.strftime('%H:%M:%S', time.localtime(now + CACHE_TTL))}")
            else:
                logger.error(f"🛑 {canal}.m3u8 → ❌ No se pudo obtener la URL del stream")
                abort(500, "No se pudo obtener el stream")

    try:
        logger.info(f"⬇️  Descargando M3U8 original: {m3u8_url}")
        r = requests.get(
            m3u8_url,
            headers={**HEADERS, "Referer": "https://hoca6.com/"},
            timeout=10
        )
        logger.info(f"📥 Respuesta M3U8: HTTP {r.status_code} | Tamaño: {len(r.content)} bytes")

        r.raise_for_status()

        content = r.text
        logger.debug(f"📄 Contenido M3U8 original:\n{content[:500]}...")

        rewritten_content = rewrite_m3u8(content, base_url, canal)
        logger.info(f"✅ {canal}.m3u8 → ✅ Playlist reescrito y listo para servir")

        response = Response(rewritten_content, mimetype="application/x-mpegurl")
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Content-Length'] = str(len(rewritten_content))
        return response

    except requests.exceptions.RequestException as e:
        logger.error(f"📡 Error al descargar el M3U8 de '{canal}': {e}")
        abort(502, "Error de conexión con el origen")
    except Exception as e:
        logger.error(f"💥 Error inesperado procesando playlist '{canal}': {e}")
        abort(500, "Error interno del servidor")

@app.route('/proxy/segment/<canal>')
def proxy_segment(canal):
    if canal not in CHANNELS:
        logger.warning(f"🚫 Segmento: canal '{canal}' no permitido")
        abort(404, "Canal no encontrado")

    encoded_url = request.args.get("real_url")
    if not encoded_url:
        logger.warning(f"🚫 Segmento: URL no especificada para canal '{canal}'")
        abort(400, "URL no especificada")

    real_url = unquote(encoded_url)
    logger.info(f"🔗 Proxyeando recurso: {real_url} (canal={canal})")

    try:
        r = requests.get(
            real_url,
            headers=HEADERS,
            stream=True,
            timeout=15,
            verify=False
        )
        logger.info(f"📥 Segment GET {real_url} → HTTP {r.status_code} | Content-Type: {r.headers.get('Content-Type')}")

        r.raise_for_status()

        headers = {}
        excluded_headers = ['content-length', 'connection', 'transfer-encoding']
        for key, value in r.headers.items():
            if key.lower() not in excluded_headers:
                headers[key] = value
        headers['Access-Control-Allow-Origin'] = '*'
        headers['Cache-Control'] = 'no-cache'

        response = Response(
            r.iter_content(chunk_size=8192),
            status=r.status_code,
            headers=headers
        )
        logger.info(f"📤 Segmento servido exitosamente: {real_url}")
        return response

    except requests.exceptions.RequestException as e:
        logger.error(f"🔻 Error al descargar segmento {real_url}: {e}")
        abort(502, "Error al obtener el segmento")
    except Exception as e:
        logger.error(f"💥 Error inesperado proxyeando {real_url}: {e}")
        abort(500, "Error interno")

@app.route('/m3u')
def generate_m3u():
    base = request.host_url.rstrip("/")
    logger.info(f"📋 Generando lista M3U con {len(CHANNELS)} canales")
    lines = ["#EXTM3U x-tvg-url=\"https://iptv-org.github.io/epg/guides/tvplus.com.epg.xml\""]
    for canal in CHANNELS:
        lines.append(
            f'#EXTINF:-1 tvg-id="{canal}" tvg-name="{canal.title()}" '
            f'group-title="hoca",{canal.title()}\n'
            f'{base}/stream/{canal}.m3u8'
        )
    response = Response("\n".join(lines), mimetype="application/x-mpegurl")
    response.headers['Content-Disposition'] = 'attachment; filename="playlist.m3u"'
    logger.info("📥 Lista M3U generada y descargada")
    return response

@app.route('/')
def home():
    base = request.host_url.rstrip("/")
    links = '<h1>📡 Proxy HLS HOCA</h1><ul>'
    for canal in CHANNELS:
        links += f'<li><a href="/stream/{canal}.m3u8">{canal}</a> | '
        links += f'<a href="{base}/stream/{canal}.m3u8" target="_blank">🔗 URL</a></li>'
    links += '</ul><p><a href="/m3u">📥 Descargar lista M3U</a></p>'
    logger.info("🏠 Página de inicio servida")
    return links

@app.route('/debug/<canal>')
def debug(canal):
    url = BASE_URL.format(canal)
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        return Response(
            f"<pre>{response.text}</pre>",
            mimetype="text/html"
        )
    except Exception as e:
        return f"Error: {e}"

# === Refresco automático en segundo plano ===
def background_refresh():
    while True:
        time.sleep(240)
        logger.info("🔄 Iniciando refresco en segundo plano de URLs m3u8...")
        for canal in CHANNELS:
            logger.debug(f"🔁 Refrescando canal: {canal}")
            threading.Thread(target=extract_m3u8_url, args=(canal,), daemon=True).start()

if __name__ == '__main__':
    threading.Thread(target=background_refresh, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"🚀 Servidor iniciado en http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
