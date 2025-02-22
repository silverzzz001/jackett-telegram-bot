import requests
import transmission_rpc
from transmission_rpc import Client, TransmissionError
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Bot, BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler,CallbackContext
import nest_asyncio
import asyncio
import re
import sqlite3
import os
import aiofiles
import time
import shutil
import getpass

# Configuración de APIs
JACKETT_API_URL = "http://127.0.0.1:9117/api/v2.0/indexers/all/results"
JACKETT_API_KEY = "xxxxxxxxxxxxx"  # Reemplaza con tu clave API de Jackett

# Configuración del bot de Telegram
TOKEN_TELEGRAM = "xxxxxx:xxxxxxxxxxxxxxxx"
RESULTADOS_POR_PAGINA = 5
ADMIN_ID = 0000000000  # ID de telegram del administrador del bot

# Filtros de busqueda

incluir_terminos = []

excluir_terminos = [
    "480p", "480", "360p", "360", "240p", "240", "144p", "144",  # resoluciones bajas
    "sd", "vhs", "tvrip", "dvd", "dvdr", "r5", "cam", "ts", "tc", "telesync", "telecine", "sat rip",  # rips de baja calidad
    "screener", "wp", "workprint", "pdtv", "lq", "low quality", "xvid", "divx", "mp4-lq", "x265-lq", "h.264-lq"  # otros formatos de baja calidad
]

calidad_maxima_actual_por_defecto = "4K"

#cambia las rutas por las tuyas
MOVIES_DIR = r'/media/user/discohdd/peliculas'
SERIES_DIR = r'/media/user/discohdd/series'
OTROS_DIR = r'/media/user/discohdd/otros'

# Definir las carpetas de origen
DIRECTORIOS_USB = [
    MOVIES_DIR,
    SERIES_DIR,
    OTROS_DIR,
    # Agrega más rutas según sea necesario
]

TRANSMISSION_HOST = "127.0.0.1"
TRANSMISSION_PORT = 9091
TRANSMISSION_USER = "admin"  # Dejar vacío si no hay autenticación
TRANSMISSION_PASS = "admin"

# Configuración del cliente de Transmission
transmission_client = Client(
    host=TRANSMISSION_HOST,  # Cambia a la IP de tu servidor
    port=TRANSMISSION_PORT,
    username=TRANSMISSION_USER,  # Reemplaza con tu usuario
    password=TRANSMISSION_PASS  # Reemplaza con tu contraseña
)

current_dir = os.path.dirname(os.path.realpath(__file__))
data_file = os.path.join(current_dir, 'jackett_bot.db')

def inicializar_base_de_datos():
    # Conectar o crear la base de datos
    conexion = sqlite3.connect(data_file)
    cursor = conexion.cursor()

    # Eliminar la tabla torrent_cache si existe
    cursor.execute("DROP TABLE IF EXISTS torrent_cache")

    # Crear la tabla auth_users si no existe
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS auth_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_user TEXT NOT NULL,
        telegram_chat_id INTEGER NOT NULL UNIQUE
    )
    """)

    # Crear la tabla torrent_cache si no existe
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS torrent_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            link TEXT NOT NULL
        )
    """)

    # Guardar cambios y cerrar la conexión
    conexion.commit()
    conexion.close()

def usuario_autorizado(telegram_chat_id):
    conexion = sqlite3.connect(data_file)
    cursor = conexion.cursor()

    # Verificar si el usuario está en la tabla auth_users
    cursor.execute("SELECT 1 FROM auth_users WHERE telegram_chat_id = ?", (telegram_chat_id,))
    autorizado = cursor.fetchone() is not None

    conexion.close()
    return autorizado

def agregar_usuario_autorizado(telegram_chat_id):
    conexion = sqlite3.connect(data_file)
    cursor = conexion.cursor()
    try:
        cursor.execute(
            "INSERT INTO auth_users (telegram_user, telegram_chat_id) VALUES (?, ?)",
            ("@placeholder", telegram_chat_id),
        )
        conexion.commit()
        return f"✅ Usuario con ID {telegram_chat_id} agregado correctamente."
    except sqlite3.IntegrityError:
        return f"⚠️ El usuario con ID {telegram_chat_id} ya está autorizado."
    except Exception as e:
        return f"❌ Error al agregar el usuario: {e}"
    finally:
        conexion.close()

def obtener_teclado_usuarios_autorizados():
    conexion = sqlite3.connect(data_file)
    cursor = conexion.cursor()

    # Obtener todos los usuarios autorizados
    cursor.execute("SELECT telegram_user, telegram_chat_id FROM auth_users")
    authorized_users = cursor.fetchall()
    conexion.close()

    # Crear botones para cada usuario autorizado
    keyboard = [
        [InlineKeyboardButton(f"{user[0] or 'Sin alias'}", callback_data=f"unauth:{user[1]}")]
        for user in authorized_users
    ]
    return InlineKeyboardMarkup(keyboard)

def eliminar_usuario_autorizado(telegram_chat_id):
    conexion = sqlite3.connect(data_file)
    cursor = conexion.cursor()
    try:
        cursor.execute(
            "DELETE FROM auth_users WHERE telegram_chat_id = ?",
            (telegram_chat_id,)
        )
        if cursor.rowcount > 0:
            conexion.commit()
            return f"✅ Usuario con ID {telegram_chat_id} eliminado correctamente."
        else:
            return f"⚠️ No se encontró ningún usuario con ID {telegram_chat_id}."
    except Exception as e:
        return f"❌ Error al eliminar el usuario: {e}"
    finally:
        conexion.close()

def requiere_autorizacion(func):
    async def wrapper(update, context, *args, **kwargs):
        usuario = update.effective_user
        telegram_chat_id = usuario.id
        alias_actual = f"@{usuario.username}" if usuario.username else None

        # Verificar si el usuario tiene un alias configurado
        if not alias_actual:
            await update.message.reply_text(
                "❌ Necesitas configurar un alias en Telegram para usar este bot. "
                "Por favor, ve a la configuración de Telegram y establece un nombre de usuario."
            )
            return

        # Permitir siempre al administrador
        if telegram_chat_id == ADMIN_ID:
            return await func(update, context, *args, **kwargs)

        # Verificar si el usuario está en la base de datos
        if not usuario_autorizado(telegram_chat_id):
            await update.message.reply_text(
                f"❌ No estás autorizado para usar este bot. Contacta al administrador con tu ID: {telegram_chat_id}."
            )
            return

        # Si está autorizado, verificar y actualizar el alias si es necesario
        conexion = sqlite3.connect(data_file)
        cursor = conexion.cursor()

        cursor.execute("SELECT telegram_user FROM auth_users WHERE telegram_chat_id = ?", (telegram_chat_id,))
        resultado = cursor.fetchone()

        if resultado:
            alias_guardado = resultado[0]
            if alias_guardado != alias_actual:
                cursor.execute(
                    "UPDATE auth_users SET telegram_user = ? WHERE telegram_chat_id = ?",
                    (alias_actual, telegram_chat_id),
                )
                conexion.commit()
                print(f"Alias actualizado para el usuario {telegram_chat_id}: {alias_guardado} -> {alias_actual}")
        conexion.close()

        # Ejecutar la función decorada
        return await func(update, context, *args, **kwargs)

    return wrapper

# Función para buscar torrents en Jackett y filtrar por categoría
def buscar_torrents(query, user_id, incluir=None, excluir=None):
    params = {"apikey": JACKETT_API_KEY, "Query": query}
    incluir = incluir or []  # Si es None, inicializar como lista vacía
    excluir = excluir or []  # Si es None, inicializar como lista vacía

    try:
        response = requests.get(JACKETT_API_URL, params=params)
        if response.status_code == 200:
            data = response.json()
            resultados_filtrados = []

            for resultado in data.get("Results", []):
                # Extraer el enlace del torrent
                link = resultado.get("Link", None)

                if link:
                    # Obtener o insertar el link en la base de datos
                    resultado["ID"] = obtener_o_insertar_link(link)

                # Lista de términos para identificar películas y series
                terminos_peliculas = ["movies", "películas"]
                terminos_series = ["tv", "series"]

                # Obtener CategoryDesc y asegurarse de que es un string
                category_desc = resultado.get("CategoryDesc", "").lower()  # Convertir a minúsculas para evitar errores

                tipo = None

                # Verificar si alguna palabra clave está contenida en CategoryDesc
                if any(term in category_desc for term in terminos_peliculas):
                    tipo = "Películas"
                elif any(term in category_desc for term in terminos_series):
                    tipo = "Series"
                # Aplicar filtros de inclusión y exclusión
                titulo = resultado.get("Title", "").lower()

                # Verificar inclusión (si incluir no está vacío)
                incluir_match = any(re.search(rf'\b{re.escape(term)}\b', titulo, flags=re.IGNORECASE) for term in
                                    incluir) if incluir else True

                # Verificar exclusión (siempre que excluir no esté vacío)
                termino_excluido = next(
                    (term for term in excluir if re.search(rf'\b{re.escape(term)}\b', titulo, flags=re.IGNORECASE)),
                    None)
                excluir_match = termino_excluido is not None

                # Filtrar según inclusiones y exclusiones
                if tipo and (incluir_match and not excluir_match):
                    resultado["Tipo"] = tipo  # Añadir el tipo al resultado
                    resultados_filtrados.append(resultado)

            return resultados_filtrados
        else:
            print(f"Error en la API de Jackett: {response.status_code} {response.text}")
            return []
    except Exception as e:
        print(f"Error al realizar la búsqueda en Jackett: {e}")
        return []

# Escapa caracteres para MarkdownV2
def escape_markdown_v2(text):
    if text is None:
        return ''
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

# Función para extraer la ID desde el campo Details
def extraer_id(details_url):
    if details_url:
        match = re.search(r"/(\d+)$", details_url)
        if match:
            return match.group(1)
    return "N/A"

def obtener_o_insertar_link(link):
    """
    Verifica si el link existe en la tabla torrent_cache.
    Si existe, devuelve la ID asociada.
    Si no existe, lo inserta y devuelve la nueva ID.
    """
    conn = sqlite3.connect(data_file)
    cursor = conn.cursor()

    # Verificar si el link ya está en la base de datos
    cursor.execute("SELECT id FROM torrent_cache WHERE link = ?", (link,))
    row = cursor.fetchone()

    if row:
        torrent_id = row[0]  # Si existe, obtener el ID
    else:
        # Insertar nuevo link
        cursor.execute("INSERT INTO torrent_cache (link) VALUES (?)", (link,))
        torrent_id = cursor.lastrowid  # Obtener el ID recién insertado
        conn.commit()

    conn.close()
    return torrent_id

def descargar_torrent(id_torrent):
    """
    Busca el link del torrent en la base de datos usando el ID y lo descarga.
    """
    conn = sqlite3.connect(data_file)
    cursor = conn.cursor()

    # Buscar el link en la base de datos
    cursor.execute("SELECT link FROM torrent_cache WHERE id = ?", (id_torrent,))
    row = cursor.fetchone()
    conn.close()

    if row:
        url_torrent = row[0]  # Extraer la URL del torrent

        try:
            response = requests.get(url_torrent)
            if response.status_code == 200:
                return response.content  # Devuelve el contenido del archivo torrent
            else:
                print(f"Error al descargar el torrent: {response.status_code} {response.text}")
                return None
        except Exception as e:
            print(f"Error al descargar el torrent: {e}")
            return None
    else:
        print("Error: No se encontró el ID en la base de datos.")
        return None

# Función para enviar el torrent a Transmission
def enviar_a_transmission(torrent_data):
    intentos_maximos = 3  # Aumentar intentos
    espera_entre_intentos = 5  # Mayor tiempo de espera

    for intento in range(1, intentos_maximos + 1):
        try:
            # Intentar una operación simple primero (para "despertar" la conexión)
            transmission_client.get_session()  # Verifica si la conexión está activa

            # Agregar el torrent
            transmission_client.add_torrent(torrent_data)
            print("Torrent enviado correctamente a Transmission.")
            return True

        except TransmissionError as e:
            print(f"Error de Transmission (Intento {intento}): {e}")
            if intento < intentos_maximos:
                print(f"Reintentando en {espera_entre_intentos} segundos...")
                time.sleep(espera_entre_intentos)
            else:
                print("Error persistente. Verifica el servidor de Transmission.")
                return False

        except ConnectionError as e:
            print(f"Error de conexión (Intento {intento}): {e}")
            time.sleep(espera_entre_intentos)  # Esperar antes de reintentar

        except Exception as e:
            print(f"Error inesperado (Intento {intento}): {e}")
            return False


async def monitorear_descarga(update: Update, torrent_id, context: ContextTypes.DEFAULT_TYPE):
    """Monitorea el progreso de la descarga y actualiza el mensaje en Telegram."""
    descargas = context.bot_data.get("descargas", {})
    if torrent_id not in descargas:
        print(f"Torrent {torrent_id} no encontrado")
        return

    user_data = descargas[torrent_id]
    user_id = user_data["user_id"]
    mensaje_id = user_data["message_id"]
    mensaje_base = user_data["mensaje_base"]

    reintentos = 0
    max_reintentos = 3

    try:
        while reintentos < max_reintentos:
            try:
                # Obtener estado del torrent de forma asíncrona
                torrent = await asyncio.get_event_loop().run_in_executor(
                    None,
                    transmission_client.get_torrent,
                    torrent_id
                )

                progreso = int(torrent.progress)

                # Actualización periódica del progreso
                if progreso != user_data.get("ultimo_progreso", -1):
                    await actualizar_mensaje_progreso(context, user_id, mensaje_id, mensaje_base, progreso)
                    user_data["ultimo_progreso"] = progreso
                    reintentos = 0  # Resetear reintentos

                # Manejo de finalización
                if progreso >= 100:
                    await manejar_descarga_completa(update, context, torrent, user_data)
                    break  # Salir del bucle principal

                await asyncio.sleep(5)

            except Exception as e:
                print(f"Error en iteración: {str(e)}")
                reintentos += 1
                await asyncio.sleep(10)

    finally:
        # Limpieza final garantizada
        if torrent_id in context.bot_data.get("descargas", {}):
            del context.bot_data["descargas"][torrent_id]


async def actualizar_mensaje_progreso(context, user_id, mensaje_id, mensaje_base, progreso):
    """Actualiza el mensaje de progreso con manejo de errores."""
    try:
        await context.bot.edit_message_text(
            chat_id=user_id,
            message_id=mensaje_id,
            text=f"{mensaje_base}\nDescargando... {progreso}% completado."
        )
    except Exception as e:
        if "Message is not modified" not in str(e):
            print(f"Error actualizando progreso: {str(e)}")
            raise

async def manejar_descarga_completa(update, context, torrent, user_data):
    """Maneja todas las acciones posteriores a la descarga completa."""
    try:
        # Notificar administrador
        size_gb = convertir_bytes_a_gb(torrent.total_size)
        usuario = update.effective_user
        alias = f"@{usuario.username}" if usuario.username else usuario.full_name
        mensaje_admin = (
            f"✅Descarga completada:\n"
            f"Título: {torrent.name}\n"
            f"Tamaño: {size_gb} GB\n"
            f"Solicitada por: {alias}"
        )
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=mensaje_admin
        )

        # Notificar usuario
        await context.bot.edit_message_text(
            chat_id=user_data["user_id"],
            message_id=user_data["message_id"],
            text=f"{user_data['mensaje_base']}\n\n✅¡Descarga completada!"
        )
    except Exception as e:
        print(f"Error en notificaciones finales: {str(e)}")
        # Puedes agregar aquí un reintento de notificación si es crítico

# Función que se invoca al usar el comando /descargar
async def descargar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    id_torrent = update.message.text.replace("/descargar", "").strip()
    resultados = context.user_data.get("resultados", [])

    if not resultados:
        await update.message.reply_text(
            "No hay resultados disponibles. Realiza una búsqueda primero para descargar un torrent."
        )
        return

    # Buscar el torrent correspondiente en la lista de resultados
    torrent_info = None
    for r in resultados:
        print(f"Analizando torrent: {r.get('Title', 'Sin título')}")
        print(f"ID en resultado: {r.get('ID')}, ID buscado: {id_torrent}")
        if str(r.get("ID")) == str(id_torrent):
            torrent_info = r
            print("✅ Torrent encontrado:", torrent_info)
            break

    if not torrent_info:
        await update.message.reply_text(f"No se encontró un torrent con la ID {id_torrent}.")
        return

    # Extraer detalles del torrent
    titulo = torrent_info.get("Title", "Sin título")
    size_bytes = torrent_info.get("Size", 0)
    size_gb = convertir_bytes_a_gb(size_bytes)
    tipo = torrent_info.get("Tipo", "Desconocido")

    torrent_data = descargar_torrent(id_torrent)
    if torrent_data:
        # Para soportar múltiples descargas simultáneas, se almacena la info en un diccionario
        if "torrent_descargas" not in context.user_data:
            context.user_data["torrent_descargas"] = {}
        context.user_data["torrent_descargas"][id_torrent] = {
            "id_torrent": id_torrent,
            "torrent_info": torrent_info,
            "torrent_data": torrent_data,
            "titulo": titulo,
            "size_gb": size_gb,
            "tipo": tipo,
        }

        # Construir el mensaje de confirmación, incluyendo el nombre del torrent en el título
        texto_confirmacion = f"Torrent: *{titulo}*\n¿Deseas descargar este torrent en la carpeta otros?"
        keyboard = [
            [
                # Se incluye la ID en la callback_data para distinguir entre descargas simultáneas.
                InlineKeyboardButton("SI", callback_data=f"descarga_otros_si_{id_torrent}"),
                InlineKeyboardButton("NO", callback_data=f"descarga_otros_no_{id_torrent}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        # Se envía el mensaje de confirmación
        await update.message.reply_text(
            texto_confirmacion,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(f"No se pudo descargar el torrent {id_torrent}")

# Callback query handler para procesar la respuesta de la confirmación
async def confirmar_descarga(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Extraer la ID del torrent desde la callback_data.
    # Se espera un formato "descarga_otros_si_{id}" o "descarga_otros_no_{id}"
    torrent_id = query.data.rsplit("_", 1)[-1]

    # Recuperar la información asociada a este torrent
    torrent_data_info = context.user_data.get("torrent_descargas", {}).get(torrent_id)
    if not torrent_data_info:
        await query.edit_message_text("Información del torrent no encontrada, por favor intenta nuevamente.")
        return

    # Extraer datos necesarios
    id_torrent   = torrent_data_info["id_torrent"]
    torrent_info = torrent_data_info["torrent_info"]
    torrent_data = torrent_data_info["torrent_data"]
    titulo       = torrent_data_info["titulo"]
    size_gb      = torrent_data_info["size_gb"]
    tipo         = torrent_data_info["tipo"]

    # Según la respuesta, determinar el directorio de descarga
    if query.data.startswith("descarga_otros_si_"):
        # Si el usuario elige "SI", se cambia el tipo a "otros" y se usa OTROS_DIR
        torrent_info["Tipo"] = "otros"
        download_dir = OTROS_DIR
    else:
        # Si el usuario elige "NO", se sigue la lógica original
        download_dir = MOVIES_DIR if tipo == "Películas" else SERIES_DIR

    try:
        # Función para verificar la conexión y reconectar si es necesario
        def verificar_conexion():
            global transmission_client
            try:
                transmission_client.session_stats()
            except (transmission_rpc.TransmissionError, ConnectionError):
                transmission_client = transmission_rpc.Client(
                    host=TRANSMISSION_HOST,
                    port=TRANSMISSION_PORT,
                    username=TRANSMISSION_USER,
                    password=TRANSMISSION_PASS
                )

        verificar_conexion()

        # Agregar el torrent a Transmission
        torrent = transmission_client.add_torrent(torrent_data, download_dir=download_dir)
        transmission_id = torrent.id
        usuario = update.effective_user
        alias = f"@{usuario.username}" if usuario.username else usuario.full_name

        mensaje_base = (
            f"El torrent '{titulo}' ({torrent_info.get('Tipo', tipo)}) se ha enviado correctamente a Transmission "
            f"en la ruta {download_dir}."
        )

        mensaje_admin = (
            f"Nueva descarga iniciada:\n"
            f"Título: {titulo}\n"
            f"Tipo: {torrent_info.get('Tipo', tipo)}\n"
            f"Tamaño: {size_gb} GB\n"
            f"Enviado por: {alias}"
        )
        await context.bot.send_message(chat_id=ADMIN_ID, text=mensaje_admin)

        # Se actualiza el mensaje original de confirmación para indicar que se inició la descarga
        mensaje = await query.edit_message_text(f"{mensaje_base}\nDescargando... 0% completado.")

        if "descargas" not in context.bot_data:
            context.bot_data["descargas"] = {}
        context.bot_data["descargas"][transmission_id] = {
            "user_id": update.effective_user.id,
            "message_id": mensaje.message_id,
            "mensaje_base": mensaje_base,
        }

        # Inicia la tarea para monitorear el progreso de la descarga
        asyncio.create_task(monitorear_descarga(update, transmission_id, context))

        # Una vez procesada la descarga, se puede eliminar la info del torrent de la lista de pendientes
        context.user_data["torrent_descargas"].pop(torrent_id, None)

    except transmission_rpc.TransmissionError as e:
        await query.edit_message_text(f"Error de Transmission: {e}")
    except ConnectionRefusedError:
        await query.edit_message_text("Error de conexión: El daemon de Transmission no responde.")
    except Exception as e:
        await query.edit_message_text(f"Error inesperado: {str(e)}")

async def listar_origenes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra botones inline para seleccionar la carpeta de origen."""
    botones = [
        [InlineKeyboardButton(os.path.basename(directorio), callback_data=f"origen_{i}")]
        for i, directorio in enumerate(DIRECTORIOS_USB)
    ]
    teclado = InlineKeyboardMarkup(botones)
    await update.message.reply_text('Selecciona una carpeta de origen:', reply_markup=teclado)

async def listar_archivos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Muestra la lista de archivos de la carpeta seleccionada, paginados.
    Se espera que el callback data tenga el formato:
        - "origen_{origen_index}" o "origen_{origen_index}_pagina_{pagina}"
    """
    query = update.callback_query
    await query.answer()
    data = query.data.split('_')
    origen_index = int(data[1])
    # Si se envía la información de página, se obtiene; si no, se asume 0
    pagina_ = int(data[3]) if len(data) == 4 else 0

    directorio = DIRECTORIOS_USB[origen_index]
    archivos = os.listdir(directorio)
    #archivos = [f for f in archivos if os.path.isfile(os.path.join(directorio, f))]
    archivos = [f for f in archivos if
                os.path.isfile(os.path.join(directorio, f)) or os.path.isdir(os.path.join(directorio, f))]

    # Guardar el índice de origen en user_data para usarlo luego en el comando /enviar{idx}
    context.user_data['origen_index'] = origen_index

    inicio = pagina_ * RESULTADOS_POR_PAGINA
    fin = inicio + RESULTADOS_POR_PAGINA
    archivos_pagina = archivos[inicio:fin]

    respuesta = f"Resultados para '{directorio}':\n"
    for idx, archivo in enumerate(archivos_pagina):
        global_idx = inicio + idx  # índice global en la lista completa
        respuesta += (
            f"\n🔹 {archivo}\n"
            f"Usa /enviar{global_idx} para enviar este archivo\n"
        )

    botones = []
    if pagina_ > 0:
        botones.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"origen_{origen_index}_pagina_{pagina_ - 1}"))
    if fin < len(archivos):
        botones.append(InlineKeyboardButton("Siguiente ➡️", callback_data=f"origen_{origen_index}_pagina_{pagina_ + 1}"))

    teclado = InlineKeyboardMarkup([botones] if botones else [])

    await query.edit_message_text(respuesta, reply_markup=teclado)


async def listar_usbs(update: Update, context: ContextTypes.DEFAULT_TYPE, archivo_id: int = None) -> None:
    """
    Muestra los dispositivos USB montados para enviar el elemento (archivo o carpeta) seleccionado.
    Se espera que previamente se haya seleccionado un directorio de origen (por ejemplo, con un comando /listarusb)
    que asigne a context.user_data['origen_index'] el índice correspondiente en DIRECTORIOS_USB.
    """
    # Si se trata de un CallbackQuery (por ejemplo, al seleccionar el dispositivo USB de destino)
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        data = query.data.split('_')
        if len(data) == 3:
            # Formato esperado: "usb_{origen_index}_{archivo_id}"
            try:
                origen_index = int(data[1])
                archivo_id = int(data[2])
            except ValueError:
                await query.edit_message_text("Datos de callback no válidos.")
                return
        elif len(data) == 2:
            # Formato esperado: "usb_{dispositivo}"
            dispositivo = data[1]
            if 'archivo_seleccionado' not in context.user_data:
                await query.edit_message_text("No se ha seleccionado ningún elemento.")
                return
            # Validación: obtenemos el dispositivo de origen usando el directorio configurado
            if 'origen_index' in context.user_data:
                origen_index = context.user_data['origen_index']
                directorio_origen = DIRECTORIOS_USB[origen_index]
                origen_device = os.path.basename(os.path.dirname(directorio_origen))
                if dispositivo == origen_device:
                    await query.edit_message_text("El dispositivo de origen no puede ser seleccionado como destino.")
                    return
            archivo_seleccionado = context.user_data['archivo_seleccionado']
            user = getpass.getuser()  # Se obtiene el nombre de usuario de forma dinámica
            destino_dir = os.path.join(f'/media/{user}', dispositivo)
            if not os.path.exists(destino_dir):
                await query.edit_message_text(f"La ruta {destino_dir} no existe.")
                return
            # Definimos la ruta destino manteniendo el nombre del elemento
            destino_final = os.path.join(destino_dir, os.path.basename(archivo_seleccionado))
            try:
                if os.path.isdir(archivo_seleccionado):
                    shutil.copytree(archivo_seleccionado, destino_final)
                else:
                    shutil.copy(archivo_seleccionado, destino_final)
                await query.edit_message_text(f"Elemento enviado a {destino_final}.")
            except Exception as e:
                await query.edit_message_text(f"Error al enviar el elemento: {str(e)}")
            return
        else:
            await query.edit_message_text("Formato de callback no reconocido.")
            return
    else:
        # Se invoca desde un mensaje (comando /enviar{idx})
        if archivo_id is None:
            await update.message.reply_text("Debes especificar un ID de elemento.")
            return
        if 'origen_index' not in context.user_data:
            await update.message.reply_text("Primero debes seleccionar una carpeta con /listarusb.")
            return
        origen_index = context.user_data['origen_index']

    # Obtenemos el directorio de origen
    directorio = DIRECTORIOS_USB[origen_index]
    try:
        elementos = sorted(os.listdir(directorio))
    except Exception as e:
        await update.message.reply_text(f"Error al listar el directorio: {str(e)}")
        return

    # Comprobamos que el índice esté dentro del rango de elementos disponibles
    if 0 <= archivo_id < len(elementos):
        elemento_seleccionado = elementos[archivo_id]
        context.user_data['archivo_seleccionado'] = os.path.join(directorio, elemento_seleccionado)
        context.user_data['origen_index'] = origen_index

        user = getpass.getuser()  # Se obtiene el nombre de usuario actual
        media_path = f'/media/{user}/'
        if os.path.exists(media_path):
            # Extraemos el dispositivo de origen a partir del directorio (por ejemplo, "discohdd")
            origen_device = os.path.basename(os.path.dirname(directorio))
            # Listamos y excluimos el dispositivo de origen
            dispositivos = sorted([d for d in os.listdir(media_path) if d != origen_device])
            if dispositivos:
                botones_usbs = [
                    [InlineKeyboardButton(dispositivo, callback_data=f"usb_{dispositivo}")]
                    for dispositivo in dispositivos
                ]
                teclado = InlineKeyboardMarkup(botones_usbs)
                await update.message.reply_text('Selecciona el dispositivo USB de destino:', reply_markup=teclado)
            else:
                await update.message.reply_text("No hay dispositivos USB montados disponibles (se excluyó el dispositivo de origen).")
        else:
            await update.message.reply_text(f"La ruta {media_path} no existe.")
    else:
        await update.message.reply_text(
            f"ID de elemento no válido.\n"
            f"Recibido: {archivo_id}\n"
            f"Elementos disponibles: {len(elementos)}\n"
            f"Elementos: {', '.join(elementos)}"
        )

async def enviar_comando(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handler para el comando /enviar{idx}. Se extrae el número y se llama a listar_usbs.
    Ejemplo: "/enviar7" enviará el elemento (archivo o carpeta) con índice 7.
    """
    text = update.message.text
    if text.startswith('/enviar'):
        idx_str = text[len('/enviar'):]
        try:
            archivo_id = int(idx_str)
        except ValueError:
            await update.message.reply_text("Comando mal formateado. Usa /enviar{numero}.")
            return
    else:
        await update.message.reply_text("Comando desconocido.")
        return

    await listar_usbs(update, context, archivo_id=archivo_id)


async def enviar_comando(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handler para el comando /enviar{idx}. Extrae el número y llama a listar_usbs.
    Por ejemplo, si el usuario envía "/enviar7" se interpretará que debe enviar el archivo con índice 7.
    """
    text = update.message.text
    if text.startswith('/enviar'):
        idx_str = text[len('/enviar'):]
        try:
            archivo_id = int(idx_str)
        except ValueError:
            await update.message.reply_text("Comando mal formateado. Usa /enviar{numero}.")
            return
    else:
        await update.message.reply_text("Comando desconocido.")
        return

    # Llama a la función que muestra los dispositivos USB, pasando el archivo_id obtenido
    await listar_usbs(update, context, archivo_id=archivo_id)


# Función auxiliar para copiar el archivo en bloques y actualizar el progreso cada 10 segundos
async def copiar_archivo_con_progreso(src: str, dst: str, context: ContextTypes.DEFAULT_TYPE,
                                      chat_id: int, message_id: int, mensaje_base: str):
    total_size = os.path.getsize(src)
    copied = 0
    chunk_size = 1024 * 1024  # 1 MB por bloque
    update_interval = 10  # Actualización cada 10 segundos
    last_update_time = time.monotonic()

    async with aiofiles.open(src, 'rb') as fsrc:
        async with aiofiles.open(dst, 'wb') as fdst:
            while True:
                chunk = await fsrc.read(chunk_size)
                if not chunk:
                    break
                await fdst.write(chunk)
                copied += len(chunk)
                progreso = int((copied / total_size) * 100)
                current_time = time.monotonic()
                # Actualizamos solo cada "update_interval" segundos
                if current_time - last_update_time >= update_interval:
                    last_update_time = current_time
                    try:
                        await context.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=message_id,
                            text=f"{mensaje_base}\nCopiando... {progreso}% completado."
                        )
                    except Exception as e:
                        if "Message is not modified" not in str(e):
                            print(f"Error actualizando progreso: {e}")


async def copiar_directorio_con_progreso(src: str, dst: str, context: ContextTypes.DEFAULT_TYPE,
                                         chat_id: int, message_id: int, mensaje_base: str):
    """
    Copia recursivamente el directorio src a dst mostrando el progreso basado en la suma
    de los tamaños de todos los archivos.
    """
    # 1. Calcular el tamaño total de todos los archivos en el directorio
    total_size = 0
    for root, dirs, files in os.walk(src):
        for file in files:
            archivo = os.path.join(root, file)
            try:
                total_size += os.path.getsize(archivo)
            except Exception as e:
                print(f"Error obteniendo tamaño de {archivo}: {e}")

    if total_size == 0:
        total_size = 1  # Evitar división por cero

    # Variable para llevar la cuenta global de bytes copiados
    bytes_copiados = 0
    update_interval = 10  # segundos
    last_update_time = time.monotonic()

    # 2. Recorrer el árbol del directorio y copiar archivo por archivo
    for root, dirs, files in os.walk(src):
        # Determinar la ruta relativa y crear la carpeta destino correspondiente
        rel_path = os.path.relpath(root, src)
        destino_dir = os.path.join(dst, rel_path)
        os.makedirs(destino_dir, exist_ok=True)

        for file in files:
            src_file = os.path.join(root, file)
            dst_file = os.path.join(destino_dir, file)

            # Copiar el archivo en bloques usando aiofiles
            try:
                async with aiofiles.open(src_file, 'rb') as fsrc, aiofiles.open(dst_file, 'wb') as fdst:
                    while True:
                        chunk = await fsrc.read(1024 * 1024)  # 1 MB por bloque
                        if not chunk:
                            break
                        await fdst.write(chunk)
                        bytes_copiados += len(chunk)

                        # Actualizar el progreso cada update_interval segundos
                        current_time = time.monotonic()
                        if current_time - last_update_time >= update_interval:
                            last_update_time = current_time
                            progreso = int((bytes_copiados / total_size) * 100)
                            try:
                                await context.bot.edit_message_text(
                                    chat_id=chat_id,
                                    message_id=message_id,
                                    text=f"{mensaje_base}\nCopiando... {progreso}% completado."
                                )
                            except Exception as e:
                                if "Message is not modified" not in str(e):
                                    print(f"Error actualizando progreso: {e}")
            except Exception as e:
                print(f"Error copiando {src_file}: {e}")

    # Actualización final
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=f"{mensaje_base}\n✅ Directorio copiado exitosamente."
        )
    except Exception as e:
        if "Message is not modified" not in str(e):
            print(f"Error finalizando mensaje: {e}")

async def copiar_archivo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Copia el archivo o carpeta seleccionado al dispositivo USB elegido, mostrando el progreso."""
    query = update.callback_query
    await query.answer()
    data = query.data.split('_')
    # Permite nombres con guiones bajos en el dispositivo
    dispositivo = '_'.join(data[1:])

    archivo_seleccionado = context.user_data.get('archivo_seleccionado')
    origen_index = context.user_data.get('origen_index')

    if archivo_seleccionado and origen_index is not None:
        user = os.getenv('USER')
        media_path = os.path.join(f'/media/{user}', dispositivo)

        if os.path.exists(media_path):
            # Determinar la carpeta de destino según la carpeta de origen (comparando en minúsculas)
            origen_path = DIRECTORIOS_USB[origen_index].lower()
            if 'series' in origen_path:
                carpeta_destino = os.path.join(media_path, 'series')
            elif 'peliculas' in origen_path:
                carpeta_destino = os.path.join(media_path, 'peliculas')
            elif 'otros' in origen_path:
                carpeta_destino = os.path.join(media_path, 'otros')
            else:
                await query.edit_message_text("No se pudo determinar la carpeta de destino en el USB.")
                return

            os.makedirs(carpeta_destino, exist_ok=True)
            nombre_elemento = os.path.basename(archivo_seleccionado)
            destino_elemento = os.path.join(carpeta_destino, nombre_elemento)
            mensaje_base = f"Copiando {nombre_elemento} a {carpeta_destino}."

            if os.path.isdir(archivo_seleccionado):
                # Si es un directorio, usamos la función para copiar directorios con progreso
                await query.edit_message_text(f"{mensaje_base}\nCopiando directorio...")
                try:
                    await copiar_directorio_con_progreso(
                        archivo_seleccionado,
                        destino_elemento,
                        context,
                        query.message.chat_id,
                        query.message.message_id,
                        mensaje_base
                    )
                    await query.edit_message_text(f"{mensaje_base}\n✅ Directorio copiado exitosamente a {carpeta_destino}.")
                except Exception as e:
                    await query.edit_message_text(f"{mensaje_base}\n❌ Error al copiar directorio: {e}")
            else:
                # Si es un archivo, usamos la función existente que muestra el progreso
                await query.edit_message_text(f"{mensaje_base}\nCopiando... 0% completado.")
                try:
                    await copiar_archivo_con_progreso(
                        archivo_seleccionado,
                        destino_elemento,
                        context,
                        query.message.chat_id,
                        query.message.message_id,
                        mensaje_base
                    )
                    await query.edit_message_text(f"{mensaje_base}\n✅ Archivo copiado exitosamente a {carpeta_destino}.")
                except Exception as e:
                    await query.edit_message_text(f"{mensaje_base}\n❌ Error al copiar el archivo: {e}")
        else:
            await query.edit_message_text(f"El dispositivo USB {dispositivo} no está montado.")
    else:
        await query.edit_message_text("No se ha seleccionado un archivo o carpeta de origen.")


def actualizar_exclusiones(selected_quality):
    global excluir_terminos, calidad_maxima_actual_por_defecto
    calidad_2k = ["2k", "1440p", "qhd", "wqhd", "quad hd"]
    calidad_4k = ["4k", "2160p", "uhd", "ultra hd", "4k uhd", "4k ultra hd", "4kuhdrip", "uhd 4k"]

    calidad_maxima_actual_por_defecto = selected_quality

    if selected_quality == "1080p":
        excluir_terminos = list(set(excluir_terminos + calidad_2k + calidad_4k))
    elif selected_quality == "2K":
        excluir_terminos = list(set(excluir_terminos) - set(calidad_2k))
        excluir_terminos = list(set(excluir_terminos + calidad_4k))
    elif selected_quality == "4K":
        excluir_terminos = list(set(excluir_terminos) - set(calidad_2k) - set(calidad_4k))

# Manejo del comando /start
@requiere_autorizacion
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = update.effective_user

    # Crear teclado
    botones = [["Buscar Torrent"],["Copiar archivos"]]
    if usuario.id == ADMIN_ID:
        botones.append(["Añadir usuario autorizado"])
        botones.append(["Eliminar usuario autorizado"])

    teclado = ReplyKeyboardMarkup(botones, resize_keyboard=True)

    # Responder con el teclado
    await update.message.reply_text(
        f"¡Hola, @{usuario.username or 'usuario autorizado'}! Escribiendo o presionando en /start aparecerá"
        f" un menú con las opciones a elegir",
        reply_markup=teclado,
    )

def resetear_estado(context):
    keys_to_remove = [
        "esperando_id_usuario_agregar",
        "esperando_id_usuario_eliminar",
        "buscando_torrent",
        "copiar_archivos",
    ]
    for key in keys_to_remove:
        context.user_data.pop(key, None)

# Manejar la opción "Añadir usuario autorizado"
async def agregar_usuario_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = update.effective_user

    if usuario.id != ADMIN_ID:
        await update.message.reply_text("❌ Solo el administrador puede usar esta función.")
        return

    # Resetear estados previos
    resetear_estado(context)

    # Activar el nuevo estado
    context.user_data["esperando_id_usuario_agregar"] = True
    await update.message.reply_text(
        "Por favor, envíame el ID de Telegram del usuario que deseas autorizar.",
        parse_mode="Markdown",
    )

@requiere_autorizacion
async def copiar_archivos_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Resetear estados previos
    resetear_estado(context)

    # Activar el nuevo estado
    context.user_data["copiar_archivos"] = True

    botones = [
        [InlineKeyboardButton(os.path.basename(directorio), callback_data=f"origen_{i}")]
        for i, directorio in enumerate(DIRECTORIOS_USB)
    ]
    teclado = InlineKeyboardMarkup(botones)
    await update.message.reply_text('Selecciona una carpeta de origen:', reply_markup=teclado)

async def eliminar_usuario_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = update.effective_user

    if usuario.id != ADMIN_ID:
        await update.message.reply_text("❌ Solo el administrador puede usar esta función.")
        return

    # Generar el teclado con los usuarios autorizados
    reply_markup = obtener_teclado_usuarios_autorizados()

    # Enviar el mensaje con el teclado
    await update.message.reply_text(
        "Selecciona un usuario para desautorizar:",
        reply_markup=reply_markup
    )

async def buscar_torrent_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Resetear estados previos
    resetear_estado(context)

    # Activar el nuevo estado
    context.user_data["buscando_torrent"] = True
    await update.message.reply_text(
        "📝 Por favor, envíame el nombre del torrent que quieres buscar."
    )

async def manejar_paginacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Extraer la página del callback_data
    pagina = int(query.data.split("_")[1])
    await mostrar_pagina(update, context, pagina)

async def manejar_desautorizacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Extraer el ID del usuario del callback_data
    data = query.data
    if data.startswith("unauth:"):
        telegram_chat_id = data.split(":")[1]

        # Intentar eliminar al usuario
        resultado = eliminar_usuario_autorizado(telegram_chat_id)

        # Responder al administrador
        await query.edit_message_text(
            f"{resultado}"
        )

@requiere_autorizacion
async def manejar_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 1. Manejar "Añadir usuario autorizado"
    if context.user_data.get("esperando_id_usuario_agregar", False):
        try:
            telegram_chat_id = int(update.message.text.strip())
            resultado = agregar_usuario_autorizado(telegram_chat_id)
            await update.message.reply_text(resultado)
            context.user_data["esperando_id_usuario_agregar"] = False
        except ValueError:
            await update.message.reply_text("⚠️ El ID debe ser un número válido.")
        return

    # 2. Manejar "Buscar Torrent"
    if context.user_data.get("buscando_torrent", False):
        query = update.message.text.strip()
        user_id = update.effective_user.id  # Obtener el ID del usuario

        if not query:
            await update.message.reply_text("⚠️ Por favor, proporciona un término de búsqueda.")
            return

        resultados = buscar_torrents(query, user_id, incluir=incluir_terminos, excluir=excluir_terminos)

        if resultados:
            context.user_data["resultados"] = resultados
            context.user_data["query"] = query
            await mostrar_pagina(update, context, pagina=0)
        else:
            await update.message.reply_text("No se encontraron resultados para tu búsqueda.")
        context.user_data["buscando_torrent"] = False
        return

    # Si no hay flujo activo
    await update.message.reply_text("⚠️ No entiendo tu mensaje. Usa un comando válido.")

# Manejo de mensajes de texto (búsqueda de torrents)
@requiere_autorizacion
async def buscar_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Si estamos esperando datos, ignorar el mensaje
    if context.user_data.get("esperando_id_usuario_agregar", False):
        return

    query = update.message.text.strip()
    if not query:
        await update.message.reply_text("⚠️ Por favor, proporciona un término de búsqueda.")
        return

    # Simulación de búsqueda
    resultados = buscar_torrents(query)

    if resultados:
        context.user_data["resultados"] = resultados
        context.user_data["query"] = query

        # Mostrar resultados
        await update.message.reply_text(
            f"Resultados para '{query}':\n" + "\n".join([r["Title"] for r in resultados])
        )
    else:
        await update.message.reply_text("No se encontraron resultados para tu búsqueda.")

# Función para convertir bytes a gigabytes con dos decimales
def convertir_bytes_a_gb(bytes_tamano):
    return round(bytes_tamano / (1024 ** 3), 2)


async def ajustes_handler(update: Update, context: CallbackContext) -> None:
    keyboard = [[
        InlineKeyboardButton("1080p", callback_data='quality_1080p'),
        InlineKeyboardButton("2K", callback_data='quality_2K'),
        InlineKeyboardButton("4K", callback_data='quality_4K')
    ]]

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(f"Calidad máxima actual: {calidad_maxima_actual_por_defecto}"
                                    f"\n\nSelecciona la calidad máxima:",reply_markup=reply_markup)


async def quality_handler(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()

    # Procesar la selección de calidad
    selected_quality = query.data.split('_')[1]
    actualizar_exclusiones(selected_quality)

    await query.edit_message_text(
        text=f"Has seleccionado {selected_quality} como tope de calidad. Lista de exclusión actualizada.")

# Función para mostrar una página de resultados
async def mostrar_pagina(update, context, pagina):
    resultados = context.user_data.get("resultados", [])
    query = context.user_data.get("query", "")

    # Calcular el rango de resultados para la página actual
    inicio = pagina * RESULTADOS_POR_PAGINA
    fin = inicio + RESULTADOS_POR_PAGINA
    resultados_pagina = resultados[inicio:fin]

    # Construir el mensaje
    respuesta = f"Resultados para '{query}':\n"
    for resultado in resultados_pagina:
        titulo = resultado.get("Title", "Sin título")
        tipo = resultado.get("Tipo", "Desconocido")
        tracker = resultado.get("TrackerId", "Desconocido")
        size_gb = resultado.get("Size", 0) / (1024 ** 3)
        seeders = resultado.get("Seeders", "N/A")
        id_torrent = resultado.get("ID", "N/A")
        respuesta += (
            f"\n🔹 {titulo}\n"
            f"Tipo: {tipo}\n"
            f"Tamaño: {size_gb:.2f} GB\n"
            f"Tracker: {tracker}\n"
            f"Seeders: {seeders}\n"
            f"Usa /descargar{id_torrent} para descargar este torrent.\n"
        )

    # Crear botones de navegación
    total_paginas = (len(resultados) - 1) // RESULTADOS_POR_PAGINA + 1
    botones = []
    if pagina > 0:
        botones.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"pagina_{pagina - 1}"))
    if pagina < total_paginas - 1:
        botones.append(InlineKeyboardButton("Siguiente ➡️", callback_data=f"pagina_{pagina + 1}"))

    teclado = InlineKeyboardMarkup([botones] if botones else [])

    # Editar o enviar el mensaje
    if update.callback_query:
        await update.callback_query.edit_message_text(respuesta, reply_markup=teclado)
    else:
        await update.message.reply_text(respuesta, reply_markup=teclado)


# Manejo de las interacciones con los botones
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Detectar si el botón es para cambiar de página
    if query.data.startswith("pagina_"):
        pagina = int(query.data.split("_")[1])
        await mostrar_pagina(update, context, pagina)

# Manejo del comando de navegación
async def pagina(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pagina = int(update.message.text.replace("/pagina", "").strip())
    await mostrar_pagina(update, context, pagina)

async def post_init(application):
    comandos = [
        BotCommand("start", "Muestra el menú de opciones"),
        BotCommand("ajustes", "Configura el tope de calidad"),
    ]
    await application.bot.set_my_commands(comandos)

# Función principal del bot
async def main():
    inicializar_base_de_datos()
    application = ApplicationBuilder().token(TOKEN_TELEGRAM).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Regex("Añadir usuario autorizado"), agregar_usuario_handler))
    application.add_handler(MessageHandler(filters.Regex("Buscar Torrent"), buscar_torrent_handler))
    application.add_handler(MessageHandler(filters.Regex("Eliminar usuario autorizado"), eliminar_usuario_handler))
    application.add_handler(MessageHandler(filters.Regex("Copiar archivos"), copiar_archivos_handler))
    application.add_handler(CallbackQueryHandler(manejar_desautorizacion, pattern=r"^unauth:"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_texto))
    application.add_handler(CallbackQueryHandler(manejar_paginacion, pattern=r"^pagina_\d+$"))
    application.add_handler(CallbackQueryHandler(listar_archivos, pattern=r'^origen_\d+(_pagina_\d+)?$'))
    application.add_handler(CallbackQueryHandler(listar_usbs, pattern=r'^archivo_\d+_\d+$'))
    application.add_handler(MessageHandler(filters.Regex(r"^/descargar\d+$"), descargar))
    application.add_handler(MessageHandler(filters.Regex(r'^/enviar\d+$'), enviar_comando))
    application.add_handler(CallbackQueryHandler(copiar_archivo, pattern=r'^usb_.+$'))
    application.add_handler(CallbackQueryHandler(confirmar_descarga, pattern="^descarga_otros_"))
    application.add_handler(CommandHandler("ajustes", ajustes_handler))
    application.add_handler(CallbackQueryHandler(quality_handler, pattern=r'quality_.*'))

    await application.initialize()
    await application.run_polling()

if __name__ == "__main__":
    nest_asyncio.apply()
    asyncio.run(main())

