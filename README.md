# ♪ Nocturne

Reproductor de música para la terminal: explorador de carpetas, espectro en
tiempo real dibujado con caracteres y la carátula del disco renderizada con
chafa. Sin dependencias de Python — sólo ffmpeg y, opcionalmente, chafa.

```
 ♪ NOCTURNE  ~/Downloads                                                               ▶ sonando
╭─ BIBLIOTECA ──────── 2 elem. ─╮ ╭─ REPRODUCIENDO ──────────────────────────────────────── 1/1 ─╮
│ ↑ ..                          │ │ ▏▄▄▂▗▄▄▄▃▄▄▄▄▄▄▄▄▂                                           │
│ ▶ HAVEN. - I Run.mp3     2:09 │ │ ▕▏▕▝▋▏▏ ▔ ▋▗▊▏▕▏▕▏  I Run                                    │
│                               │ │ ▕▏▕▖▋▕▔▏▗ ▋▏▊▏▁▔▕▄  Haven., Kaitlin Aragon                   │
│                               │ │ ▎▏▕▍▋▎▗▌▍▁▝▖▊▏▊▏▕▕  I Run · 2025                             │
│                               │ │ ▕▅▅▁▝▅▅▅▘▅▄▅▅▅▅▅▅▌  Electro                                  │
│                               │ │ ▕▏▁▝▘▔▁▝▔▏▝▁▖▏▕▋▆▎                                           │
│                               │ │ ▗▝▆▆▎▗▖▏▏▏▕▏▏▊▝▃▘▊  MP3 · 320 kbps · 44.1 kHz · estéreo      │
│                               │ │ ▏▌▂▘▂▕▁▕▔▏▕▆▏▗▄▞▘▊                                           │
│                               │ │ ▖▕▁▝▄▝▝▁▂▅▃▅▖▃▄▏▃▏                                           │
│                               │ ╰──────────────────────────────────────────────────────────────╯
│                               │ ╭─ ESPECTRO ──────────────────────────────────────── espectro ─╮
│                               │ │                                           ▔                  │
│                               │ │                         ▔     ▔     ▁   ▃ ▂   ▔ ▆            │
│                               │ │                     ▔ ▔ ▆ ▔ ▔ █ ▇ ▁ █ ▔ █ █ ▔ █ █ ▅ ▔        │
│                               │ │                   ▔   ▂ █ █ ▁ █ █ █ █ █ █ █ █ █ █ █ █ ▔ ▔ ▔  │
│                               │ │                   ▃ ▃ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ ▇ █ █  │
│                               │ │           ▁ ▔ ▔ ▔ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █  │
│                               │ │                 ▇ ▅ ▅ ▃ █ █ ▃ █ █ ▁ █ █ █ █ █ █ █ █ ▁ ▄ ▃ ▃  │
│                               │ │                         ▇     ▅ ▇   ▆   ▅ ▆ ▇ ▅ ▄ ▇          │
╰───────────────────────────────╯ ╰──────────────────────────────────────────────────────────────╯
  0:03 ━━●───────────────────────────────────────────────────────────────────────────────── 2:09
                                                                               ♪ ▱▱▱▱▱▱▱▱▱▱  70%
  espacio sonar · ↑↓ mover · → abrir · ← atrás · ,. saltar · n/N pista · v vista · / buscar · ?…
```

El bloque de la izquierda del panel *REPRODUCIENDO* es la carátula del disco a
todo color; aquí se ve en blanco y negro porque el README es texto plano.

## Requisitos

| Herramienta | Para qué | Obligatorio |
|---|---|---|
| `python3` (3.10+) | el programa | sí |
| `ffplay`, `ffprobe`, `ffmpeg` | reproducir, leer etiquetas y decodificar | sí |
| `chafa` | dibujar la carátula | no |
| `librespot` | reproducir Spotify dentro de Nocturne | no |

```sh
brew install ffmpeg chafa
```

No hace falta instalar nada con `pip`: la FFT del espectro está escrita en
Python puro.

## Cómo se usa

```sh
./bin/nocturne              # la carpeta actual
./bin/nocturne ~/Music      # una carpeta concreta
./bin/nocturne cancion.mp3  # empieza sonando ese archivo
```

Para tenerlo a mano desde cualquier sitio, enlázalo a tu PATH:

```sh
ln -s "$PWD/bin/nocturne" /opt/homebrew/bin/nocturne
```

…y a partir de ahí basta con escribir `nocturne` dentro de la carpeta de música.

### Opciones

| Opción | Qué hace |
|---|---|
| `-v`, `--volume 0-100` | volumen inicial (80 por defecto) |
| `--viz bars\|wave\|dual` | visualizador con el que arranca |
| `--no-art` | no dibujar carátulas |
| `--symbols ...` | juego de símbolos de chafa (`vhalf+block+space` por defecto) |
| `--fps N` | refrescos por segundo (30 por defecto) |
| `--sync SEG` | desplaza el espectro respecto al audio si lo notas adelantado |
| `--spotify` | arrancar directamente en el árbol de Spotify |
| `--spotify-login` | autorizar Nocturne en tu cuenta (ver más abajo) |
| `--spotify-device-login` | autorizar el altavoz librespot (una sola vez) |
| `--no-spotify` | ignorar Spotify aunque esté configurado |
| `--spotify-audio ...` | `auto`, `pipe`, `loopback` o `none` |
| `--spotify-lag SEG` | retraso del espectro respecto a lo que ya suena |

## Teclas

**Reproducción**

| Tecla | Acción |
|---|---|
| `espacio`, `p` | reproducir o pausar |
| `Intro`, `→` | abrir la carpeta o hacer sonar la pista |
| `n` / `N` | pista siguiente / anterior |
| `.` / `,` | saltar ±5 s |
| `>` / `<` | saltar ±30 s |
| `0`–`9` | saltar al 0 %–90 % de la pista |
| `s` | aleatorio |
| `r` | repetir: no → toda la carpeta → una sola |
| `a` | encadenar pistas al terminar (activado por defecto) |

**Navegación**

| Tecla | Acción |
|---|---|
| `↑` `↓` o `k` `j` | mover la selección |
| `→` o `l` | entrar en la carpeta |
| `←` o `h` | volver a la carpeta anterior |
| `g` / `G` | principio / final de la lista |
| `RePág` / `AvPág` | página arriba / abajo |
| `/` | buscar en la carpeta (`Esc` limpia) |
| `u` | recargar la carpeta |
| `o` | mostrar también archivos que no son de audio |

**Spotify**

| Tecla | Acción |
|---|---|
| `S` | saltar entre tus archivos y Spotify |
| `/` | dentro de Spotify, `Intro` busca en todo el catálogo |
| `d` | ver qué dispositivos hay a la escucha |

**Sonido y vista**

| Tecla | Acción |
|---|---|
| `+` / `-` | volumen ±5 |
| `m` | silenciar |
| `v` / `V` | cambiar de visualizador |
| `c` | mostrar u ocultar la carátula |
| `?` | ayuda |
| `q` | salir |

## Spotify

Nocturne puede navegar tus playlists y reproducirlas sin salir de la terminal.
Hace falta **Spotify Premium**: la API de Connect no deja controlar la
reproducción con una cuenta gratuita.

### Conectarlo, una sola vez

1. Entra en [el panel de Spotify](https://developer.spotify.com/dashboard) y
   crea una app (el nombre da igual).
2. Añade esta Redirect URI, exacta: `http://127.0.0.1:8898/callback`.
   Tiene que ser la IP: desde 2025 Spotify ya no acepta `localhost`.
3. Copia el *client id* y ejecuta:

```sh
nocturne --spotify-login --spotify-client-id TU_CLIENT_ID
```

Se abre el navegador, autorizas y listo. El refresh token queda en
`~/.config/nocturne/spotify.json` con permisos `600` y se renueva solo; no hace
falta client secret porque el flujo es Authorization Code + PKCE. Para borrarlo,
`nocturne --spotify-logout`.

Dentro de Nocturne, `S` salta entre tus carpetas y el árbol de Spotify:
playlists, canciones guardadas, álbumes, artistas seguidos y búsqueda. Cada
árbol recuerda dónde lo dejaste. La cola, el aleatorio y la repetición las
sigue llevando Nocturne: las pistas se mandan de una en una para que Spotify no
encadene por su cuenta.

### De dónde sale el sonido

Nocturne **controla** la reproducción, pero el audio lo pone un dispositivo de
Spotify Connect. Hay dos formas de montarlo, y de ellas depende si hay espectro:

**a) librespot — todo en la terminal, con espectro exacto.** `librespot` es un
cliente de Connect de ingeniería inversa que sabe escribir PCM crudo a un FIFO.
Nocturne lo lee, lo manda a la salida de audio con ffmpeg y de camino se queda
una copia para la FFT, así que el dibujo corresponde exactamente a lo que suena.

```sh
brew install librespot
nocturne --spotify-device-login    # una sola vez
```

A partir de ahí no hay que hacer nada más: Nocturne arranca librespot solo al
reproducir la primera canción y lo mata al salir. El altavoz se llama
**Nocturne** y tiene prioridad sobre cualquier otro dispositivo tuyo.

Ese login es aparte del de Nocturne porque librespot usa su propio client id de
Spotify; las credenciales quedan en `~/.cache/nocturne/librespot`.

Aviso honesto: librespot incumple los términos de servicio de Spotify. En la
práctica lo usan miles de instalaciones (moOde, Volumio, spotifyd) y los baneos
por esto son rarísimos, pero la cuenta es tuya y la decisión también.

**b) La app oficial + un bucle de audio — sin zona gris.** Deja sonando la app
de Spotify y captura la salida del sistema con un dispositivo de bucle
([BlackHole](https://existential.audio/blackhole) y un dispositivo de salida
múltiple, por ejemplo). Nocturne lo detecta solo y lo usa para el espectro.

Sin ninguna de las dos, todo funciona igual: navegas, reproduces y controlas,
pero el panel del espectro se queda quieto. La API de Spotify no da acceso al
audio, y los endpoints de análisis (`audio-features`, `audio-analysis`) están
cerrados a las apps nuevas desde noviembre de 2024.

Si el espectro va desfasado respecto al sonido, ajústalo con `--spotify-lag`
(segundos; por defecto 0,22).

## Los tres visualizadores

- **espectro** — barras por bandas logarítmicas (40 Hz – 10 kHz) con reflejo y
  marcas de pico que caen despacio.
- **onda** — osciloscopio dibujado con puntos braille, a cuádruple resolución
  vertical.
- **dual** — barras arriba, onda abajo.

## Cómo funciona por dentro

- **Reproducción**: `ffplay -nodisp`. Su reloj maestro (la posición realmente
  audible) se lee de stderr cada ~30 ms, así que la barra y el espectro no se
  desincronizan nunca. La pausa se hace con `SIGSTOP`/`SIGCONT` descontando el
  tiempo parado, y los saltos relanzan el proceso con `-ss`.
- **Espectro**: al abrir una pista se decodifica entera a PCM mono 22 050 Hz en
  un archivo temporal (≈0,2 s) que se mapea en memoria. Cada frame se lee una
  ventana de 2048 muestras en la posición exacta de reproducción y se le aplica
  una FFT radix-2 en Python puro (~1,3 ms). Un control automático de ganancia
  mantiene los picos rozando el techo sin bombear.
- **Carátula**: se extrae la imagen incrustada con ffmpeg (o se busca un
  `cover.jpg` al lado) y se pasa por chafa. El bloque ANSI resultante se escribe
  directamente en el terminal sobre un rectángulo que curses deja intacto.
- **Etiquetas**: `ffprobe` en hilos de fondo, dando prioridad a lo que está
  visible en pantalla, para que una carpeta con cientos de canciones no se
  atasque.

- **Spotify**: el catálogo se navega con el Web API y se reproduce por Connect,
  con un reloj local que se corrige contra `/me/player` cada dos segundos (más a
  menudo daría con el rate limit). Las conexiones HTTPS se reutilizan: abrir
  TCP+TLS contra la API cuesta unos 45 ms y se pagaba en cada orden. Al pasar a
  la pista siguiente del mismo contexto se manda `/me/player/next` en vez de un
  play nuevo, porque librespot ya la tiene descargada y suena al momento; los
  saltos que no siguen el orden (aleatorio, repetir una) sí necesitan el play.
  El audio ya descargado se cachea en `~/.cache/nocturne/librespot/audio`
  (hasta 1 GB). El espectro sale de un búfer circular de seis
  segundos que alimenta ffmpeg, leyendo unos milisegundos por detrás de la
  cabeza para compensar la latencia de la salida de audio.

Todos los procesos hijo se lanzan en su propia sesión: sin terminal de control
no pueden interrogar al terminal ni robarle las pulsaciones al usuario.

## Estructura

```
nocturne/
  app.py          bucle principal, teclado y cola de reproducción
  player.py       control de ffplay (posición, pausa, salto, volumen)
  analysis.py     caché de PCM, FFT y bandas del espectro
  visualizer.py   barras, osciloscopio braille y suavizado
  artwork.py      extracción de carátulas y render con chafa
  library.py      exploración de carpetas y metadatos
  spotify/
    auth.py       OAuth PKCE y custodia del refresh token
    client.py     cliente del Web API sobre urllib
    source.py     el catálogo de Spotify como árbol de carpetas
    player.py     control de reproducción por Spotify Connect
    capture.py    PCM para el espectro (FIFO de librespot o bucle de audio)
    device.py     arranca y para librespot como altavoz propio
    session.py    monta las piezas y decide de dónde sale el audio
  ui.py           composición y dibujo en curses
  theme.py        paleta azul noche
  util.py         texto, tiempos y orden natural
bin/nocturne      lanzador
```

## Si algo falla

**`cbreak() returned ERR`, o aparecen códigos como `[?1049h` sueltos.** El
programa no ha recibido una terminal interactiva: suele pasar al lanzarlo desde
el prompt de otra herramienta o a través de una tubería. Nocturne reabre
`/dev/tty` por su cuenta cuando puede; si no existe ninguna, lo dice y sale.
Ábrelo en una ventana de terminal normal.

**No se ve la carátula.** Comprueba que tienes `chafa` (`brew install chafa`) y
que la pista lleva imagen incrustada o un `cover.jpg` al lado. Con `c` se
alterna.

**Para depurar.** `NOCTURNE_DEBUG_LOG=ruta` traza la navegación y las órdenes
de reproducción; `NOCTURNE_LIBRESPOT_LOG=ruta` guarda la salida de librespot,
que es lo único que explica por qué una pista concreta no suena.

**El espectro va adelantado respecto al sonido.** Ajústalo con `--sync 0.05`
(segundos, admite negativos). En Spotify, con `--spotify-lag`.

**librespot se queda en «Connecting to AP …:4070» y no aparece nunca.** La red
bloquea ese puerto. Pasa en wifis de institutos, oficinas y hoteles, que dejan
salir sólo 80 y 443. El login y la navegación siguen funcionando porque van por
HTTPS; lo que no puede es reproducir. Prueba con otra conexión.

**«No hay ningún dispositivo de Spotify a la escucha».** Nocturne manda órdenes,
no abre el audio por su cuenta. Con librespot instalado y autorizado se encarga
él solo; si no, necesita la app oficial abierta y sonando. Con `d` ves qué
dispositivos encuentra.

**Spotify responde «esta acción requiere Premium».** Es así: la API de Connect
no controla cuentas gratuitas. Navegar y ver metadatos sí funciona.
