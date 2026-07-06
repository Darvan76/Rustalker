# Rustalker

<p align="center">
  <strong>Discord bot para Rust + BattleMetrics</strong><br>
  Rastrea servidores, vigila jugadores, genera alertas tácticas y crea análisis de actividad desde un solo panel.
</p>

---

## ¿Qué es Rustalker?

Rustalker es un bot de Discord pensado para clanes, administradores y equipos de Rust que quieren centralizar su inteligencia táctica con BattleMetrics.

Con Rustalker puedes:

- Monitorear servidores de BattleMetrics.
- Vigilar jugadores y recibir alertas de conexión y desconexión.
- Registrar clanes y miembros.
- Generar estadísticas de actividad por hora.
- Calcular ventanas óptimas de inactividad para raids.

## Stack

- Python
- discord.py
- aiohttp
- aiosqlite
- matplotlib
- python-dotenv

## Características

- Slash commands modernos.
- Base de datos SQLite creada automáticamente.
- Alertas en tiempo real por canal y rol.
- Selector interactivo cuando hay varios resultados posibles.
- Gráficas de actividad con estilo oscuro.
- Modo limitado si no configuras `BATTLEMETRICS_TOKEN`.

## Requisitos

- Python 3.11 o superior.
- Un bot creado en el Discord Developer Portal.
- Un token de BattleMetrics es muy recomendable.

## Instalación

### 1. Clona el repositorio

```bash
git clone <URL_DE_TU_REPOSITORIO>
cd Rustalker
```

### 2. Crea y activa un entorno virtual

```bash
python -m venv .venv
.venv\Scripts\activate
```

### 3. Instala dependencias

```bash
pip install -r requirements.txt
```

### 4. Configura el archivo `.env`

Copia `.env.example` a `.env` y completa los valores:

```env
DISCORD_BOT_TOKEN=tu_token_de_discord
BATTLEMETRICS_TOKEN=tu_token_de_battlemetrics
DATABASE_PATH=rustalker.db
```

Si no tienes `BATTLEMETRICS_TOKEN`, el bot arrancará con funcionalidades limitadas, pero varios comandos no podrán consultar datos.

### 5. Crea el bot en Discord

1. Entra al [Discord Developer Portal](https://discord.com/developers/applications).
2. Crea una nueva aplicación.
3. Ve a la sección **Bot** y crea el bot.
4. Copia el token y colócalo en `DISCORD_BOT_TOKEN`.

### 6. Invita el bot a tu servidor

Usa un enlace OAuth2 con estos permisos mínimos:

- `applications.commands`
- `Send Messages`
- `Embed Links`
- `Attach Files`
- `Read Message History`

Si quieres que publique alertas en canales concretos, asegúrate de darle acceso a esos canales.

### 7. Ejecuta el bot

```bash
python main.py
```

La base de datos SQLite se creará automáticamente en el archivo definido por `DATABASE_PATH`.

## Tutorial de uso

### Configuración inicial

1. Ejecuta `/setup_alerts` para definir el canal donde llegarán las alertas.
2. Si quieres, añade un rol para menciones en alertas críticas.
3. Ejecuta `/setup_rules` para ajustar los umbrales tácticos.

Ejemplo:

```text
/setup_alerts channel:#alertas role_mention:@Raid Team
/setup_rules spike_window:15 spike_threshold:3 queue_threshold:5
```

### Monitoreo de servidores

1. Usa `/server_track` con la URL o el ID del servidor de BattleMetrics.
2. El bot empezará a revisar mapa, cola, población y jugadores incluidos.
3. Para dejar de rastrearlo usa `/server_untrack`.

Ejemplo:

```text
/server_track target: https://www.battlemetrics.com/servers/rust/123456
/server_untrack server_id:123456
```

### Watchlist de jugadores

1. Añade jugadores con `/watch`.
2. Puedes sumar notas y un canal personalizado para ese jugador.
3. Revisa el estado actual con `/watchlist`.
4. Elimina un jugador con `/unwatch`.

Ejemplo:

```text
/watch target:PlayerName notes:"Líder del clan enemigo" custom_channel:#seguimiento
```

### Clanes

1. Crea un clan con `/clan create`.
2. Lista los clanes con `/clan list`.
3. Agrega o quita miembros con `/clan add_member` y `/clan remove_member`.

### Análisis

- `/check_player` muestra el perfil completo del jugador.
- `/stats` genera una gráfica de actividad por hora.
- `/raid_predictor` calcula la mejor ventana de inactividad.

## Comandos principales

### BattleMetrics / Monitoreo

| Comando | Uso |
| --- | --- |
| `/tuto` | Muestra la guía rápida dentro de Discord |
| `/setup_alerts` | Configura el canal de alertas |
| `/setup_rules` | Ajusta umbrales tácticos |
| `/server_track` | Empieza a monitorear un servidor |
| `/server_untrack` | Detiene el monitoreo |
| `/watch` | Añade un jugador a la watchlist |
| `/unwatch` | Elimina un jugador vigilado |
| `/watchlist` | Muestra jugadores vigilados |
| `/check_player` | Consulta un jugador |
| `/stats` | Genera estadísticas de actividad |
| `/raid_predictor` | Calcula la ventana óptima de raid |
| `/clan create` | Crea un clan |
| `/clan list` | Lista los clanes guardados |
| `/clan add_member` | Añade un miembro a un clan |
| `/clan remove_member` | Elimina un miembro de un clan |

### Rust+ (Integración directa con el servidor)

| Comando | Uso |
| --- | --- |
| `/rustplus pair` | Empareja el bot con un servidor de Rust vía Rust+ |
| `/rustplus unpair` | Desempareja un servidor |
| `/rustplus list` | Lista los servidores emparejados y su estado |
| `/rustplus server_info` | Información en tiempo real del servidor |
| `/rustplus team_info` | Miembros del equipo y estado online/offline |
| `/rustplus send_message` | Envía un mensaje al chat de equipo del juego |
| `/rustplus alarm_add` | Registra una alarma inteligente (Smart Alarm) |
| `/rustplus alarm_remove` | Elimina una alarma registrada |
| `/rustplus alarm_list` | Lista las alarmas de un servidor |
| `/rustplus switch_add` | Registra un interruptor inteligente (Smart Switch) |
| `/rustplus switch_remove` | Elimina un interruptor registrado |
| `/rustplus switch_on` | Activa un Smart Switch desde Discord |
| `/rustplus switch_off` | Desactiva un Smart Switch desde Discord |
| `/rustplus reconnect` | Reconecta el socket de un servidor |

## Integración Rust+

Rustalker puede conectarse directamente a tu servidor de Rust a través del protocolo Rust+ (WebSocket), sin necesidad de ser administrador ni modificar el servidor.

### Cómo obtener el `player_token`

1. Descarga la app oficial de **Rust+** en tu móvil.
2. En el juego, abre el menú y empareja tu base con la app.
3. Usa una de estas herramientas para capturar el token generado:
   - Extensión de navegador de la comunidad rustplus.py (disponible en su documentación).
   - Acceso al archivo `player.tokens.db` si eres administrador del servidor.
4. Una vez tengas el token, usa `/rustplus pair` en Discord.

### Flujo de trabajo típico

```text
1. /rustplus pair ip:123.45.67.89 port:28082 steam_id:76561198XXXX player_token:XXXXX
2. /rustplus alarm_add pairing_id:1 entity_id:123456 label:"Puerta principal" channel:#alertas
3. /rustplus switch_add pairing_id:1 entity_id:789012 label:"Torretas"
4. /rustplus switch_on pairing_id:1 entity_id:789012   → ¡Torretas activadas desde Discord!
```

### Funcionalidades en tiempo real

- **Alertas de raid:** Cuando una Smart Alarm del juego se activa (explosión, intruso), el bot envía un embed de alerta inmediata al canal configurado.
- **Relay de chat:** Los mensajes del chat de equipo del juego se reenvían automáticamente a un canal de Discord, y viceversa con `/rustplus send_message`.
- **Control de dispositivos:** Activa o desactiva torretas, generadores o trampas directamente con botones en Discord.

## Estructura

- `main.py` arranque del bot y carga de extensiones.
- `cogs/commands.py` comandos slash y utilidades de análisis.
- `cogs/tracker.py` tareas en segundo plano para alertas de BattleMetrics.
- `cogs/rustplus_cog.py` integración completa con Rust+ via WebSocket.
- `database.py` capa SQLite y esquema (incluye tablas Rust+).
- `battlemetrics.py` cliente para la API de BattleMetrics.

## Notas

- La sincronización global de slash commands puede tardar un poco en reflejarse.
- Si el bot no responde, revisa el token de Discord y el acceso de red a BattleMetrics.
- El archivo de base de datos se puede borrar si quieres empezar desde cero.
- La integración Rust+ es **opcional**: si no instalas `rustplus` o no configuras ningún pairing, el resto del bot funciona con normalidad.
- El puerto Companion de Rust+ (por defecto `28082`) es **diferente** al puerto del juego (normalmente `28015`).
